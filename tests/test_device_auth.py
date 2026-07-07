# -*- coding: utf-8 -*-
"""
test_device_auth — DeviceRegistry（傳輸層裝置權杖）單元測試
==============================================================
鎖住每台 Pi 獨立 token（IMPROVEMENT_PLAN.md F-01）最容易弄壞的邏輯：
存在性判斷（決定 ingest.py 要不要退回舊版單一 token 模式）、驗證成功/
失敗、停用裝置、查無裝置、清單絕不含 token。

純標準庫 unittest，不需要網路。用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.crypto import hash_password
from monitor.transport.device_auth import DeviceRegistry


class DeviceRegistryTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write(self, devices):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": devices}, f)

    def test_missing_file_does_not_exist(self):
        os.remove(self.path)
        reg = DeviceRegistry(self.path)
        self.assertFalse(reg.exists())
        self.assertFalse(reg.verify("pi-01", "anything"))

    def test_existing_file_exists(self):
        self._write([])
        reg = DeviceRegistry(self.path)
        self.assertTrue(reg.exists())

    def test_correct_token_verifies(self):
        self._write([{"device_id": "pi-01", "enabled": True,
                      "token_hash": hash_password("secrettoken")}])
        reg = DeviceRegistry(self.path)
        self.assertTrue(reg.verify("pi-01", "secrettoken"))

    def test_wrong_token_rejected(self):
        self._write([{"device_id": "pi-01", "enabled": True,
                      "token_hash": hash_password("secrettoken")}])
        reg = DeviceRegistry(self.path)
        self.assertFalse(reg.verify("pi-01", "wrongtoken"))

    def test_unknown_device_rejected(self):
        self._write([{"device_id": "pi-01", "enabled": True,
                      "token_hash": hash_password("secrettoken")}])
        reg = DeviceRegistry(self.path)
        self.assertFalse(reg.verify("pi-99", "secrettoken"))

    def test_disabled_device_rejected_even_with_correct_token(self):
        self._write([{"device_id": "pi-01", "enabled": False,
                      "token_hash": hash_password("secrettoken")}])
        reg = DeviceRegistry(self.path)
        self.assertFalse(reg.verify("pi-01", "secrettoken"))

    def test_impersonation_with_another_devices_token_rejected(self):
        self._write([
            {"device_id": "pi-01", "enabled": True,
             "token_hash": hash_password("token-for-01")},
            {"device_id": "pi-02", "enabled": True,
             "token_hash": hash_password("token-for-02")},
        ])
        reg = DeviceRegistry(self.path)
        self.assertFalse(reg.verify("pi-02", "token-for-01"))
        self.assertTrue(reg.verify("pi-01", "token-for-01"))
        self.assertTrue(reg.verify("pi-02", "token-for-02"))

    def test_list_devices_never_includes_token(self):
        self._write([{"device_id": "pi-01", "note": "ICU 3床", "enabled": True,
                      "token_hash": hash_password("secrettoken")}])
        reg = DeviceRegistry(self.path)
        listed = reg.list_devices()
        self.assertEqual(listed, [{"device_id": "pi-01", "note": "ICU 3床",
                                   "enabled": True}])
        self.assertNotIn("token_hash", json.dumps(listed))
        self.assertNotIn("secrettoken", json.dumps(listed))

    def test_reload_picks_up_changes_without_restart(self):
        self._write([{"device_id": "pi-01", "enabled": True,
                      "token_hash": hash_password("old")}])
        reg = DeviceRegistry(self.path)
        self.assertTrue(reg.verify("pi-01", "old"))
        self._write([{"device_id": "pi-01", "enabled": True,
                      "token_hash": hash_password("new")}])
        self.assertFalse(reg.verify("pi-01", "old"))
        self.assertTrue(reg.verify("pi-01", "new"))


if __name__ == "__main__":
    unittest.main()
