# -*- coding: utf-8 -*-
"""
test_device_directory — 裝置清冊（床號／呼吸器財編）單元測試
================================================================
鎖住最容易在未來改動時弄壞的規則：
- **清冊檔不存在時絕不建檔**（建檔會把伺服器切換成逐台驗證模式，
  讓所有用共用權杖的既有裝置在下次重連時被拒）
- 換發權杖時保留床號／財編／備註（換的是權杖，不是實體機器）
- 設定床號不影響其他裝置，也不動 token_hash
- 清冊查詢絕不回傳 token_hash

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

from monitor.domain.device_directory import DeviceDirectory


class DeviceDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "devices.json")
        self.dir = DeviceDirectory(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, devices):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": devices}, f)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)["devices"]

    def entry(self, device_id="pi-01", **kw):
        d = {"device_id": device_id, "note": "", "enabled": True,
             "token_hash": f"hash-of-{device_id}", "bed": "", "asset": ""}
        d.update(kw)
        return d

    # ── 不存在的清冊檔 ──────────────────────────────────────────────

    def test_missing_file_is_empty_not_created(self):
        self.assertFalse(self.dir.exists())
        self.assertEqual(self.dir.all_meta(), {})
        self.assertFalse(self.dir.has_device("pi-01"))
        self.assertEqual(self.dir.meta("pi-01"),
                         {"bed": "", "asset": "", "note": ""})
        self.assertFalse(os.path.exists(self.path))   # 讀取不得建檔

    def test_set_meta_refuses_to_create_registry(self):
        """共用 token 模式下改床號若建出 devices.json，所有既有裝置會被踢掉"""
        result, data = self.dir.set_meta("pi-01", bed="RCC-01")
        self.assertEqual(result, "no_registry")
        self.assertIsNone(data)
        self.assertFalse(os.path.exists(self.path))

    def test_set_meta_unknown_device(self):
        self.write([self.entry("pi-01")])
        self.assertEqual(self.dir.set_meta("pi-99", bed="RCC-09")[0], "not_found")

    # ── 設定床號／財編 ──────────────────────────────────────────────

    def test_set_bed_and_asset(self):
        self.write([self.entry("pi-01")])
        result, data = self.dir.set_meta("pi-01", bed="RCC-01", asset="A-123")
        self.assertEqual(result, "ok")
        self.assertEqual(data, {"device": "pi-01", "bed": "RCC-01", "asset": "A-123"})

        d = self.read()[0]
        self.assertEqual((d["bed"], d["asset"]), ("RCC-01", "A-123"))
        self.assertEqual(d["token_hash"], "hash-of-pi-01")   # 權杖不受影響
        self.assertTrue(d["enabled"])

    def test_none_keeps_previous_value(self):
        self.write([self.entry("pi-01", bed="RCC-01", asset="A-123")])
        self.dir.set_meta("pi-01", bed="RCC-02")             # 只改床號
        d = self.read()[0]
        self.assertEqual((d["bed"], d["asset"]), ("RCC-02", "A-123"))

    def test_empty_string_clears(self):
        self.write([self.entry("pi-01", bed="RCC-01", asset="A-123")])
        self.dir.set_meta("pi-01", bed="", asset="")
        d = self.read()[0]
        self.assertEqual((d["bed"], d["asset"]), ("", ""))

    def test_values_are_trimmed_and_capped(self):
        self.write([self.entry("pi-01")])
        self.dir.set_meta("pi-01", bed="  RCC-01  ", asset="A" * 100)
        d = self.read()[0]
        self.assertEqual(d["bed"], "RCC-01")
        self.assertEqual(len(d["asset"]), 32)

    def test_other_devices_untouched(self):
        self.write([self.entry("pi-01", bed="RCC-01"),
                    self.entry("pi-02", bed="RCC-02", enabled=False)])
        self.dir.set_meta("pi-01", bed="RCC-09")
        by_id = {d["device_id"]: d for d in self.read()}
        self.assertEqual(by_id["pi-02"]["bed"], "RCC-02")
        self.assertFalse(by_id["pi-02"]["enabled"])
        self.assertEqual(by_id["pi-02"]["token_hash"], "hash-of-pi-02")

    # ── 讀取 ────────────────────────────────────────────────────────

    def test_all_meta_never_exposes_token(self):
        self.write([self.entry("pi-01", bed="RCC-01", asset="A-1", note="ICU")])
        metas = self.dir.all_meta()
        self.assertEqual(metas["pi-01"],
                         {"bed": "RCC-01", "asset": "A-1", "note": "ICU"})
        self.assertNotIn("hash-of-pi-01", json.dumps(metas))

    def test_corrupt_file_is_treated_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(self.dir.all_meta(), {})
        # 但檔案存在 → 仍允許寫入（不會誤判成共用 token 模式）
        self.assertEqual(self.dir.set_meta("pi-01", bed="X")[0], "not_found")

    # ── 配對核發（upsert）──────────────────────────────────────────

    def test_upsert_creates_registry_with_empty_meta(self):
        self.assertTrue(self.dir.upsert_device("pi-01", "ICU 3床", "hash-1"))
        d = self.read()[0]
        self.assertEqual(d["device_id"], "pi-01")
        self.assertEqual(d["note"], "ICU 3床")
        self.assertEqual((d["bed"], d["asset"]), ("", ""))

    def test_reissue_keeps_bed_asset_and_note(self):
        """換發只換權杖：實體機器沒變，床號、財編與管理員備註都要留著"""
        self.write([self.entry("pi-01", bed="RCC-01", asset="A-123",
                               note="管理員填的備註")])
        self.assertTrue(self.dir.upsert_device("pi-01", "配對申請帶來的備註", "hash-新"))
        devices = self.read()
        self.assertEqual(len(devices), 1)             # 覆寫而非新增
        d = devices[0]
        self.assertEqual(d["token_hash"], "hash-新")
        self.assertEqual(d["bed"], "RCC-01")
        self.assertEqual(d["asset"], "A-123")
        self.assertEqual(d["note"], "管理員填的備註")

    def test_reissue_reenables_disabled_device(self):
        self.write([self.entry("pi-01", enabled=False)])
        self.dir.upsert_device("pi-01", "", "hash-新")
        self.assertTrue(self.read()[0]["enabled"])


if __name__ == "__main__":
    unittest.main()
