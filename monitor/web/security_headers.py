"""
呈現層 — HTTP 安全標頭 middleware
====================================
本專案前端零外部資源（無 CDN、無 inline script/style，見 CLAUDE.md §2.2/§5），
CSP 可以直接鎖到 'self'，成本低、效果好（對應 IMPROVEMENT_PLAN.md F-04/F-11）。
"""

from aiohttp import web

# connect-src 額外列 ws:/wss:：涵蓋較舊瀏覽器對 'self' 是否自動含 WebSocket
# scheme 的實作差異（CSP3 規範上 'self' 應已涵蓋同來源 WebSocket）
CSP = "default-src 'self'; connect-src 'self' ws: wss:"


def _apply_headers(resp, tls_enabled: bool):
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if tls_enabled:
        # 兩年 + includeSubDomains：本機服務只有這一個網域，無子網域疑慮
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    resp.headers["Server"] = "RespiraMark"    # 蓋掉 aiohttp 預設值，避免洩漏元件版本


@web.middleware
async def security_headers_middleware(request, handler):
    """login/權限檢查等大多回應是用 raise web.HTTPException（301/401/403…）
    而不是 return——這種例外會直接穿過本層往外拋，不會被視為正常回傳值，
    所以要另外攔截例外並在重新拋出前補上標頭，確保所有回應都有這些標頭。"""
    tls_enabled = bool(request.app.get("tls_enabled"))
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        _apply_headers(exc, tls_enabled)
        raise
    _apply_headers(resp, tls_enabled)
    return resp
