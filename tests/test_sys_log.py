# -*- coding: utf-8 -*-
"""SysLog SQLite 儲存、匯出與七天清除測試。"""

import csv
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.domain.sys_log import SysLog


SYS_A = {
    "ts": 1_700_000_000,
    "cpu": 12.3,
    "mem": 45.6,
    "temp": 52.1,
    "disk_pct": 61.2,
    "disk_free": 10.5,
    "throttled": "0x0",
    "uptime": 3600.0,
}


class Clock:
    def __init__(self, value=1_700_000_100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class SysLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rm_syslog_test_")
        self.db_path = os.path.join(self.dir, "sys_history.sqlite3")
        self.clock = Clock()
        self.logs = []

    def make_log(self, path=None, retention_days=7):
        log = SysLog(self.db_path if path is None else path,
                     retention_days=retention_days, now_fn=self.clock)
        self.logs.append(log)
        return log

    def tearDown(self):
        for log in self.logs:
            log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_disabled_when_no_path(self):
        log = self.make_log(path="")
        self.assertFalse(log.enabled)
        self.assertFalse(log.record("pi-01", SYS_A))
        self.assertIsNone(log.export_csv("pi-01"))

    def test_export_keeps_existing_csv_fields(self):
        log = self.make_log()
        self.assertTrue(log.record("pi-01", SYS_A))
        content = log.export_csv("pi-01")
        rows = list(csv.DictReader(io.StringIO(content)))

        self.assertEqual(
            list(rows[0]),
            ["time", "cpu", "mem", "temp", "disk_pct", "disk_free",
             "throttled", "uptime"],
        )
        self.assertEqual(rows[0]["time"], time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(SYS_A["ts"])))
        self.assertEqual(rows[0]["throttled"], "0x0")
        self.assertNotIn("patient", content.lower())

    def test_devices_export_independently(self):
        log = self.make_log()
        log.record("pi-01", SYS_A)
        log.record("pi-02", dict(SYS_A, cpu=99.0))
        rows = list(csv.DictReader(io.StringIO(log.export_csv("pi-01"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cpu"], "12.3")
        self.assertIsNone(log.export_csv("pi-03"))

    def test_received_time_controls_seven_day_retention(self):
        log = self.make_log()
        # 即使 Pi source timestamp 不可信，仍以伺服器收到時間執行保存期限。
        log.record("pi-01", dict(SYS_A, ts=123))
        self.clock.advance(8 * 86400)
        log.record("pi-01", dict(SYS_A, cpu=88.0, ts=124))
        rows = list(csv.DictReader(io.StringIO(log.export_csv("pi-01"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cpu"], "88.0")

    def test_data_survives_restart(self):
        log1 = self.make_log()
        log1.record("pi-01", SYS_A)
        log1.close()
        log2 = self.make_log()
        self.assertIn("12.3", log2.export_csv("pi-01"))


if __name__ == "__main__":
    unittest.main()
