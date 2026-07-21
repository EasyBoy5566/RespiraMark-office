# -*- coding: utf-8 -*-
"""AlarmLog SQLite episode 儲存、查詢與七天清除測試。"""

import csv
import io
import os
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.domain.alarm_log import AlarmLog


ALARM_A = {"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}


class Clock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AlarmLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rm_alarmlog_test_")
        self.db_path = os.path.join(self.dir, "alarm_history.sqlite3")
        self.clock = Clock()
        self.logs = []

    def make_log(self, path=None, retention_days=7):
        log = AlarmLog(self.db_path if path is None else path,
                       retention_days=retention_days, now_fn=self.clock)
        self.logs.append(log)
        return log

    def tearDown(self):
        for log in self.logs:
            log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_disabled_when_no_path(self):
        log = self.make_log(path="")
        log.on_alarm("pi-01", [ALARM_A])
        self.assertFalse(log.enabled)
        self.assertEqual(log.recent("pi-01"), [])
        self.assertIsNone(log.export_csv("pi-01"))

    def test_first_snapshot_creates_active_observed_episode(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A], source_ts=self.clock.value - 1)

        rows = log.recent("pi-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["start_reason"], "observed_active")
        self.assertEqual(rows[0]["code"], "10")
        self.assertIsNotNone(rows[0]["source_started_at"])

    def test_clearing_updates_same_episode_and_duration(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        episode_id = log.recent("pi-01")[0]["episode_id"]
        self.clock.advance(12.5)
        log.on_alarm("pi-01", [])

        rows = log.recent("pi-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["episode_id"], episode_id)
        self.assertEqual(rows[0]["status"], "cleared")
        self.assertEqual(rows[0]["end_reason"], "cleared")
        self.assertEqual(rows[0]["duration_seconds"], 12.5)
        self.assertIsNotNone(rows[0]["ended_at"])

    def test_unchanged_alarm_does_not_duplicate_episode(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        self.clock.advance(2)
        log.on_alarm("pi-01", [dict(ALARM_A, text="PAW HIGH updated")])
        rows = log.recent("pi-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "PAW HIGH updated")

    def test_new_alarm_after_first_snapshot_is_appeared(self):
        log = self.make_log()
        log.on_alarm("pi-01", [])
        log.on_alarm("pi-01", [ALARM_A])
        self.assertEqual(log.recent("pi-01")[0]["start_reason"], "appeared")

    def test_same_code_different_codepage_are_distinct(self):
        log = self.make_log()
        log.on_alarm("pi-01", [
            {"cp": 1, "code": "10", "prio": 1, "text": "A"},
            {"cp": 2, "code": "10", "prio": 1, "text": "B"},
        ])
        rows = log.recent("pi-01")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["text"] for row in rows}, {"A", "B"})

    def test_devices_can_be_queried_independently(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        log.on_alarm("pi-02", [{"cp": 2, "code": "20", "prio": 3, "text": "B"}])
        self.assertEqual([row["code"] for row in log.recent("pi-01")], ["10"])
        self.assertEqual([row["code"] for row in log.recent("pi-02")], ["20"])
        self.assertEqual(log.recent("pi-03"), [])

    def test_disconnect_marks_active_episode_unknown_not_cleared(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        self.clock.advance(3)
        log.on_disconnect("pi-01")
        row = log.recent("pi-01")[0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["end_reason"], "device_offline")
        self.assertEqual(row["duration_seconds"], 3.0)

    def test_active_episode_is_never_expired(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        self.clock.advance(8 * 86400)
        rows = log.recent("pi-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active")

    def test_finished_episode_older_than_seven_days_is_deleted(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        self.clock.advance(5)
        log.on_alarm("pi-01", [])
        self.clock.advance(8 * 86400)
        self.assertEqual(log.recent("pi-01"), [])

    def test_recent_returns_newest_first_and_honours_limit(self):
        log = self.make_log()
        log.on_alarm("pi-01", [])
        for code in ("10", "11", "12"):
            self.clock.advance(1)
            log.on_alarm("pi-01", [{"cp": 1, "code": code,
                                      "prio": 1, "text": f"Alarm {code}"}])
        rows = log.recent("pi-01", limit=2)
        self.assertEqual([row["code"] for row in rows], ["12", "11"])
        self.assertEqual([row["status"] for row in rows], ["active", "cleared"])

    def test_export_csv_contains_episode_fields_and_no_patient(self):
        log = self.make_log()
        log.on_alarm("pi-01", [ALARM_A])
        content = log.export_csv("pi-01")
        rows = list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(rows[0]["device_id"], "pi-01")
        self.assertEqual(rows[0]["status"], "active")
        self.assertIn("duration_seconds", rows[0])
        self.assertNotIn("patient", content.lower())

    def test_restart_closes_stale_active_then_starts_new_episode(self):
        log1 = self.make_log()
        log1.on_alarm("pi-01", [ALARM_A])
        # 模擬程序非正常中斷：略過 close()，讓資料庫保留 active。
        log1._conn.close()
        log1._conn = None
        self.clock.advance(20)

        log2 = self.make_log()
        stale = log2.recent("pi-01")[0]
        self.assertEqual(stale["status"], "unknown")
        self.assertEqual(stale["end_reason"], "server_restart")

        log2.on_alarm("pi-01", [ALARM_A])
        rows = log2.recent("pi-01")
        self.assertEqual([row["status"] for row in rows], ["active", "unknown"])
        self.assertEqual(rows[0]["start_reason"], "observed_active")


if __name__ == "__main__":
    unittest.main()
