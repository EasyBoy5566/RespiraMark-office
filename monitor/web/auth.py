"""
呈現層 — 登入驗證與 session 管理
==================================
保護「人看的那一側」：`/`、`/ws`、`/history/*`、`/api/me` 都要先登入。
（Pi 資料連入的驗證是 ingest_token + TLS，與本模組無關。）

設計：
- 密碼只存 PBKDF2-SHA256 雜湊（accounts.json，不進 git；tools/make_user.py 建立）
- session 存伺服器記憶體：cookie 只是隨機權杖，本身不含任何資訊；
  閒置逾時（sliding）由 SessionStore 判斷，0 = 不逾時（護理站看板用）
- 「驗證帳密」隔離成 Authenticator 介面：日後接醫院 AD/LDAP 時，
  只需新增一個實作（authenticate(username, password) -> role | None），
  登入頁、session、middleware 全部不用改
- 連續登入失敗 → 暫時鎖定該來源 IP（基本暴力破解防護）
- 純標準庫 + aiohttp（遵守 CLAUDE.md §2.3 相依規則）
"""

import json
import logging
import os
import secrets
import time

from aiohttp import web

from monitor.crypto import hash_password, verify_password  # noqa: F401 (對外沿用既有匯入路徑)

COOKIE_NAME = "rm_session"

# 不需登入即可存取的路徑（登入頁本身與靜態資源；資料端點一律受保護）
PUBLIC_PATHS = {"/login"}
PUBLIC_PREFIXES = ("/static/",)

# 只有 admin 角色能進的路徑（管理頁與其 API，見 PROTOCOL.md「管理頁」）
ADMIN_PAGES = {"/admin"}
ADMIN_API_PREFIX = "/api/admin/"

# 未登入時導向登入頁（而非 401 JSON）的「頁面」路徑
PAGE_PATHS = {"/", "/admin"}

# 登入失敗鎖定：同一 IP 在視窗內失敗達上限 → 拒絕直到視窗滑出
FAIL_WINDOW = 600.0     # 秒
FAIL_MAX = 5


# 帳號不存在時也跑一次雜湊比對：讓「帳號存在與否」在回應時間上無差異
_DUMMY_HASH = hash_password("dummy-timing-equalizer")


class LocalAuthenticator:
    """本機帳號檔驗證（accounts.json）。

    LDAP/AD 接口：日後新增 LdapAuthenticator，實作同名方法
    authenticate(username, password) -> role 字串或 None，
    在 main.py 依設定選用即可，其餘程式不動。
    """

    def __init__(self, accounts_path: str):
        self.path = accounts_path
        self.log = logging.getLogger("auth")

    def _load_users(self) -> list:
        """每次登入時重讀：make_user.py 改動後不需重啟伺服器"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            users = data.get("users")
            return users if isinstance(users, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self.log.warning(f"帳號檔讀取失敗: {e}")
            return []

    def has_users(self) -> bool:
        return bool(self._load_users())

    def list_users(self) -> list:
        """帳號唯讀清單（管理頁用）：只回帳號與角色，絕不回密碼雜湊"""
        return [{"username": u.get("username") or "?",
                 "role": u.get("role") or "viewer"}
                for u in self._load_users()]

    def authenticate(self, username: str, password: str):
        """驗證成功回傳角色字串（viewer/admin），失敗回傳 None"""
        for u in self._load_users():
            if u.get("username") == username:
                if verify_password(password, u.get("password", "")):
                    return u.get("role") or "viewer"
                return None
        verify_password(password, _DUMMY_HASH)   # 時間均衡（見上）
        return None


class SessionStore:
    """伺服器端 session（記憶體）；sliding 閒置逾時"""

    def __init__(self, idle_minutes: float = 30.0):
        self.idle = float(idle_minutes) * 60.0
        self._sessions = {}     # token -> {"username","role","last"}

    def create(self, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {"username": username, "role": role,
                                 "last": time.time()}
        return token

    def get(self, token):
        """有效回傳 session dict（並展延閒置計時），無效/逾時回傳 None"""
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return None
        now = time.time()
        if self.idle > 0 and now - s["last"] > self.idle:
            del self._sessions[token]
            return None
        s["last"] = now
        return s

    def delete(self, token):
        self._sessions.pop(token, None)


class LoginRateLimiter:
    """同一 IP 連續失敗達 FAIL_MAX 次（FAIL_WINDOW 內）→ 暫時拒絕"""

    def __init__(self):
        self._fails = {}        # ip -> [失敗時間...]

    def blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
        self._fails[ip] = recent
        return len(recent) >= FAIL_MAX

    def fail(self, ip: str):
        self._fails.setdefault(ip, []).append(time.time())

    def clear(self, ip: str):
        self._fails.pop(ip, None)


class AuthManager:
    """登入功能的組裝：驗證器 + session + 限流 + HTTP handler"""

    def __init__(self, accounts_path: str, idle_minutes: float = 30.0,
                 secure_cookie: bool = False):
        self.log = logging.getLogger("auth")
        self.authenticator = LocalAuthenticator(accounts_path)
        self.sessions = SessionStore(idle_minutes)
        self.limiter = LoginRateLimiter()
        self.secure_cookie = secure_cookie   # TLS 啟用時 cookie 加 Secure 旗標

    def has_users(self) -> bool:
        return self.authenticator.has_users()

    def session_from(self, request):
        return self.sessions.get(request.cookies.get(COOKIE_NAME))

    # ── HTTP handlers ───────────────────────────────────────────────

    async def login_page(self, request):
        if self.session_from(request) is not None:
            raise web.HTTPSeeOther("/")          # 已登入 → 直接進儀表板
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        return web.FileResponse(os.path.join(static_dir, "login.html"))

    async def login_post(self, request):
        form = await request.post()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        ip = request.remote or "?"
        if self.limiter.blocked(ip):
            self.log.warning(f"登入嘗試被鎖定（{ip} 失敗次數過多）")
            raise web.HTTPSeeOther("/login?err=lock")
        role = self.authenticator.authenticate(username, password)
        if role is None:
            self.limiter.fail(ip)
            self.log.warning(f"登入失敗（{ip}）")     # 不記帳號名，避免洩漏嘗試內容
            raise web.HTTPSeeOther("/login?err=1")
        self.limiter.clear(ip)
        token = self.sessions.create(username, role)
        self.log.info(f"登入成功: {username}（{role}）")
        resp = web.HTTPSeeOther("/")
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax",
                        secure=self.secure_cookie, path="/")
        return resp

    async def logout(self, request):
        self.sessions.delete(request.cookies.get(COOKIE_NAME))
        resp = web.HTTPSeeOther("/login")
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    async def api_me(self, request):
        sess = request.get("user") or {}
        return web.json_response({"auth": True,
                                  "username": sess.get("username"),
                                  "role": sess.get("role")})


@web.middleware
async def auth_middleware(request, handler):
    """未登入 → 頁面導向登入頁、資料端點回 401；登入資訊掛在 request["user"]。
    /admin 與 /api/admin/* 另外要求 admin 角色（viewer 頁面導回 /、API 回 403）。"""
    mgr = request.app.get("authmgr")
    if mgr is None:                              # 登入未啟用（開發模式）
        return await handler(request)
    path = request.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await handler(request)
    sess = mgr.session_from(request)
    if sess is None:
        if path in PAGE_PATHS:
            raise web.HTTPSeeOther("/login")
        raise web.HTTPUnauthorized(
            text='{"error": "未登入"}', content_type="application/json")
    if (path in ADMIN_PAGES or path.startswith(ADMIN_API_PREFIX)) \
            and sess.get("role") != "admin":
        if path in PAGE_PATHS:
            raise web.HTTPSeeOther("/")          # viewer 誤入管理頁 → 回儀表板
        raise web.HTTPForbidden(
            text='{"error": "需要管理員權限"}', content_type="application/json")
    request["user"] = sess
    return await handler(request)
