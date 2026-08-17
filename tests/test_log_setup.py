# -*- coding: utf-8 -*-
"""
test_log_setup — 日誌時戳與保留期（資通系統防護基準第 16／19／24 點）
====================================================================
- 第 24 點：時戳要「可以對應到世界協調時間(UTC)」——只寫本地時刻而沒有
  時區位移，事後跨系統比對無從還原，所以驗證輸出可被 fromisoformat 解析
  且帶得出 utcoffset()。
- 第 16 點：正式系統日誌「保留至少 6 個月」——驗證用的是日期輪替（大小
  輪替只保證留最近 N MB，忙起來會把幾個月前的紀錄擠掉），且保留天數低於
  180 會被拉回下限。
- 第 19 點：日誌需含事件類型/時間/位置/使用者身分——驗證 audit 那一行
  四者俱全。

用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import datetime
import logging
import os
import shutil
import sys
import tempfile
import unittest
from logging.handlers import TimedRotatingFileHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import main as app_main


def _format(fmt: str, message: str) -> str:
    record = logging.LogRecord("audit", logging.INFO, __file__, 1,
                               message, None, None)
    return app_main.LocalIsoFormatter(fmt).format(record)


class TimestampTest(unittest.TestCase):

    def test_時戳可還原成UTC(self):
        stamp = _format("%(asctime)s %(message)s", "x").split(" ")[0]
        parsed = datetime.datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.utcoffset(),
                             f"時戳沒有時區位移，無法對應 UTC: {stamp}")

    def test_時戳含完整日期(self):
        """原本 server.log 的 datefmt 只有 %H:%M:%S，跨日之後根本分不出是哪天。"""
        stamp = _format("%(asctime)s %(message)s", "x").split(" ")[0]
        parsed = datetime.datetime.fromisoformat(stamp)
        self.assertEqual(parsed.date(), datetime.date.today())

    def test_審計行含事件類型與身分與位置(self):
        line = _format("%(asctime)s %(message)s",
                       "login_ok ip=10.1.2.3 username=alice role=admin")
        self.assertIn("login_ok", line)        # 事件類型
        self.assertIn("ip=10.1.2.3", line)     # 發生位置
        self.assertIn("username=alice", line)  # 使用者身分


class RetentionTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.audit = logging.getLogger("audit")
        self.saved = self.audit.handlers[:], self.audit.propagate

    def tearDown(self):
        for h in self.audit.handlers:
            if h not in self.saved[0]:
                h.close()
        self.audit.handlers, self.audit.propagate = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_審計日誌採日期輪替且保留六個月以上(self):
        app_main.setup_audit_log({"log_dir": self.tmp, "log_retention_days": 190})
        timed = [h for h in self.audit.handlers
                 if isinstance(h, TimedRotatingFileHandler)]
        self.assertTrue(timed, "audit.log 必須用日期輪替才談得上保留期限")
        self.assertGreaterEqual(timed[0].backupCount, 180)

    def test_保留天數低於六個月會被拉回下限(self):
        self.assertEqual(app_main._log_retention_days({"log_retention_days": 7}), 180)

    def test_未設定時採預設值(self):
        self.assertGreaterEqual(app_main._log_retention_days({}), 180)


if __name__ == "__main__":
    unittest.main()
