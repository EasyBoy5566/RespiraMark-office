"""
RespiraMark Office — 中央監視儀表板 進入點
============================================
- TCP :8765  接收各 Pi 的遙測資料（JSON Lines，見 PROTOCOL.md；可 TLS）
- HTTP(S) :8080 提供儀表板網頁 + WebSocket /ws 即時廣播（可 TLS + 登入）

啟動：python main.py（或雙擊 start_server.bat）
設定：config.json（不存在則用預設值，範本見 config.json.example）
憑證：python tools/make_certs.py；帳號：python tools/make_user.py；裝置權杖：python tools/make_device.py

本檔是 composition root——唯一同時 import 三層並組裝的地方：
    transport(ingest) ──▶ domain(hub) ◀── web(routes)
"""

import argparse
import asyncio
import logging
import os
import socket
import ssl
import sys
from logging.handlers import RotatingFileHandler

from monitor.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config
from monitor.domain.hub import TelemetryHub
from monitor.transport.device_auth import DeviceRegistry
from monitor.transport.ingest import start_ingest
from monitor.web.auth import AuthManager, LocalAuthenticator
from monitor.web.ldap_auth import LdapAuthenticator
from monitor.web.routes import start_web


def _resolve(path: str) -> str:
    """相對路徑一律以專案根目錄為基準（不受啟動 cwd 影響）"""
    if path and not os.path.isabs(path):
        return os.path.join(PROJECT_ROOT, path)
    return path


def build_ssl_context(cfg: dict):
    """依設定建立伺服器 TLS context；未設定回傳 None（明文，僅限開發）。
    設了卻讀不到檔案 → 直接結束：寧可不啟動，也不要靜默退回明文。"""
    cert = _resolve(str(cfg.get("tls_cert") or ""))
    key = _resolve(str(cfg.get("tls_key") or ""))
    if not cert and not key:
        return None
    if not (cert and key) or not os.path.exists(cert) or not os.path.exists(key):
        print("錯誤：config.json 已設定 tls_cert/tls_key，但憑證檔不存在。")
        print("  請先執行 python tools/make_certs.py 產生憑證，")
        print("  或把 tls_cert/tls_key 清成空字串以停用加密（僅限開發環境）。")
        sys.exit(1)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def build_authenticator(cfg: dict):
    """依 auth_backend 建立驗證器（local=預設／ldap=院內 LDAP/AD，見 W-307）"""
    backend = str(cfg.get("auth_backend") or "local").lower()
    if backend == "ldap":
        return LdapAuthenticator(
            server_uri=str(cfg.get("ldap_server") or ""),
            bind_template=str(cfg.get("ldap_bind_template") or "{username}"),
            roles_path=_resolve(str(cfg.get("accounts_file") or "accounts.json")),
            use_ssl=bool(cfg.get("ldap_use_ssl", True)),
            timeout=float(cfg.get("ldap_timeout") or 5.0),
            ca_certs_file=_resolve(str(cfg.get("ldap_ca") or "")))
    return LocalAuthenticator(_resolve(str(cfg.get("accounts_file") or "accounts.json")))


def build_auth_manager(cfg: dict, tls_on: bool):
    """依設定建立登入管理器；auth_enabled=false 回傳 None（僅限開發）"""
    log = logging.getLogger("main")
    if not cfg.get("auth_enabled"):
        log.warning("登入驗證未啟用（auth_enabled=false）——僅限開發環境使用")
        return None
    authenticator = build_authenticator(cfg)
    mgr = AuthManager(authenticator,
                      idle_minutes=float(cfg.get("session_idle_minutes") or 0),
                      secure_cookie=tls_on,
                      absolute_hours=float(cfg.get("session_absolute_hours") or 0),
                      max_sessions=int(cfg.get("session_max") or 200))
    if not mgr.has_users():
        backend = str(cfg.get("auth_backend") or "local").lower()
        hint = ("請先執行 python tools/make_user.py --user <帳號> --role admin"
               if backend != "ldap" else
               "請先在 accounts.json 加入至少一筆 {\"username\":...,\"role\":...}"
               "（LDAP 模式不需要 password 欄位，帳密驗證交給 LDAP）")
        log.warning(f"帳號/角色名單尚無任何項目，目前無人能登入——{hint}")
    return mgr


def setup_server_log(cfg: dict):
    """一般運行 log 同步寫檔 `logs/server.log`（10MB×5 輪替，UTF-8）；
    主控台照印不受影響。掛在 root logger，涵蓋 hub/ingest/auth/main 等
    全部既有 logger。檔案一律 UTF-8——主控台在 cp950 語系 Windows 下
    印中文常亂碼，但寫檔不受主控台編碼影響（對應 IMPROVEMENT_PLAN.md F-06）。"""
    log_dir = _resolve(str(cfg.get("log_dir") or "logs"))
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "server.log"),
                                  maxBytes=10 * 1024 * 1024, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(handler)


def setup_audit_log(cfg: dict):
    """掛上審計日誌的檔案 handler（logs/audit.log，10MB×5 輪替，UTF-8）。
    只寫檔、不印主控台（避免跟一般運行 log 混在一起）；
    monitor/audit.py 的 audit() 呼叫最終都會經由這個 handler 落地。"""
    log_dir = _resolve(str(cfg.get("log_dir") or "logs"))
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "audit.log"),
                                  maxBytes=10 * 1024 * 1024, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    audit_logger = logging.getLogger("audit")
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False


def lan_ip() -> str:
    """找出本機對外的區網 IP（不會真的發封包）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def print_banner(cfg: dict, tls_on: bool, auth_on: bool):
    ip = lan_ip()
    scheme = "https" if tls_on else "http"
    print()
    print("=" * 62)
    print("  RespiraMark Office 彙整伺服器已啟動")
    print(f"  儀表板（本機）:   {scheme}://localhost:{cfg['web_port']}")
    print(f"  儀表板（區網）:   {scheme}://{ip}:{cfg['web_port']}")
    print(f"  Pi 端 telemetry.json 的 server_host 請填: {ip}")
    # 注意：只用 cp950 可編碼的字元（Windows 輸出重導向到檔案時非 UTF-8）
    print(f"  Pi 資料接收 port: {cfg['ingest_port']}"
          + ("（TLS 加密，Pi 端需設 tls: true）" if tls_on else ""))
    print(f"  傳輸加密: {'已啟用' if tls_on else '未啟用（僅限開發環境）'}"
          f"    登入驗證: {'已啟用' if auth_on else '未啟用（僅限開發環境）'}")
    print("  （若其他裝置連不上，請確認 Windows 防火牆已放行以上兩個 port）")
    print("=" * 62)
    print()


async def main():
    ap = argparse.ArgumentParser(description="RespiraMark Office 彙整伺服器")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help="設定檔路徑（預設: 專案目錄的 config.json）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    setup_server_log(cfg)
    setup_audit_log(cfg)

    sys_log_dir = _resolve(str(cfg.get("sys_log_dir") or ""))
    alarm_log_dir = _resolve(str(cfg.get("alarm_log_dir") or ""))

    # 安全設定（TLS 與登入）
    ssl_ctx = build_ssl_context(cfg)
    authmgr = build_auth_manager(cfg, tls_on=ssl_ctx is not None)

    # 裝置權杖：devices.json 存在則每台獨立驗證，否則退回單一 ingest_token（向後相容）
    devices = DeviceRegistry(_resolve(str(cfg.get("devices_file") or "devices.json")))
    log = logging.getLogger("main")
    if devices.exists():
        log.info(f"裝置權杖模式：每台獨立（devices.json，共 {len(devices.list_devices())} 台）")
    elif cfg.get("ingest_token"):
        log.info("裝置權杖模式：單一共用 ingest_token（建議改用 tools/make_device.py 逐台核發）")
    else:
        log.warning("裝置權杖模式：未設定（僅限開發環境，任何裝置皆可連入）")

    # 組裝三層
    hub = TelemetryHub(offline_timeout=float(cfg["offline_timeout"]),
                       max_devices=int(cfg["max_devices"]),
                       sys_history_max=int(cfg["sys_history_max"]),
                       sys_log_dir=sys_log_dir,
                       sys_csv_interval=float(cfg["sys_csv_interval"]),
                       max_viewers=int(cfg["max_viewers"]),
                       alarm_log_dir=alarm_log_dir)
    ingest_server = await start_ingest(hub, int(cfg["ingest_port"]),
                                       token=str(cfg["ingest_token"] or ""),
                                       ssl_ctx=ssl_ctx,
                                       max_conns=int(cfg["ingest_max_conns"]),
                                       hello_timeout=float(cfg["ingest_hello_timeout"]),
                                       idle_timeout=float(cfg["ingest_idle_timeout"]),
                                       devices=devices)
    web_runner = await start_web(hub, int(cfg["web_port"]),
                                 ssl_ctx=ssl_ctx, authmgr=authmgr)
    watchdog = asyncio.ensure_future(hub.watchdog())
    auth_watchdog = asyncio.ensure_future(authmgr.watchdog()) if authmgr else None

    print_banner(cfg, tls_on=ssl_ctx is not None, auth_on=authmgr is not None)
    try:
        await asyncio.Event().wait()           # 永久運行，Ctrl+C 結束
    finally:
        watchdog.cancel()
        if auth_watchdog:
            auth_watchdog.cancel()
        ingest_server.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n伺服器已停止")
