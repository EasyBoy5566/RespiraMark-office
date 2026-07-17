"""
呈現層 — LDAP/AD 帳號驗證（IMPROVEMENT_PLAN.md W-307）
==========================================================
比照院內既有系統 MAYA(RCS) 的作法：登入時把帳密現場交給醫院的集中帳號
目錄（LDAP / Active Directory）做一次 bind，bind 成功=密碼正確。本機
完全不存密碼、不提供改密碼功能——密碼從醫院 HIS/AD 端改，這裡立刻生效。

角色（viewer/admin）不是 LDAP 決定的：LDAP 只回答「這組帳密對不對」，
「這個人能不能用這套呼吸器監測系統、給什麼角色」由本機一份小名單決定
（沿用 accounts.json 的 {"users":[{"username":...,"role":...}]} 格式，
password 欄位在這個模式下不會被讀取，留著也無妨）——不用 AD 群組的原因
是那是醫院整體 IT 治理範圍，這裡不該也無法自行決定「哪個 AD 群組=可以用
這套系統」。

實作方式：每次登入呼叫 authenticate() 時才建立連線、bind、關閉，不維持
常駐連線池——登入頻率低（護理站一天可能就那幾次），簡單可靠優先於效能。

伺服器憑證驗證（ldap_ca，2026-07-17 補強）：use_ssl 時若有提供院內 CA 根
憑證檔（config.json 的 ldap_ca），ldaps 連線會以該 CA 驗證 AD 伺服器憑證
（CERT_REQUIRED），防止院內網有人冒充 AD 收集帳密；未提供時維持「只加密、
不驗證」並在啟動時記警告——正式環境必須設定 ldap_ca。

已知限制（等資訊室提供實際環境資訊後可能要調整）：
- 目前只支援「直接 bind」模式（帳號套進 bind_template 直接當 DN 使用，
  例如 UPN 格式 "{username}@csh.org.tw"）。若院方環境需要「先用服務帳號
  查詢使用者實際 DN 再 bind」（search-then-bind），需要另外擴充。
- LDAP 連不上時一律視為驗證失敗（fail closed），不會退回本機密碼比對
  ——避免「已在 HIS 停用的帳號」在本系統還能用舊密碼登入的資安漏洞。
"""

import json
import logging
import ssl

try:
    import ldap3
    from ldap3.core.exceptions import LDAPExceptionError
except ImportError:
    ldap3 = None
    LDAPExceptionError = Exception


class LdapAuthenticator:
    """LDAP/AD 驗證器：authenticate()/has_users()/list_users() 介面與
    LocalAuthenticator 相同，AuthManager 不需要知道兩者的差異。"""

    def __init__(self, server_uri: str, bind_template: str, roles_path: str,
                 use_ssl: bool = True, timeout: float = 5.0,
                 ca_certs_file: str = ""):
        if ldap3 is None:
            raise RuntimeError(
                "auth_backend 設為 ldap 但未安裝 ldap3 套件："
                "python -m pip install -r requirements.txt")
        self.server_uri = server_uri
        self.bind_template = bind_template   # 例如 "{username}@csh.org.tw"
        self.roles_path = roles_path
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.ca_certs_file = ca_certs_file   # 院內 CA 根憑證檔；空 = 只加密不驗證
        self.log = logging.getLogger("auth")
        if self.use_ssl and not self.ca_certs_file:
            self.log.warning(
                "ldap_ca 未設定：ldaps 連線只加密、不驗證 AD 伺服器憑證，"
                "正式環境請設定 ldap_ca（院內 CA 根憑證檔）")

    # ── 角色名單（本機小檔案，沿用 accounts.json 格式）───────────────

    def _load_roles(self) -> list:
        """每次登入時重讀：改角色名單不需重啟伺服器（同 LocalAuthenticator 模式）"""
        try:
            with open(self.roles_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            users = data.get("users")
            return users if isinstance(users, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self.log.warning(f"角色名單檔讀取失敗: {e}")
            return []

    def has_users(self) -> bool:
        return bool(self._load_roles())

    def list_users(self) -> list:
        """帳號唯讀清單（管理頁用）：只回帳號與角色（這個模式下本來就沒有密碼雜湊）"""
        return [{"username": u.get("username") or "?",
                 "role": u.get("role") or "viewer"}
                for u in self._load_roles()]

    def _role_for(self, username: str):
        for u in self._load_roles():
            if u.get("username") == username:
                return u.get("role") or "viewer"
        return None

    # ── LDAP bind ────────────────────────────────────────────────────

    def _bind_ok(self, username: str, password: str) -> bool:
        bind_dn = self.bind_template.format(username=username)
        try:
            # 有院內 CA 檔 → 以 CERT_REQUIRED 驗證 AD 伺服器憑證；CA 檔不存在等
            # 設定錯誤 ldap3.Tls 會直接拋例外 → 一律視為驗證失敗（fail closed）
            tls = None
            if self.use_ssl and self.ca_certs_file:
                tls = ldap3.Tls(validate=ssl.CERT_REQUIRED,
                                ca_certs_file=self.ca_certs_file)
            server = ldap3.Server(self.server_uri, use_ssl=self.use_ssl,
                                  connect_timeout=self.timeout, tls=tls)
            conn = ldap3.Connection(server, user=bind_dn, password=password,
                                    receive_timeout=self.timeout)
        except LDAPExceptionError as e:
            self.log.warning(f"LDAP 連線設定錯誤: {type(e).__name__}")
            return False
        try:
            return bool(conn.bind())
        except LDAPExceptionError as e:
            self.log.warning(f"LDAP 連線或驗證發生例外: {type(e).__name__}")
            return False
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    def authenticate(self, username: str, password: str):
        """驗證成功回傳角色字串（viewer/admin），失敗回傳 None。

        白名單檢查與 LDAP bind 兩者都做、不因白名單先未命中就提早回傳
        ——避免用回應時間差洩漏「這個帳號是否在本系統的名單內」。
        """
        if not username or not password:
            return None
        role = self._role_for(username)
        bind_ok = self._bind_ok(username, password)
        if bind_ok and role is not None:
            return role
        return None
