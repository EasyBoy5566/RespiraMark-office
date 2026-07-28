"""
呈現層 — 裝置配對 HTTP API
============================
把 domain/pairing.py 的 PairingService 包成 HTTP 端點（契約見 PROTOCOL.md
「裝置配對」一節）。本模組不含任何配對規則——上限、逾時、換發判斷、
devices.json 寫入全在 domain 層，這裡只做請求解析、審計與錯誤碼對應。

權限（monitor/web/auth.py 的 auth_middleware 依路徑前綴判斷）：
- `/api/pair/*`   免登入——Pi 上沒有人可以登入，身分靠管理員核對 6 位確認碼
- `/api/admin/pair/*`  自動落在 ADMIN_API_PREFIX，只有 admin 角色能呼叫

⚠️ 審計一律不得帶 token（monitor/audit.py 的 _FORBIDDEN_KEYS 會直接 raise）。
"""

import asyncio
import json

from aiohttp import web

from monitor.audit import audit


def _err(exc_class, message: str):
    """沿用本專案的錯誤回應慣例：手寫 JSON 字串 + application/json"""
    return exc_class(text=json.dumps({"error": message}, ensure_ascii=False),
                     content_type="application/json")


def _actor(request) -> str:
    return (request.get("user") or {}).get("username") or "?"


def _client_ip(request) -> str:
    """來源 IP：僅用於「同一 IP 只留一筆申請」與審計。刻意不看
    X-Forwarded-For——本系統直接對院內網提供服務，沒有反向代理，
    信任該標頭只會讓人隨手偽造來繞過同 IP 限制。"""
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else "?"


# ── Pi 端（免登入）────────────────────────────────────────────────────

async def pair_request(request):
    """POST /api/pair/request → 送出配對申請，回 pair_id 與 6 位確認碼"""
    svc = request.app["pairing"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except (ValueError, json.JSONDecodeError):
        raise _err(web.HTTPBadRequest, "JSON 格式錯誤")

    ip = _client_ip(request)
    result, data = svc.request(str(body.get("device_id") or ""),
                               str(body.get("note") or ""), ip)
    if result == "bad_id":
        raise _err(web.HTTPBadRequest, "device_id 格式不正確")
    if result == "limit":
        audit("pair_rejected", ip=ip, reason="limit")
        raise _err(web.HTTPTooManyRequests, "配對請求已達上限，請稍後再試")

    for device in data.pop("replaced"):
        audit("pair_replaced", device=device, ip=ip)
    audit("pair_requested", device=data["device_id"], ip=ip, renew=data["renew"])
    return web.json_response({"pair_id": data["pair_id"], "code": data["code"],
                              "expires_in": data["expires_in"],
                              "poll_interval": data["poll_interval"]})


async def pair_poll(request):
    """GET /api/pair/poll/{pair_id} → 查詢結果；approved 時一次性領走 token"""
    svc = request.app["pairing"]
    body, claimed = svc.poll(request.match_info["pair_id"])
    if claimed:
        audit("pair_claimed", device=claimed, ip=_client_ip(request))
    return web.json_response(body)


# ── 管理端（僅 admin）────────────────────────────────────────────────

async def admin_pair_pending(request):
    """GET /api/admin/pair/pending → 待核可清單。

    管理頁定期輪詢，刻意不寫審計（幾秒一次會把 audit.log 灌爆，
    真正該留下紀錄的是 approve/deny 這種會改變狀態的動作）。"""
    return web.json_response({"pending": request.app["pairing"].list_pending()})


async def admin_pair_approve(request):
    """POST /api/admin/pair/{pair_id}/approve → 核發 token 並寫入 devices.json"""
    svc = request.app["pairing"]
    pair_id = request.match_info["pair_id"]
    # approve 內含 PBKDF2（600k 迭代，約數百毫秒）與檔案 I/O，
    # 直接在 event loop 執行會讓所有裝置的波形轉發跟著卡住
    result, data = await asyncio.get_running_loop().run_in_executor(
        None, svc.approve, pair_id)

    if result == "not_found":
        raise _err(web.HTTPNotFound, "配對不存在或已過期")
    if result == "already":
        raise _err(web.HTTPConflict, "此配對已處理")
    if result == "write_failed":
        audit("pair_approved", actor=_actor(request), result="write_failed")
        raise _err(web.HTTPInternalServerError, "devices.json 寫入失敗")

    audit("pair_approved", actor=_actor(request), device=data["device_id"],
          renew=data["renew"], ip=_client_ip(request))
    return web.json_response({"approved": data})


async def admin_pair_deny(request):
    """POST /api/admin/pair/{pair_id}/deny → 拒絕配對（不動 devices.json）"""
    svc = request.app["pairing"]
    result, data = svc.deny(request.match_info["pair_id"])
    if result == "not_found":
        raise _err(web.HTTPNotFound, "配對不存在或已過期")
    if result == "already":
        raise _err(web.HTTPConflict, "此配對已處理")
    audit("pair_denied", actor=_actor(request), device=data["device_id"])
    return web.json_response({"denied": data})


def add_routes(router):
    """由 routes.py 的 create_app 呼叫（只在配對啟用時）"""
    router.add_post("/api/pair/request", pair_request)
    router.add_get("/api/pair/poll/{pair_id}", pair_poll)
    router.add_get("/api/admin/pair/pending", admin_pair_pending)
    router.add_post("/api/admin/pair/{pair_id}/approve", admin_pair_approve)
    router.add_post("/api/admin/pair/{pair_id}/deny", admin_pair_deny)
