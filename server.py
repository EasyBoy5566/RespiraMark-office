"""
RespiraMark Office — 彙整伺服器
================================
- TCP :8765  接收各 Pi 的遙測資料（JSON Lines，見 PROTOCOL.md）
- HTTP :8080 提供儀表板網頁 + WebSocket /ws 即時廣播

啟動：python server.py（或雙擊 start_server.bat）
設定：config.json（不存在則用預設值，範本見 config.json.example）
"""

import asyncio
import json
import logging
import os
import socket

from aiohttp import web, WSMsgType

from telemetry_hub import TelemetryHub

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DEFAULTS = {
    "ingest_port": 8765,      # Pi 連入的 TCP port
    "web_port": 8080,         # 瀏覽器網頁 port
    "offline_timeout": 5.0,   # 秒，無資料判定裝置離線
}

MAX_LINE = 512 * 1024         # 單行訊息上限（防禦異常資料）


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logging.warning(f"config.json 讀取失敗，使用預設值: {e}")
    return cfg


# ── Pi 資料接收（TCP ingest）────────────────────────────────────────

async def handle_ingest(reader, writer, hub: TelemetryHub):
    log = logging.getLogger("ingest")
    peer = writer.get_extra_info("peername")
    device = None
    conn_seq = None
    try:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning(f"{peer} 訊息過長，斷線")
                break
            if not line:
                break                          # 對方關閉連線
            line = line.strip()
            if not line or len(line) > MAX_LINE:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning(f"{peer} JSON 解析失敗（忽略該行）")
                continue
            if device is None:
                if msg.get("type") != "hello":
                    log.warning(f"{peer} 第一則訊息不是 hello，斷線")
                    break
                device, conn_seq = hub.device_hello(msg)
            else:
                hub.device_message(device, conn_seq, msg)
    except (ConnectionResetError, OSError):
        pass
    finally:
        hub.device_disconnected(device, conn_seq)
        try:
            writer.close()
        except OSError:
            pass


# ── 瀏覽器端（HTTP + WebSocket）────────────────────────────────────

async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def ws_handler(request):
    hub: TelemetryHub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    q = hub.add_viewer()
    sender = asyncio.create_task(_ws_sender(ws, q))
    try:
        await ws.send_str(json.dumps(hub.snapshot(), ensure_ascii=False))
        async for msg in ws:                   # 瀏覽器不會主動送資料，等 close 即可
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        hub.remove_viewer(q)
        sender.cancel()
    return ws


async def _ws_sender(ws, q: asyncio.Queue):
    try:
        while True:
            line = await q.get()
            await ws.send_str(line)
    except (asyncio.CancelledError, ConnectionResetError):
        pass


# ── 啟動 ────────────────────────────────────────────────────────────

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


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = load_config()
    hub = TelemetryHub(offline_timeout=float(cfg["offline_timeout"]))

    await asyncio.start_server(
        lambda r, w: handle_ingest(r, w, hub),
        "0.0.0.0", int(cfg["ingest_port"]), limit=1024 * 1024,
    )

    app = web.Application()
    app["hub"] = hub
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC_DIR)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(cfg["web_port"])).start()

    asyncio.ensure_future(hub.watchdog())

    ip = lan_ip()
    print()
    print("=" * 62)
    print("  RespiraMark Office 彙整伺服器已啟動")
    print(f"  儀表板（本機）:   http://localhost:{cfg['web_port']}")
    print(f"  儀表板（區網）:   http://{ip}:{cfg['web_port']}")
    print(f"  Pi 端 telemetry.json 的 server_host 請填: {ip}")
    print(f"  Pi 資料接收 port: {cfg['ingest_port']}")
    print("  （若其他裝置連不上，請確認 Windows 防火牆已放行以上兩個 port）")
    print("=" * 62)
    print()

    await asyncio.Event().wait()               # 永久運行，Ctrl+C 結束


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n伺服器已停止")
