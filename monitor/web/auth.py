"""
呈現層 — 登入驗證與 session 管理
==================================
保護「人看的那一側」：`/`、`/ws`、`/history/*`、`/api/me` 都要先登入。
（Pi 資料連入的驗證是 ingest_token + TLS，與本模組無關。）

設計：
- 密碼只存 PBKDF2-SHA256 雜湊（accounts.json，不進 git；tools/make_user.py 建立）；
  本應用**不提供改密碼功能**——密碼由 tools/make_user.py（本機模式）或院內
  HIS/AD（LDAP 模式，見 monitor/web/ldap_auth.py）管理，改了立刻生效
- session 存伺服器記憶體：cookie 只是隨機權杖，本身不含任何資訊；
  閒置逾時（sliding）由 SessionStore 判斷，0 = 不逾時（護理站看板用）
- 「驗證帳密」隔離成 Authenticator 介面：AuthManager 用依賴注入接收
  authenticator（LocalAuthenticator 或 LdapAuthenticator），實作同名方法
  authenticate(username, password) -> role | None，登入頁、session、
  middleware 全部不用管是哪一種
- 連續登入失敗 → 暫時鎖定該來源 IP（基本暴力破解防護）
- 純標準庫 + aiohttp（遵守 CLAUDE.md §2.3 相依規則；LDAP 模式額外用 ldap3，
  只有選用該模式時才需要安裝，見 requirements.txt 說明）
"""

import asyncio
import json
import logging
import os
import secrets
import time

from aiohttp import web

from monitor.audit import audit
from monitor.crypto import hash_password, verify_password

COOKIE_NAME = "rm_session"

# 不需登入即可存取的路徑（登入頁本身與靜態資源；資料端點一律受保護）
#
# /api/pair/ 是裝置配對用（PROTOCOL.md「裝置配對」）：Pi 上沒有人可以登入，
# 所以這兩個端點必須免登入。它們不會洩漏任何資料——申請只能得到一組隨機
# pair_id，要拿到 token 得由管理員在 /admin 核對 6 位確認碼後核可；查詢用的
# pair_id 是高熵隨機字串，猜不到的一律回 expired。核可端點在
# /api/admin/pair/*，走下面的 ADMIN_API_PREFIX 保護。
PUBLIC_PATHS = {"/login", "/healthz"}
PUBLIC_PREFIXES = ("/static/", "/api/pair/")

# 只有 admin 角色能進的路徑（管理頁與其 API，見 PROTOCOL.md「管理頁」）
ADMIN_PAGES = {"/admin"}
ADMIN_API_PREFIX = "/api/admin/"

# 未登入時導向登入頁（而非 401 JSON）的「頁面」路徑
PAGE_PATHS = {"/", "/admin"}

# 登入失敗鎖定：同一(IP,帳號)在視窗內失敗達上限 → 拒絕直到視窗滑出。
# 另外用同一 IP 對「任意帳號」的失敗總數設較高上限，擋住同一來源對
# 多個帳號亂猜的情境；只鎖單一(IP,帳號)則不會誤傷共用同一台電腦/
# 出口 IP 的其他使用者嘗試登入自己的帳號（F-08）。
FAIL_WINDOW = 600.0     # 秒
FAIL_MAX = 5            # 同一 (IP, 帳號) 上限
FAIL_MAX_IP = 20        # 同一 IP 對任意帳號的總上限


# 帳號不存在時也跑一次雜湊比對：讓「帳號存在與否」在回應時間上無差異
_DUMMY_HASH = hash_password("dummy-timing-equalizer")


class LocalAuthenticator:
    """本機帳號檔驗證（accounts.json）。

    LDAP/AD 模式見 monitor/web/ldap_auth.py 的 LdapAuthenticator，
    實作同名方法 authenticate(username, password) -> role 字串或 None，
    main.py 依 config.json 的 auth_backend 選用，其餘程式不動。
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
    """伺服器端 session（記憶體）；sliding 閒置逾時 + 選用絕對逾時 + 總量上限。

    絕對逾時（absolute_hours）與閒置逾時（idle）是兩件事：閒置逾時只要一直
    有人在用就永遠不會觸發（sliding），絕對逾時則是「登入後最多能撐多久」
    的硬上限，即使一直有操作也一樣會過期，逼迫重新輸入密碼。兩者皆
    0 = 不逾時（護理站看板帳號用）。
    """

    def __init__(self, idle_minutes: float = 30.0, absolute_hours: float = 0.0,
                 max_sessions: int = 200):
        self.idle = float(idle_minutes) * 60.0
        self.absolute = float(absolute_hours) * 3600.0
        self.max_sessions = max_sessions
        self._sessions = {}     # token -> {"username","role","last","created"}

    def create(self, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[token] = {"username": username, "role": role,
                                 "last": now, "created": now}
        if len(self._sessions) > self.max_sessions:
            self._evict_oldest()
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
        if self.absolute > 0 and now - s["created"] > self.absolute:
            del self._sessions[token]
            return None
        s["last"] = now
        return s

    def delete(self, token):
        self._sessions.pop(token, None)

    def _evict_oldest(self):
        """超過 max_sessions 時淘汰最久沒動作的一筆（長年運行防止無限增長，F-09）"""
        oldest = min(self._sessions, key=lambda t: self._sessions[t]["last"])
        del self._sessions[oldest]

    def sweep(self) -> int:
        """清掉所有已過期 session（idle 或 absolute）；回傳清掉的筆數。
        由 AuthManager.watchdog() 定期呼叫——get() 只在被存取時才會發現
        過期，長年沒人用舊 cookie 存取的殘留項目要靠這個主動清掉。"""
        now = time.time()
        expired = [t for t, s in self._sessions.items()
                  if (self.idle > 0 and now - s["last"] > self.idle)
                  or (self.absolute > 0 and now - s["created"] > self.absolute)]
        for t in expired:
            del self._sessions[t]
        return len(expired)


class LoginRateLimiter:
    """雙鍵計數：同一 (IP,帳號) 達 FAIL_MAX，或同一 IP 對任意帳號累計達
    FAIL_MAX_IP（FAIL_WINDOW 內）→ 暫時拒絕（見上方常數註解，F-08）。"""

    def __init__(self):
        self._by_pair = {}      # (ip,username) -> [失敗時間...]
        self._by_ip = {}        # ip -> [失敗時間...]

    @staticmethod
    def _recent(times: list, now: float) -> list:
        return [t for t in times if now - t < FAIL_WINDOW]

    def blocked(self, ip: str, username: str) -> bool:
        now = time.time()
        pair_recent = self._recent(self._by_pair.get((ip, username), []), now)
        ip_recent = self._recent(self._by_ip.get(ip, []), now)
        self._by_pair[(ip, username)] = pair_recent
        self._by_ip[ip] = ip_recent
        return len(pair_recent) >= FAIL_MAX or len(ip_recent) >= FAIL_MAX_IP

    def fail(self, ip: str, username: str):
        now = time.time()
        self._by_pair.setdefault((ip, username), []).append(now)
        self._by_ip.setdefault(ip, []).append(now)

    def clear(self, ip: str, username: str):
        self._by_pair.pop((ip, username), None)
        # 注意：不清 _by_ip——這是「同 IP 對多帳號亂猜」的總量防護，
        # 某一帳號登入成功不代表這個 IP 沒有在對其他帳號嘗試


SWEEP_INTERVAL = 300.0     # 秒，session 定期清掃間隔（F-09：長年運行防止殘留累積）


class AuthManager:
    """登入功能的組裝：驗證器 + session + 限流 + HTTP handler。

    authenticator 用依賴注入傳入（LocalAuthenticator 或 LdapAuthenticator，
    見 main.py 的 build_auth_manager 依 config.json 的 auth_backend 選用），
    本類別不關心是哪一種、只呼叫共同介面 authenticate()/has_users()/list_users()。
    """

    def __init__(self, authenticator, idle_minutes: float = 30.0,
                 secure_cookie: bool = False, absolute_hours: float = 0.0,
                 max_sessions: int = 200):
        self.log = logging.getLogger("auth")
        self.authenticator = authenticator
        self.sessions = SessionStore(idle_minutes, absolute_hours, max_sessions)
        self.limiter = LoginRateLimiter()
        self.secure_cookie = secure_cookie   # TLS 啟用時 cookie 加 Secure 旗標

    async def watchdog(self):
        """定期清掃過期 session（比照 hub.watchdog() 的既有模式）；
        main.py 用 asyncio.ensure_future 啟動，與 hub 的 watchdog 並存"""
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            n = self.sessions.sweep()
            if n:
                self.log.info(f"清掃過期 session {n} 筆")

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
        if self.limiter.blocked(ip, username):
            self.log.warning(f"登入嘗試被鎖定（{ip} 失敗次數過多）")
            raise web.HTTPSeeOther("/login?err=lock")
        role = self.authenticator.authenticate(username, password)
        if role is None:
            self.limiter.fail(ip, username)
            self.log.warning(f"登入失敗（{ip}）")     # 不記帳號名，避免洩漏嘗試內容
            audit("login_fail", ip=ip, username=username)
            raise web.HTTPSeeOther("/login?err=1")
        self.limiter.clear(ip, username)
        token = self.sessions.create(username, role)
        self.log.info(f"登入成功: {username}（{role}）")
        audit("login_ok", ip=ip, username=username, role=role)
        resp = web.HTTPSeeOther("/")
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax",
                        secure=self.secure_cookie, path="/")
        return resp

    async def logout(self, request):
        sess = self.session_from(request)
        if sess is not None:
            audit("logout", username=sess.get("username"))
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
