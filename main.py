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
import datetime
import logging
import os
import socket
import ssl
import sys
from logging.handlers import TimedRotatingFileHandler

from monitor.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config
from monitor.domain.device_directory import DeviceDirectory
from monitor.domain.hub import TelemetryHub
from monitor.domain.pairing import PairingService
from monitor.transport.device_auth import DeviceRegistry
from monitor.transport.ingest import start_ingest
from monitor.web.auth import AuthManager, LocalAuthenticator
from monitor.web.routes import start_web


def _resolve(path: str) -> str:
    """相對路徑一律以專案根目錄為基準（不受啟動 cwd 影響）"""
    if path and not os.path.isabs(path):
        return os.path.join(PROJECT_ROOT, path)
    return path


def _resolve_log_path(log_dir: str, path: str) -> str:
    """日誌內的相對路徑一律從統一 log_dir 解析。"""
    if path and not os.path.isabs(path):
        return os.path.normpath(os.path.join(log_dir, path))
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
    """建立帳密驗證器。

    目前只有本機 accounts.json 一種；院方定案登入改走 HIS，介接規格到手後
    在這裡多建一種驗證器即可（AuthManager 只認 authenticate()/has_users()/
    list_users() 介面，其餘程式不用動）。
    """
    return LocalAuthenticator(_resolve(str(cfg.get("accounts_file") or "accounts.json")))


def build_auth_manager(cfg: dict, tls_on: bool):
    """依設定建立登入管理器；auth_enabled=false 回傳 None（僅限開發）"""
    log = logging.getLogger("main")
    if not cfg.get("auth_enabled"):
        log.warning("登入驗證未啟用（auth_enabled=false）——僅限開發環境使用")
        return None
    mgr = AuthManager(build_authenticator(cfg),
                      idle_minutes=float(cfg.get("session_idle_minutes") or 0),
                      secure_cookie=tls_on,
                      absolute_hours=float(cfg.get("session_absolute_hours") or 0),
                      max_sessions=int(cfg.get("session_max") or 200))
    if not mgr.has_users():
        log.warning("帳號名單尚無任何項目，目前無人能登入——"
                    "請先執行 python tools/make_user.py --user <帳號> --role admin")
    return mgr


class LocalIsoFormatter(logging.Formatter):
    """時戳寫成含時區位移的 ISO 8601（例如 `2026-08-11T14:23:05+08:00`）。

    資通系統防護基準第 24 點要求日誌時戳「可以對應到世界協調時間(UTC)」：
    只記本地時刻而不帶位移，事後跨系統比對無從還原成 UTC，跨日光節約或
    換時區的機器更會直接對不上。改用 datetime.astimezone() 取本機位移，
    不依賴各平台 strftime 對 %z 的實作差異（Windows 的 %z 會回時區名稱，
    不是數字位移）。
    """

    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created).astimezone()
        return dt.isoformat(timespec="seconds")


def _daily_log_handler(path: str, fmt: str, retention_days: int):
    """每日輪替、保留 retention_days 天的 UTF-8 檔案 handler。

    改用日期輪替（原本是 10MB×5 的大小輪替）的原因：防護基準第 16 點要求
    日誌「保留至少 6 個月」，而大小輪替只保證留下最近 50MB，日誌一忙就會把
    幾個月前的紀錄擠掉，無法對稽核交代保存期限。
    檔案一律 UTF-8——主控台在 cp950 語系 Windows 下印中文常亂碼，但寫檔
    不受主控台編碼影響（對應 IMPROVEMENT_PLAN.md F-06）。
    """
    handler = TimedRotatingFileHandler(path, when="midnight",
                                       backupCount=retention_days,
                                       encoding="utf-8")
    handler.setFormatter(LocalIsoFormatter(fmt))
    return handler


def _log_retention_days(cfg: dict) -> int:
    """保留天數；低於 180 天（6 個月）視為設定錯誤，拉回下限並警告。"""
    days = int(cfg.get("log_retention_days") or 190)
    if days < 180:
        logging.getLogger("main").warning(
            f"log_retention_days={days} 低於資通系統防護基準要求的 6 個月，已改用 180")
        return 180
    return days


def setup_server_log(cfg: dict):
    """一般運行 log 同步寫檔 `logs/server.log`；主控台照印不受影響。
    掛在 root logger，涵蓋 hub/ingest/auth/main 等全部既有 logger。"""
    log_dir = _resolve(str(cfg.get("log_dir") or "logs"))
    os.makedirs(log_dir, exist_ok=True)
    logging.getLogger().addHandler(_daily_log_handler(
        os.path.join(log_dir, "server.log"),
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        _log_retention_days(cfg)))


def setup_audit_log(cfg: dict):
    """掛上審計日誌的檔案 handler（logs/audit.log）。
    只寫檔、不印主控台（避免跟一般運行 log 混在一起）；
    monitor/audit.py 的 audit() 呼叫最終都會經由這個 handler 落地。"""
    log_dir = _resolve(str(cfg.get("log_dir") or "logs"))
    os.makedirs(log_dir, exist_ok=True)
    audit_logger = logging.getLogger("audit")
    audit_logger.addHandler(_daily_log_handler(
        os.path.join(log_dir, "audit.log"), "%(asctime)s %(message)s",
        _log_retention_days(cfg)))
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


def print_banner(cfg: dict, tls_on: bool, auth_on: bool, pair_on: bool = False):
    ip = lan_ip()
    scheme = "https" if tls_on else "http"
    print()
    print("=" * 62)
    print("  RespiraMark Office 彙整伺服器已啟動")
    print(f"  儀表板（本機）:   {scheme}://localhost:{cfg['web_port']}")
    print(f"  儀表板（區網）:   {scheme}://{ip}:{cfg['web_port']}")
    print(f"  Pi 端請在設定頁填入伺服器位址: {ip}"
          + ("（按「配對」申請，再到 /admin 核可）" if pair_on else ""))
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

    log_dir = _resolve(str(cfg.get("log_dir") or "logs"))
    sys_db_path = _resolve_log_path(log_dir, str(cfg.get("sys_db_path") or ""))
    alarm_db_path = _resolve_log_path(log_dir, str(cfg.get("alarm_db_path") or ""))

    # 安全設定（TLS 與登入）
    ssl_ctx = build_ssl_context(cfg)
    authmgr = build_auth_manager(cfg, tls_on=ssl_ctx is not None)

    # 裝置權杖：devices.json 存在則每台獨立驗證，否則退回單一 ingest_token（向後相容）
    # 同一個檔案兩種用途：驗證由 transport 的 DeviceRegistry 唯讀比對，
    # 清冊（床號／財編）的讀改寫集中在 domain 的 DeviceDirectory。
    devices_path = _resolve(str(cfg.get("devices_file") or "devices.json"))
    devices = DeviceRegistry(devices_path)
    directory = DeviceDirectory(devices_path)
    log = logging.getLogger("main")
    if devices.exists():
        log.info(f"裝置權杖模式：每台獨立（devices.json，共 {len(devices.list_devices())} 台）")
    elif cfg.get("ingest_token"):
        log.info("裝置權杖模式：單一共用 ingest_token（建議改用配對流程或 tools/make_device.py 逐台核發）")
    else:
        log.warning("裝置權杖模式：未設定（僅限開發環境，任何裝置皆可連入）")

    # 裝置配對（讀 devices.json 的是上面的 DeviceRegistry，寫入的是這裡；見 PROTOCOL.md）
    pairing = None
    if cfg.get("pair_enabled", True):
        pairing = PairingService(directory, int(cfg["ingest_port"]),
                                 ttl=float(cfg["pair_ttl"]),
                                 max_pending=int(cfg["pair_max_pending"]))
        log.info(f"裝置配對已啟用：Pi 可在 :{cfg['web_port']} 申請，於 /admin 核可")
        if not devices.exists() and cfg.get("ingest_token"):
            log.warning("目前是單一共用 ingest_token 模式：第一次核可配對會建立 devices.json "
                        "並切換成每台獨立驗證，屆時未登記的舊裝置重連會被拒絕")
        if ssl_ctx is not None:
            log.info("TLS 已啟用：Pi 端配對走 HTTPS，該台 telemetry.json 需先填好 "
                     "tls=true 與 tls_ca（院內 CA 根憑證），否則配對會因憑證驗證失敗被拒")

    # 組裝三層
    hub = TelemetryHub(offline_timeout=float(cfg["offline_timeout"]),
                       max_devices=int(cfg["max_devices"]),
                       sys_history_max=int(cfg["sys_history_max"]),
                       sys_db_path=sys_db_path,
                       sys_persist_interval=float(cfg["sys_persist_interval"]),
                       sys_retention_days=int(cfg["sys_retention_days"]),
                       max_viewers=int(cfg["max_viewers"]),
                       alarm_db_path=alarm_db_path,
                       alarm_retention_days=int(cfg["alarm_retention_days"]),
                       directory=directory)
    ingest_server = await start_ingest(hub, int(cfg["ingest_port"]),
                                       token=str(cfg["ingest_token"] or ""),
                                       ssl_ctx=ssl_ctx,
                                       max_conns=int(cfg["ingest_max_conns"]),
                                       hello_timeout=float(cfg["ingest_hello_timeout"]),
                                       idle_timeout=float(cfg["ingest_idle_timeout"]),
                                       devices=devices)
    web_runner = await start_web(hub, int(cfg["web_port"]),
                                 ssl_ctx=ssl_ctx, authmgr=authmgr, pairing=pairing)
    watchdog = asyncio.ensure_future(hub.watchdog())
    auth_watchdog = asyncio.ensure_future(authmgr.watchdog()) if authmgr else None

    print_banner(cfg, tls_on=ssl_ctx is not None, auth_on=authmgr is not None,
                 pair_on=pairing is not None)
    try:
        await asyncio.Event().wait()           # 永久運行，Ctrl+C 結束
    finally:
        watchdog.cancel()
        if auth_watchdog:
            auth_watchdog.cancel()
        ingest_server.close()
        await web_runner.cleanup()
        hub.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n伺服器已停止")
