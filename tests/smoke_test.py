# -*- coding: utf-8 -*-
"""
smoke_test — push 前必跑的端對端冒煙測試（開發 SOP，見 CLAUDE.md）
====================================================================
一鍵執行：啟動伺服器（測試專用 port）→ 跑兩台 fake_pi → 模擬瀏覽器
連 /ws 驗證 snapshot 與即時廣播 → 全部關閉。

用法：
    python tests/smoke_test.py

通過 → exit code 0；任一項失敗 → exit code 1 並印出伺服器 log 供除錯。
使用 18080/18765 測試 port，不影響正在運行的正式伺服器。
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
WEB_PORT = 18080
INGEST_PORT = 18765
TOKEN = "SMOKETOKEN"     # 測試用 ingest_token，驗證 token 驗證路徑

sys.path.insert(0, BASE_DIR)
try:
    import aiohttp
except ImportError:
    print("FAIL: 未安裝 aiohttp（python -m pip install -r requirements.txt）")
    sys.exit(1)

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def start(cmd, log_file):
    return subprocess.Popen(cmd, cwd=BASE_DIR, stdout=log_file,
                            stderr=subprocess.STDOUT)


async def wait_web_up(timeout=10.0):
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as s:
        while time.time() < deadline:
            try:
                async with s.get(f"http://localhost:{WEB_PORT}/") as r:
                    if r.status == 200:
                        return True
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.3)
    return False


async def collect_ws(seconds=4.0):
    msgs = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"http://localhost:{WEB_PORT}/ws") as ws:
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


async def check_bad_token():
    """帶錯誤 token 的 hello 應被伺服器立即斷線（回傳 True = 有被斷線）"""
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", INGEST_PORT)
    except OSError:
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


async def run_checks():
    check("網頁伺服器啟動（HTTP 200）", await wait_web_up())

    # 靜態資源
    async with aiohttp.ClientSession() as s:
        for path in ("/static/app.js", "/static/style.css"):
            async with s.get(f"http://localhost:{WEB_PORT}{path}") as r:
                check(f"靜態資源 {path}", r.status == 200)

    # token 驗證：錯誤 token 必須被拒（在收 snapshot 前做，順便驗證它不會進 snapshot）
    check("錯誤 token 被伺服器斷線", await check_bad_token())

    await asyncio.sleep(2.0)          # 讓 fake_pi 連上並開始送資料
    msgs = await collect_ws(4.0)

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
        d2 = devs.get("smoke-02", {})
        alarms = (d2.get("alarm") or {}).get("alarms") or []
        check("snapshot 含 alarm 快照（--alarms 裝置）",
              len(alarms) >= 1 and "text" in alarms[0], str(alarms)[:80])

    waves = [m for m in msgs if m["type"] == "wave"]
    src = {m["device"] for m in waves}
    check("兩台裝置的波形皆有廣播", src == {"smoke-01", "smoke-02"}, str(sorted(src)))
    if waves:
        w = waves[0]
        n = len(w["p"])
        check("wave 欄位齊全且等長",
              n > 0 and len(w["f"]) == n and len(w["v"]) == n and "trig" in w)
        total = sum(len(m["p"]) for m in waves if m["device"] == "smoke-01")
        check("波形速率合理（80~120Hz）", 80 <= total / 4.0 <= 120,
              f"{total / 4.0:.0f} 樣本/秒")


def main():
    # 測試專用設定檔（放系統暫存目錄，不碰專案的 config.json）
    cfg_path = os.path.join(tempfile.gettempdir(), "respiramark_smoke_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"ingest_port": INGEST_PORT, "web_port": WEB_PORT,
                   "offline_timeout": 5.0, "ingest_token": TOKEN}, f)

    log_path = os.path.join(tempfile.gettempdir(), "respiramark_smoke_server.log")
    log_file = open(log_path, "w", encoding="utf-8")
    procs = []
    try:
        procs.append(start([PY, "main.py", "--config", cfg_path], log_file))
        time.sleep(1.5)
        # smoke-02 帶 --alarms：驗證警報路徑（啟動即觸發一則）
        for dev, patient, rr, extra in (
                ("smoke-01", "SMOKE001", "15", []),
                ("smoke-02", "SMOKE002", "22", ["--alarms"])):
            procs.append(start(
                [PY, os.path.join("tools", "fake_pi.py"),
                 "--device", dev, "--patient", patient,
                 "--rr", rr, "--port", str(INGEST_PORT),
                 "--token", TOKEN] + extra, log_file))
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
