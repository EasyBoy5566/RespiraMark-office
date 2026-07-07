"""
呈現層 — HTTP 路由與 WebSocket 廣播
====================================
只負責「怎麼給人看」：提供前端靜態檔、WebSocket 即時推送、登入路由。
裝置狀態一律透過 domain 層 Hub 的 snapshot() 與廣播佇列取得，
不直接碰裝置連線與傳輸層。登入與 session 邏輯在 auth.py。
"""

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse

from aiohttp import web, WSMsgType

from monitor.audit import audit
from monitor.version import VERSION
from monitor.web.auth import auth_middleware
from monitor.web.security_headers import security_headers_middleware

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _actor(request) -> str:
    """管理操作審計用：登入者帳號（auth_middleware 掛的 request["user"]）；
    登入未啟用（開發模式）時沒有這個欄位，退回 "?" """
    return (request.get("user") or {}).get("username") or "?"


async def healthz(request):
    """GET /healthz → 免登入健康檢查（IMPROVEMENT_PLAN.md W-203）。
    刻意只回最精簡資訊：不含裝置清單/名稱、不含病人資訊，
    供資訊室監控系統定期探測用，不是給人看的頁面。"""
    hub = request.app["hub"]
    uptime_s = round(time.time() - request.app["start_time"], 1)
    return web.json_response({"ok": True, "version": VERSION,
                              "devices": hub.device_count(), "uptime_s": uptime_s})


async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def admin_page(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "admin.html"))


# ── 管理頁 API（/api/admin/*，middleware 已限 admin 角色）─────────────

async def admin_accounts(request):
    """GET /api/admin/accounts → 帳號唯讀清單（建立/刪除仍用 tools/make_user.py）"""
    mgr = request.app["authmgr"]
    users = mgr.authenticator.list_users() if mgr is not None else []
    audit("admin_view_accounts", actor=_actor(request))
    return web.json_response({"users": users})


async def admin_remove_device(request):
    """DELETE /api/admin/devices/{device} → 移除離線裝置（見 PROTOCOL.md）"""
    hub = request.app["hub"]
    device = request.match_info["device"]
    result = hub.remove_device(device)
    audit("admin_remove_device", actor=_actor(request), device=device, result=result)
    if result == "ok":
        return web.json_response({"removed": device})
    if result == "online":
        raise web.HTTPConflict(
            text='{"error": "裝置在線上，不可移除"}', content_type="application/json")
    raise web.HTTPNotFound(
        text='{"error": "未知裝置"}', content_type="application/json")


async def admin_syslog(request):
    """GET /api/admin/syslog/{device} → 下載該裝置的長期 sys CSV"""
    hub = request.app["hub"]
    device = request.match_info["device"]
    path = hub.sys_csv_path(device)
    if not path or not os.path.exists(path):
        raise web.HTTPNotFound(
            text='{"error": "尚無 CSV 紀錄（未啟用落地或還沒寫入資料）"}',
            content_type="application/json")
    audit("admin_download_syslog", actor=_actor(request), device=device)
    return web.FileResponse(path, headers={
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{os.path.basename(path)}"',
    })


# ── 登入未啟用（開發模式）時的替代 handler ──────────────────────────

async def _login_disabled(request):
    raise web.HTTPSeeOther("/")                  # 沒有登入功能 → 一律回儀表板


async def _me_disabled(request):
    return web.json_response({"auth": False, "username": None, "role": None})


async def sys_history(request):
    """GET /history/{device} → 該裝置 sys 近段歷史（趨勢圖展開時抓一次補齊）"""
    hub = request.app["hub"]
    device = request.match_info["device"]
    return web.json_response({"device": device, "samples": hub.sys_history(device)})


def _origin_allowed(request) -> bool:
    """Origin 存在且 host 與本站不同 → 擋（跨站 WebSocket 挾持）；
    不存在則放行（相容非瀏覽器客戶端，例如既有測試工具與未來可能的
    命令列查看端）——SameSite=Lax cookie 已是第一層防護，這是第二層。"""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    return urlparse(origin).netloc == request.host


async def ws_handler(request):
    if not _origin_allowed(request):
        raise web.HTTPForbidden(
            text='{"error": "Origin 不符"}', content_type="application/json")
    hub = request.app["hub"]
    q = hub.add_viewer()
    if q is None:
        raise web.HTTPServiceUnavailable(
            text='{"error": "觀看端數已達上限，請稍後再試"}',
            content_type="application/json")

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
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


def create_app(hub, authmgr=None, tls_enabled=False) -> web.Application:
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    # security_headers 排最外層：auth_middleware 的 401/403/redirect 多半是
    # raise HTTPException 而非 return，要包在外面才能連這些回應也補上標頭
    app = web.Application(middlewares=[security_headers_middleware, auth_middleware])
    app["hub"] = hub
    app["authmgr"] = authmgr                     # None = 登入未啟用（開發模式）
    app["tls_enabled"] = tls_enabled
    app["start_time"] = time.time()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/history/{device}", sys_history)
    app.router.add_get("/api/admin/accounts", admin_accounts)
    app.router.add_delete("/api/admin/devices/{device}", admin_remove_device)
    app.router.add_get("/api/admin/syslog/{device}", admin_syslog)
    if authmgr is not None:
        app.router.add_get("/login", authmgr.login_page)
        app.router.add_post("/login", authmgr.login_post)
        app.router.add_post("/logout", authmgr.logout)
        app.router.add_get("/api/me", authmgr.api_me)
    else:
        app.router.add_get("/login", _login_disabled)
        app.router.add_post("/logout", _login_disabled)
        app.router.add_get("/api/me", _me_disabled)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def start_web(hub, port: int, ssl_ctx=None, authmgr=None):
    """啟動網頁伺服器（ssl_ctx 非 None 時為 HTTPS/WSS），回傳 runner"""
    runner = web.AppRunner(create_app(hub, authmgr, tls_enabled=ssl_ctx is not None))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port, ssl_context=ssl_ctx).start()
    return runner
