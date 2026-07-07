# -*- coding: utf-8 -*-
"""
test_crypto — monitor/crypto.py（PBKDF2 密碼/token 雜湊）單元測試
====================================================================
鎖住迭代數升級（IMPROVEMENT_PLAN.md F-10）最容易弄壞的事：舊雜湊格式
自帶迭代數，verify_password 必須用「字串內的值」而非目前的模組常數
比對，否則升級 PBKDF2_ITERATIONS 會讓所有舊帳號/舊裝置 token 一夜失效。

用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.crypto import PBKDF2_ITERATIONS, hash_password, verify_password

# 固定測試向量：200_000 次迭代（升級前的舊值）雜湊，密碼 "test-password-123"
OLD_HASH_200K = ("pbkdf2_sha256$200000$a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
                 "$e590ba872fd56e12200218b70bec2ea43a75aab1ee6260454a2fb082522afe06")


class CryptoTest(unittest.TestCase):
    def test_new_hash_roundtrip(self):
        h = hash_password("SomePassw0rd!")
        self.assertTrue(verify_password("SomePassw0rd!", h))
        self.assertFalse(verify_password("wrong", h))

    def test_new_hash_uses_current_iteration_count(self):
        h = hash_password("SomePassw0rd!")
        self.assertEqual(int(h.split("$")[1]), PBKDF2_ITERATIONS)

    def test_old_200k_hash_still_verifies_after_upgrade(self):
        """升級迭代數後，舊雜湊仍可驗證——不會讓既有帳號/裝置一夜失效"""
        self.assertTrue(verify_password("test-password-123", OLD_HASH_200K))
        self.assertFalse(verify_password("wrong-password", OLD_HASH_200K))

    def test_malformed_hash_rejected_not_crash(self):
        self.assertFalse(verify_password("anything", "not-a-valid-hash"))
        self.assertFalse(verify_password("anything", ""))


if __name__ == "__main__":
    unittest.main()
