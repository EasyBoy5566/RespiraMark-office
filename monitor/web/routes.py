"""
呈現層 — HTTP 路由與 WebSocket 廣播
====================================
只負責「怎麼給人看」：提供前端靜態檔、WebSocket 即時推送。
裝置狀態一律透過 domain 層 Hub 的 snapshot() 與廣播佇列取得，
不直接碰裝置連線與傳輸層。
"""

import asyncio
import json
import logging
import os

from aiohttp import web, WSMsgType

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def ws_handler(request):
    hub = request.app["hub"]
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


def create_app(hub) -> web.Application:
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    app = web.Application()
    app["hub"] = hub
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def start_web(hub, port: int):
    """啟動網頁伺服器，回傳 runner（保留引用避免被回收）"""
    runner = web.AppRunner(create_app(hub))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
