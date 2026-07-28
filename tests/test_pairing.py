# -*- coding: utf-8 -*-
"""
test_pairing — PairingService（裝置配對狀態機）單元測試
=========================================================
鎖住配對流程中最容易在未來改動時弄壞的規則：
- token 只能被領取一次（第二次一律 expired）
- 待核可上限、同一 IP 只留一筆、TTL 逾時
- 核可寫入 devices.json 的內容正確（雜湊可驗證、不影響其他裝置）
- 換發（renew）會讓舊 token 失效
- 未知 pair_id 與逾時回應相同，不洩漏存在性

時間用 now_fn 注入的假時鐘控制（不 sleep，避免慢機器 flaky）。
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

from monitor.crypto import verify_password
from monitor.domain.pairing import PairingService


class PairingTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)                  # 預設從「還沒有 devices.json」開始
        self.clock = [1000.0]
        self.svc = PairingService(self.path, ingest_port=8765, ttl=600.0,
                                  max_pending=5, now_fn=lambda: self.clock[0])

    def tearDown(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def tick(self, seconds):
        self.clock[0] += seconds

    def read_devices(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)["devices"]

    def write_devices(self, devices):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": devices}, f)

    def request_ok(self, device_id="pi-01", ip="10.0.0.1", note=""):
        result, data = self.svc.request(device_id, note, ip)
        self.assertEqual(result, "ok")
        return data


class RequestTest(PairingTestBase):
    def test_request_returns_pair_id_and_six_digit_code(self):
        data = self.request_ok()
        self.assertTrue(data["pair_id"])
        self.assertRegex(data["code"], r"^\d{6}$")
        self.assertEqual(data["expires_in"], 600)
        self.assertFalse(data["renew"])
        self.assertEqual(len(self.svc.list_pending()), 1)

    def test_bad_device_id_rejected(self):
        for bad in ("", "   ", "pi 01", "病房一", "a" * 65, "pi/01"):
            result, data = self.svc.request(bad, "", "10.0.0.1")
            self.assertEqual(result, "bad_id", f"應拒絕 {bad!r}")
            self.assertIsNone(data)
        self.assertEqual(self.svc.list_pending(), [])

    def test_note_is_trimmed_to_limit(self):
        data = self.request_ok(note="床" * 100)
        entry = self.svc.list_pending()[0]
        self.assertEqual(len(entry["note"]), 64)
        self.assertEqual(entry["device_id"], data["device_id"])

    def test_pending_limit(self):
        for i in range(5):
            self.request_ok(device_id=f"pi-{i}", ip=f"10.0.0.{i}")
        result, data = self.svc.request("pi-9", "", "10.0.0.9")
        self.assertEqual(result, "limit")
        self.assertIsNone(data)
        self.assertEqual(len(self.svc.list_pending()), 5)

    def test_same_ip_replaces_previous_request(self):
        first = self.request_ok(device_id="pi-01", ip="10.0.0.7")
        second = self.request_ok(device_id="pi-02", ip="10.0.0.7")
        self.assertEqual(second["replaced"], ["pi-01"])
        self.assertEqual(len(self.svc.list_pending()), 1)
        # 舊申請立刻失效
        self.assertEqual(self.svc.poll(first["pair_id"])[0]["status"], "expired")
        self.assertEqual(self.svc.poll(second["pair_id"])[0]["status"], "pending")

    def test_renew_flag_when_device_already_registered(self):
        self.write_devices([{"device_id": "pi-01", "note": "舊的",
                             "enabled": True, "token_hash": "x"}])
        self.assertTrue(self.request_ok(device_id="pi-01")["renew"])
        self.assertTrue(self.svc.list_pending()[0]["renew"])


class PollTest(PairingTestBase):
    def test_unknown_pair_id_looks_like_expired(self):
        status, claimed = self.svc.poll("no-such-id")
        self.assertEqual(status, {"status": "expired"})
        self.assertIsNone(claimed)

    def test_pending_then_approved_then_claimed_once(self):
        data = self.request_ok()
        self.assertEqual(self.svc.poll(data["pair_id"])[0]["status"], "pending")

        result, info = self.svc.approve(data["pair_id"])
        self.assertEqual(result, "ok")
        self.assertEqual(info["device_id"], "pi-01")

        first, claimed = self.svc.poll(data["pair_id"])
        self.assertEqual(first["status"], "approved")
        self.assertEqual(first["server_port"], 8765)
        self.assertEqual(claimed, "pi-01")
        self.assertTrue(first["token"])

        # 一次領取即銷毀
        second, claimed2 = self.svc.poll(data["pair_id"])
        self.assertEqual(second["status"], "expired")
        self.assertIsNone(claimed2)
        self.assertEqual(self.svc.list_pending(), [])

    def test_denied_stays_readable_until_ttl(self):
        data = self.request_ok()
        self.assertEqual(self.svc.deny(data["pair_id"])[0], "ok")
        for _ in range(3):                    # 可重複查到（Pi 可能慢一拍才輪詢）
            self.assertEqual(self.svc.poll(data["pair_id"])[0]["status"], "denied")
        self.assertEqual(self.svc.list_pending(), [])   # 但不再出現在管理頁
        self.assertFalse(os.path.exists(self.path))     # 拒絕不寫 devices.json

    def test_expiry(self):
        data = self.request_ok()
        self.tick(601)
        self.assertEqual(self.svc.poll(data["pair_id"])[0]["status"], "expired")
        self.assertEqual(self.svc.list_pending(), [])

    def test_approved_but_never_claimed_expires(self):
        data = self.request_ok()
        self.svc.approve(data["pair_id"])
        self.tick(601)
        self.assertEqual(self.svc.poll(data["pair_id"])[0]["status"], "expired")
        # 裝置項目仍留在 devices.json（無人知道其 token，重新配對即換發覆蓋）
        self.assertEqual(len(self.read_devices()), 1)


class ApproveTest(PairingTestBase):
    def test_written_token_verifies(self):
        data = self.request_ok(note="ICU 3床")
        self.svc.approve(data["pair_id"])
        token = self.svc.poll(data["pair_id"])[0]["token"]

        devices = self.read_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["device_id"], "pi-01")
        self.assertEqual(devices[0]["note"], "ICU 3床")
        self.assertTrue(devices[0]["enabled"])
        self.assertTrue(verify_password(token, devices[0]["token_hash"]))
        self.assertNotIn(token, json.dumps(devices))     # 只存雜湊，不存明文

    def test_approve_unknown_and_twice(self):
        self.assertEqual(self.svc.approve("no-such-id")[0], "not_found")
        data = self.request_ok()
        self.assertEqual(self.svc.approve(data["pair_id"])[0], "ok")
        self.assertEqual(self.svc.approve(data["pair_id"])[0], "already")
        self.assertEqual(self.svc.deny(data["pair_id"])[0], "already")

    def test_deny_then_approve_blocked(self):
        data = self.request_ok()
        self.assertEqual(self.svc.deny(data["pair_id"])[0], "ok")
        self.assertEqual(self.svc.approve(data["pair_id"])[0], "already")

    def test_renew_rotates_token_and_keeps_note(self):
        first = self.request_ok(device_id="pi-01", note="ICU 3床")
        self.svc.approve(first["pair_id"])
        old_token = self.svc.poll(first["pair_id"])[0]["token"]

        second = self.request_ok(device_id="pi-01", ip="10.0.0.2", note="換的備註")
        self.assertTrue(second["renew"])
        self.svc.approve(second["pair_id"])
        new_token = self.svc.poll(second["pair_id"])[0]["token"]

        devices = self.read_devices()
        self.assertEqual(len(devices), 1)                 # 覆寫而非新增
        self.assertEqual(devices[0]["note"], "ICU 3床")   # 換發保留原備註
        self.assertTrue(verify_password(new_token, devices[0]["token_hash"]))
        self.assertFalse(verify_password(old_token, devices[0]["token_hash"]))

    def test_other_devices_preserved(self):
        self.write_devices([{"device_id": "pi-other", "note": "別台",
                             "enabled": False, "token_hash": "keep-me"}])
        data = self.request_ok(device_id="pi-new")
        self.svc.approve(data["pair_id"])

        devices = {d["device_id"]: d for d in self.read_devices()}
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices["pi-other"]["token_hash"], "keep-me")
        self.assertFalse(devices["pi-other"]["enabled"])

    def test_write_failure_keeps_request_pending(self):
        data = self.request_ok()
        os.makedirs(self.path)                # 路徑變目錄 → 寫入必定失敗
        try:
            self.assertEqual(self.svc.approve(data["pair_id"])[0], "write_failed")
            # 仍在待核可清單，管理員可直接重按
            self.assertEqual(len(self.svc.list_pending()), 1)
            self.assertEqual(self.svc.poll(data["pair_id"])[0]["status"], "pending")
        finally:
            os.rmdir(self.path)


if __name__ == "__main__":
    unittest.main()
