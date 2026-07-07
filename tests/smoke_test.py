# -*- coding: utf-8 -*-
"""
smoke_test — push 前必跑的端對端冒煙測試（開發 SOP，見 CLAUDE.md）
====================================================================
一鍵執行：產生測試憑證與帳號（admin + viewer）→ 啟動伺服器（TLS + 登入，
測試專用 port）→ 跑三台 fake_pi（TLS）→ 模擬瀏覽器登入後連 /ws 驗證
snapshot 與即時廣播 → 驗證未登入/錯誤密碼/錯誤 token 都被拒 → 管理頁
權限（viewer 擋、admin 通）、帳號清單、CSV 下載、移除離線裝置與
device_removed 廣播 → 全部關閉。

用法：
    python tests/smoke_test.py

通過 → exit code 0；任一項失敗 → exit code 1 並印出伺服器 log 供除錯。
使用 18080/18765 測試 port 與暫存目錄的憑證/帳號檔，不影響正式伺服器。
"""

import asyncio
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
WEB_PORT = 18080
INGEST_PORT = 18765
TOKEN = "SMOKETOKEN"     # 測試用 ingest_token，驗證 token 驗證路徑
ADMIN_USER = "smokeadmin"
ADMIN_PASS = "SmokePass123"
VIEWER_USER = "smokeview"
VIEWER_PASS = "SmokeView123"

TMP = tempfile.gettempdir()
SYS_LOG_DIR = os.path.join(TMP, "respiramark_smoke_syslogs")
CERT_DIR = os.path.join(TMP, "respiramark_smoke_certs")
ACCOUNTS = os.path.join(TMP, "respiramark_smoke_accounts.json")
BASE = f"https://localhost:{WEB_PORT}"

sys.path.insert(0, BASE_DIR)
try:
    import aiohttp
except ImportError:
    print("FAIL: 未安裝 aiohttp（python -m pip install -r requirements.txt）")
    sys.exit(1)

FAILURES = []
CLIENT_SSL = None        # 信任測試 CA 的 client context（main() 產憑證後建立）
SMOKE3_PROC = None       # 第三台 fake_pi：移除離線裝置測試會先關掉它


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def start(cmd, log_file):
    return subprocess.Popen(cmd, cwd=BASE_DIR, stdout=log_file,
                            stderr=subprocess.STDOUT)


def new_session():
    """信任測試 CA 的 HTTPS client session（每個 session 有獨立 cookie jar）"""
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=CLIENT_SSL))


async def wait_web_up(timeout=10.0):
    deadline = time.time() + timeout
    async with new_session() as s:
        while time.time() < deadline:
            try:
                async with s.get(f"{BASE}/") as r:
                    if r.status == 200:
                        return True
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.3)
    return False


async def check_bad_token():
    """帶錯誤 token 的 hello 應被伺服器立即斷線（TLS 連線；True = 有被斷線）"""
    try:
        reader, writer = await asyncio.open_connection(
            "localhost", INGEST_PORT, ssl=CLIENT_SSL)
    except (OSError, ssl.SSLError):
        return False
    line = json.dumps({"type": "hello", "v": 1, "device": "smoke-bad",
                       "patient": "SMOKEBAD", "token": "WRONG"}) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()
    try:
        data = await asyncio.wait_for(reader.read(1), timeout=3.0)
        closed = data == b""              # EOF = 伺服器主動斷線
    except asyncio.TimeoutError:
        closed = False                    # 3 秒沒斷線 = 驗證沒生效
    writer.close()
    return closed


async def collect_ws(session, seconds=4.0):
    msgs = []
    async with session.ws_connect(f"{BASE}/ws") as ws:
        end = time.time() + seconds
        while time.time() < end:
            try:
                m = await ws.receive(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if m.type != aiohttp.WSMsgType.TEXT:
                break
            msgs.append(json.loads(m.data))
    return msgs


async def run_checks():
    check("網頁伺服器啟動（HTTPS 200）", await wait_web_up())

    # ── 登入驗證路徑 ────────────────────────────────────────────────
    async with new_session() as s:
        async with s.get(f"{BASE}/") as r:
            check("未登入自動導向登入頁", r.status == 200 and r.url.path == "/login")
        for path in ("/static/app.js", "/static/style.css", "/static/login.js",
                     "/static/sys.js", "/static/auth.js", "/static/admin.js",
                     "/static/alarm_levels.js"):
            async with s.get(f"{BASE}{path}") as r:
                check(f"靜態資源 {path}", r.status == 200)
        async with s.get(f"{BASE}/history/smoke-01") as r:
            check("未登入 /history 回 401", r.status == 401)
        try:
            await s.ws_connect(f"{BASE}/ws")
            check("未登入 /ws 被拒", False)
        except aiohttp.WSServerHandshakeError as e:
            check("未登入 /ws 被拒", e.status == 401, f"status={e.status}")
        async with s.post(f"{BASE}/login", data={
                "username": ADMIN_USER, "password": "wrong-password"}) as r:
            check("錯誤密碼被拒（導回登入頁）",
                  r.url.path == "/login" and "err" in r.url.query)

    # 登入成功的 session（後續所有資料檢查都用它）
    authed = new_session()
    async with authed.post(f"{BASE}/login", data={
            "username": ADMIN_USER, "password": ADMIN_PASS}) as r:
        check("正確帳密登入 → 進入儀表板", r.status == 200 and r.url.path == "/")
    async with authed.get(f"{BASE}/api/me") as r:
        me = await r.json() if r.status == 200 else {}
        check("/api/me 回報登入者與角色",
              me.get("username") == ADMIN_USER and me.get("role") == "admin",
              str(me))

    # ── ingest token（走 TLS）───────────────────────────────────────
    check("錯誤 token 被伺服器斷線", await check_bad_token())

    await asyncio.sleep(2.0)          # 讓 fake_pi 連上並開始送資料
    msgs = await collect_ws(authed, 4.0)

    check("WS 有收到訊息", len(msgs) > 10, f"共 {len(msgs)} 則")
    check("第一則是 snapshot", bool(msgs) and msgs[0]["type"] == "snapshot")
    if msgs and msgs[0]["type"] == "snapshot":
        devs = {d["device"]: d for d in msgs[0]["devices"]}
        check("snapshot 含兩台測試裝置",
              "smoke-01" in devs and "smoke-02" in devs, str(sorted(devs)))
        check("錯誤 token 裝置未進入 snapshot", "smoke-bad" not in devs)
        d1 = devs.get("smoke-01", {})
        check("snapshot 含病人代碼", d1.get("patient") == "SMOKE001")
        check("snapshot 含 params 快照", "params" in d1)
        sysm = d1.get("sys") or {}
        check("snapshot 含 sys 快照（系統狀態）",
              sysm.get("type") == "sys" and "cpu" in sysm, str(sysm)[:80])
        d2 = devs.get("smoke-02", {})
        alarms = (d2.get("alarm") or {}).get("alarms") or []
        check("snapshot 含 alarm 快照（--alarms 裝置）",
              len(alarms) >= 1 and "text" in alarms[0], str(alarms)[:80])
        check("alarm 帶 cp 欄位（codepage，供分級對照表消除同碼歧義）",
              len(alarms) >= 1 and "cp" in alarms[0], str(alarms)[:80])

    waves = [m for m in msgs if m["type"] == "wave"]
    src = {m["device"] for m in waves}
    check("測試裝置的波形皆有廣播", {"smoke-01", "smoke-02"} <= src, str(sorted(src)))
    if waves:
        w = waves[0]
        n = len(w["p"])
        check("wave 欄位齊全且等長",
              n > 0 and len(w["f"]) == n and len(w["v"]) == n and "trig" in w)
        total = sum(len(m["p"]) for m in waves if m["device"] == "smoke-01")
        check("波形速率合理（80~120Hz）", 80 <= total / 4.0 <= 120,
              f"{total / 4.0:.0f} 樣本/秒")

    # 系統狀態（sys）全鏈：即時廣播 → HTTP 歷史端點 → CSV 落地
    sysmsgs = [m for m in msgs if m["type"] == "sys"]
    check("sys 有廣播到瀏覽器", any("cpu" in m for m in sysmsgs),
          f"共 {len(sysmsgs)} 則")
    async with authed.get(f"{BASE}/history/smoke-01") as r:
        hist = (await r.json()).get("samples") if r.status == 200 else None
    check("history 端點回傳 sys 樣本",
          isinstance(hist, list) and len(hist) >= 1 and "cpu" in hist[0],
          f"{len(hist) if hist else 0} 筆")
    async with authed.get(f"{BASE}/history/nope") as r:
        empty = (await r.json()).get("samples") if r.status == 200 else None
    check("未知裝置 history 回傳空清單", empty == [])
    csv_path = os.path.join(SYS_LOG_DIR, "sys_smoke-01.csv")
    ok_csv = os.path.exists(csv_path) and sum(
        1 for _ in open(csv_path, encoding="utf-8")) >= 2
    check("sys 已落地 CSV（含表頭+資料）", ok_csv, csv_path)

    # ── 管理頁權限（PROTOCOL.md「管理頁」）──────────────────────────
    async with new_session() as s:
        async with s.get(f"{BASE}/admin") as r:
            check("未登入 /admin 導向登入頁",
                  r.status == 200 and r.url.path == "/login")

    viewer = new_session()
    async with viewer.post(f"{BASE}/login", data={
            "username": VIEWER_USER, "password": VIEWER_PASS}) as r:
        check("viewer 帳號可登入", r.status == 200 and r.url.path == "/")
    async with viewer.get(f"{BASE}/admin") as r:
        check("viewer 開 /admin 被導回儀表板",
              r.status == 200 and r.url.path == "/")
    async with viewer.get(f"{BASE}/api/admin/accounts") as r:
        check("viewer 存取管理 API 回 403", r.status == 403)
    await viewer.close()

    async with authed.get(f"{BASE}/admin") as r:
        check("admin 可開管理頁", r.status == 200 and r.url.path == "/admin")
    async with authed.get(f"{BASE}/api/admin/accounts") as r:
        data = await r.json() if r.status == 200 else {}
        users = {u.get("username"): u.get("role") for u in data.get("users", [])}
        no_secret = all("password" not in u for u in data.get("users", []))
        check("帳號清單含兩帳號且不含密碼雜湊",
              users.get(ADMIN_USER) == "admin"
              and users.get(VIEWER_USER) == "viewer" and no_secret, str(users))

    # ── 管理功能：CSV 下載、移除離線裝置 ────────────────────────────
    async with authed.get(f"{BASE}/api/admin/syslog/smoke-01") as r:
        body = await r.text() if r.status == 200 else ""
        check("admin 可下載 sys CSV（含表頭）",
              r.status == 200 and body.startswith("time,"), f"{len(body)} bytes")
    async with authed.get(f"{BASE}/api/admin/syslog/nope") as r:
        check("未知裝置 CSV 回 404", r.status == 404)

    async with authed.delete(f"{BASE}/api/admin/devices/smoke-01") as r:
        check("線上裝置不可移除（409）", r.status == 409)
    async with authed.delete(f"{BASE}/api/admin/devices/nope") as r:
        check("未知裝置移除回 404", r.status == 404)

    # 關掉 smoke-03 → 伺服器判離線 → 移除成功且廣播 device_removed
    SMOKE3_PROC.terminate()
    try:
        SMOKE3_PROC.wait(timeout=5)
    except subprocess.TimeoutExpired:
        SMOKE3_PROC.kill()
    await asyncio.sleep(1.0)          # 讓伺服器偵測 TCP 關閉 → 判離線
    removed_seen = False
    del_status = None
    async with authed.ws_connect(f"{BASE}/ws") as ws:
        await ws.receive(timeout=3.0)             # 先收掉 snapshot
        async with authed.delete(f"{BASE}/api/admin/devices/smoke-03") as r:
            del_status = r.status
        end = time.time() + 3.0
        while time.time() < end and not removed_seen:
            try:
                m = await ws.receive(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if m.type != aiohttp.WSMsgType.TEXT:
                break
            d = json.loads(m.data)
            if d.get("type") == "device_removed" and d.get("device") == "smoke-03":
                removed_seen = True
    check("離線裝置移除成功（200）", del_status == 200, f"status={del_status}")
    check("device_removed 有廣播", removed_seen)
    async with authed.ws_connect(f"{BASE}/ws") as ws:
        m = await ws.receive(timeout=3.0)
        snap = json.loads(m.data) if m.type == aiohttp.WSMsgType.TEXT else {}
        devs3 = {d["device"] for d in snap.get("devices", [])}
        check("移除後 snapshot 不含該裝置", "smoke-03" not in devs3,
              str(sorted(devs3)))

    # ── 登出 ────────────────────────────────────────────────────────
    async with authed.post(f"{BASE}/logout") as r:
        check("登出導回登入頁", r.url.path == "/login")
    async with authed.get(f"{BASE}/api/me") as r:
        check("登出後 session 失效（/api/me 401）", r.status == 401)
    await authed.close()


def prepare_certs_and_accounts(log_file) -> bool:
    """產生測試憑證與帳號（輸出寫入伺服器 log 檔）；失敗回傳 False"""
    r1 = subprocess.run(
        [PY, os.path.join("tools", "make_certs.py"), "--dir", CERT_DIR,
         "--host", "localhost", "--host", "127.0.0.1"],
        cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT)
    try:
        os.remove(ACCOUNTS)               # 每次重建，避免舊測試殘留
    except OSError:
        pass
    r2 = subprocess.run(
        [PY, os.path.join("tools", "make_user.py"), "--file", ACCOUNTS,
         "--user", ADMIN_USER, "--password", ADMIN_PASS, "--role", "admin"],
        cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT)
    r3 = subprocess.run(
        [PY, os.path.join("tools", "make_user.py"), "--file", ACCOUNTS,
         "--user", VIEWER_USER, "--password", VIEWER_PASS, "--role", "viewer"],
        cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT)
    return r1.returncode == 0 and r2.returncode == 0 and r3.returncode == 0


def main():
    global CLIENT_SSL, SMOKE3_PROC
    # 測試專用設定檔（放系統暫存目錄，不碰專案的 config.json）
    for old in ("sys_smoke-01.csv", "sys_smoke-02.csv", "sys_smoke-03.csv"):
        try:
            os.remove(os.path.join(SYS_LOG_DIR, old))
        except OSError:
            pass

    log_path = os.path.join(TMP, "respiramark_smoke_server.log")
    log_file = open(log_path, "w", encoding="utf-8")

    if not prepare_certs_and_accounts(log_file):
        log_file.close()
        print("FAIL: 測試憑證/帳號產生失敗（缺 cryptography？"
              "python -m pip install -r requirements.txt）")
        print(f"== 詳見 log: {log_path}")
        sys.exit(1)
    CLIENT_SSL = ssl.create_default_context(
        cafile=os.path.join(CERT_DIR, "ca.pem"))

    cfg_path = os.path.join(TMP, "respiramark_smoke_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"ingest_port": INGEST_PORT, "web_port": WEB_PORT,
                   "offline_timeout": 5.0, "ingest_token": TOKEN,
                   "sys_log_dir": SYS_LOG_DIR,
                   "tls_cert": os.path.join(CERT_DIR, "server.pem"),
                   "tls_key": os.path.join(CERT_DIR, "server.key"),
                   "auth_enabled": True, "accounts_file": ACCOUNTS,
                   "session_idle_minutes": 30.0}, f)

    procs = []
    try:
        procs.append(start([PY, "main.py", "--config", cfg_path], log_file))
        time.sleep(1.5)
        # smoke-02 帶 --alarms：驗證警報路徑（啟動即觸發一則）
        # smoke-03：移除離線裝置測試用（run_checks 會先關掉它再移除）
        ca_path = os.path.join(CERT_DIR, "ca.pem")
        for dev, patient, rr, extra in (
                ("smoke-01", "SMOKE001", "15", []),
                ("smoke-02", "SMOKE002", "22", ["--alarms"]),
                ("smoke-03", "SMOKE003", "18", [])):
            procs.append(start(
                [PY, os.path.join("tools", "fake_pi.py"),
                 "--device", dev, "--patient", patient,
                 "--rr", rr, "--port", str(INGEST_PORT),
                 "--host", "localhost", "--tls-ca", ca_path,
                 "--token", TOKEN] + extra, log_file))
        SMOKE3_PROC = procs[-1]
        asyncio.run(run_checks())
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        log_file.close()

    print()
    if FAILURES:
        print(f"== 冒煙測試失敗 {len(FAILURES)} 項: {FAILURES}")
        print(f"== 伺服器 log: {log_path}")
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            print(f.read()[-2000:])
        sys.exit(1)
    print("== 冒煙測試全部通過，可以 push ==")


if __name__ == "__main__":
    main()
