# -*- coding: utf-8 -*-
"""
test_alarm_log — AlarmLog（警報事件歷史落地）單元測試（IMPROVEMENT_PLAN.md W-302）
====================================================================================
鎖住「全量快照 → 出現/解除事件」的比對邏輯：新警報記一筆 appeared、
消失的警報記一筆 cleared、(cp, code) 是識別鍵、CSV 不含病人代碼。

用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.domain.alarm_log import AlarmLog


def read_rows(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class AlarmLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rm_alarmlog_test_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_disabled_when_no_dir(self):
        log = AlarmLog("")
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}])
        self.assertIsNone(log.csv_path("pi-01"))

    def test_new_alarm_writes_appeared(self):
        log = AlarmLog(self.dir)
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}])
        rows = read_rows(log.csv_path("pi-01"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "appeared")
        self.assertEqual(rows[0]["code"], "10")
        self.assertEqual(rows[0]["text"], "PAW HIGH")

    def test_alarm_clearing_writes_cleared(self):
        log = AlarmLog(self.dir)
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}])
        log.on_alarm("pi-01", [])                     # 空陣列 = 全部解除
        rows = read_rows(log.csv_path("pi-01"))
        self.assertEqual([r["event"] for r in rows], ["appeared", "cleared"])

    def test_unchanged_alarm_writes_nothing_new(self):
        log = AlarmLog(self.dir)
        alarm = {"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}
        log.on_alarm("pi-01", [alarm])
        log.on_alarm("pi-01", [alarm])                 # 同一顆警報再送一次，不應多寫
        rows = read_rows(log.csv_path("pi-01"))
        self.assertEqual(len(rows), 1)

    def test_same_code_different_codepage_are_distinct(self):
        """(cp, code) 是識別鍵：cp1 的 code=10 跟 cp2 的 code=10 是不同警報"""
        log = AlarmLog(self.dir)
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 1, "text": "A"},
                               {"cp": 2, "code": "10", "prio": 1, "text": "B"}])
        rows = read_rows(log.csv_path("pi-01"))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["text"] for r in rows}, {"A", "B"})

    def test_devices_are_independent(self):
        log = AlarmLog(self.dir)
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 1, "text": "A"}])
        log.on_alarm("pi-02", [])
        self.assertTrue(os.path.exists(log.csv_path("pi-01")))
        self.assertFalse(os.path.exists(log.csv_path("pi-02")))  # pi-02 從沒發生過警報

    def test_forget_clears_active_state_but_keeps_csv(self):
        log = AlarmLog(self.dir)
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 1, "text": "A"}])
        log.forget("pi-01")
        # 忘記後同一顆警報再送一次，視為全新出現（不會誤判成「沒變化」）
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 1, "text": "A"}])
        rows = read_rows(log.csv_path("pi-01"))
        self.assertEqual([r["event"] for r in rows], ["appeared", "appeared"])

    def test_csv_never_contains_patient_code(self):
        log = AlarmLog(self.dir)
        log.on_alarm("pi-01", [{"cp": 1, "code": "10", "prio": 1, "text": "PAW HIGH"}])
        with open(log.csv_path("pi-01"), encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("patient", content.lower())

    def test_recent_returns_newest_first_and_honours_limit(self):
        log = AlarmLog(self.dir)
        for code in ("10", "11", "12"):
            log.on_alarm("pi-01", [{"cp": 1, "code": code,
                                      "prio": 1, "text": f"Alarm {code}"}])
        rows = log.recent("pi-01", limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["code"] for row in rows], ["11", "12"])
        self.assertEqual([row["event"] for row in rows], ["cleared", "appeared"])

    def test_recent_without_log_returns_empty(self):
        self.assertEqual(AlarmLog("").recent("pi-01"), [])
        self.assertEqual(AlarmLog(self.dir).recent("pi-01"), [])


if __name__ == "__main__":
    unittest.main()
