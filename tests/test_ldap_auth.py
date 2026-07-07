# -*- coding: utf-8 -*-
"""
test_ldap_auth — LdapAuthenticator 單元測試（IMPROVEMENT_PLAN.md W-307）
====================================================================================
還沒有真的院內 LDAP/AD 環境可以測，用 ldap3 內建的 MOCK_SYNC 策略在記憶體
模擬一個小型 LDAP 目錄，驗證 bind 成功/失敗、帳號不在本機白名單等邏輯正確。

關鍵細節：MOCK_SYNC 的假資料是掛在每個 Connection 的 strategy 上，不是掛在
Server 物件上——而 LdapAuthenticator 每次 authenticate() 都會建立全新的
Connection（見 ldap_auth.py 設計說明：不維持常駐連線池）。所以測試用
monkeypatch 把 ldap_auth.ldap3.Connection 換成一個工廠函式，每次呼叫都重新
建立 MOCK_SYNC 連線並灌入同一份固定測試資料，維持跟正式程式碼相同的
「每次都是新連線」行為。

用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import ldap3

from monitor.web import ldap_auth

MOCK_DIT = {
    "cn=alice,dc=example,dc=org": {"userPassword": "CorrectPass1", "sn": "Alice"},
    "cn=bob,dc=example,dc=org": {"userPassword": "BobPass2", "sn": "Bob"},
}


_RealConnection = ldap3.Connection   # patch 目標是 ldap_auth.ldap3.Connection，即同一個模組物件；
                                      # 這裡先留住原始類別，避免下面呼叫到被 patch 後的自己而無限遞迴


def _mock_connection_factory(server, user=None, password=None, **kwargs):
    """取代 ldap3.Connection：换成 MOCK_SYNC 策略，並灌入固定測試用 DIT。"""
    conn = _RealConnection(server, user=user, password=password,
                            client_strategy=ldap3.MOCK_SYNC)
    for dn, attrs in MOCK_DIT.items():
        conn.strategy.add_entry(dn, dict(attrs))
    return conn


def _write_roles(path, users):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f)


class LdapAuthenticatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.roles_path = os.path.join(self.tmp.name, "accounts.json")
        _write_roles(self.roles_path, [
            {"username": "alice", "role": "admin"},
            {"username": "bob", "role": "viewer"},
            # carol 通過 LDAP bind 但不在這份白名單裡也不該有帳號可用
        ])
        self.auth = ldap_auth.LdapAuthenticator(
            server_uri="mock-server",
            bind_template="cn={username},dc=example,dc=org",
            roles_path=self.roles_path)
        self.patcher = mock.patch(
            "monitor.web.ldap_auth.ldap3.Connection", side_effect=_mock_connection_factory)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_correct_password_and_whitelisted_returns_role(self):
        self.assertEqual(self.auth.authenticate("alice", "CorrectPass1"), "admin")
        self.assertEqual(self.auth.authenticate("bob", "BobPass2"), "viewer")

    def test_wrong_password_returns_none(self):
        self.assertIsNone(self.auth.authenticate("alice", "wrong-password"))

    def test_unknown_ldap_username_returns_none(self):
        """LDAP 目錄裡根本沒有這個帳號（bind 一定失敗），即使剛好也不在白名單裡"""
        self.assertIsNone(self.auth.authenticate("nosuchuser", "whatever"))

    def test_correct_ldap_bind_but_not_in_local_whitelist_returns_none(self):
        """比照 W-307 設計：LDAP 只證明帳密正確，用不用得了這套系統看本機白名單，
        不是看 AD 群組——carol 在別的假設情境下可能是合法院內帳號，但沒登記在
        accounts.json，這裡就該被拒絕"""
        MOCK_DIT["cn=carol,dc=example,dc=org"] = {"userPassword": "CarolPass3"}
        try:
            self.assertIsNone(self.auth.authenticate("carol", "CarolPass3"))
        finally:
            del MOCK_DIT["cn=carol,dc=example,dc=org"]

    def test_empty_username_or_password_returns_none_without_raising(self):
        self.assertIsNone(self.auth.authenticate("", "whatever"))
        self.assertIsNone(self.auth.authenticate("alice", ""))
        self.assertIsNone(self.auth.authenticate("", ""))

    def test_has_users_and_list_users_reflect_roles_file(self):
        self.assertTrue(self.auth.has_users())
        users = self.auth.list_users()
        self.assertEqual({u["username"] for u in users}, {"alice", "bob"})
        # password 欄位（若白名單檔裡帶了）不該出現在 list_users() 回傳內容
        for u in users:
            self.assertNotIn("password", u)

    def test_missing_roles_file_means_no_users(self):
        auth = ldap_auth.LdapAuthenticator(
            server_uri="mock-server",
            bind_template="cn={username},dc=example,dc=org",
            roles_path=os.path.join(self.tmp.name, "does-not-exist.json"))
        self.assertFalse(auth.has_users())
        self.assertIsNone(auth.authenticate("alice", "CorrectPass1"))


class LdapAuthenticatorMissingDependencyTest(unittest.TestCase):
    def test_raises_when_ldap3_not_installed(self):
        with mock.patch("monitor.web.ldap_auth.ldap3", None):
            with self.assertRaises(RuntimeError):
                ldap_auth.LdapAuthenticator(
                    server_uri="mock-server",
                    bind_template="cn={username},dc=example,dc=org",
                    roles_path="unused.json")


if __name__ == "__main__":
    unittest.main()
