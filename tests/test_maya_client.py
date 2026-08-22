# -*- coding: utf-8 -*-
"""
test_maya_client — maya 設備清單解析與查詢（傳輸層）
======================================================
鎖住 maya_client.py 最容易在未來改動時弄壞的事：

- **姓名與病歷號絕不出現在解析結果裡**（CLAUDE.md §2.2 紅線）——這是本檔
  最重要的一條，其餘都是功能正確性
- 全形／半形冒號混用都要認得（院方實際回傳就是混用的）
- 帶床位序號的床號（`3520-1`、`HP13-1`）不能被截斷
- 資料不全的單筆只跳過該筆，不能拖垮整份
- 網路/HTTP/JSON 任何失敗都回 None（呼叫端據此重試），**不得拋例外**

🚨 全部案例都用本地資料或本地 http.server，**絕不對 maya-ap 發任何請求**。

用法（專案根目錄）：
    python -m unittest tests.test_maya_client -v
"""

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.transport.maya_client import (fetch_positions, parse_device_list,
                                           parse_entry)

# 實際回傳的形狀（取自使用者提供的樣本，姓名與病歷號改成假資料）
SAMPLE = {
    "CREATE_NAME": "中山醫學大學附設醫院",
    "ON_POSITION": "床號：CCU18 姓名：測試病人 病歷號：TEST0001 記錄時間:2026-08-04 08:56:06",
    "field_value": "27943|EvitaV500",
    "DEVICE_NO": "27943",
    "DEVICE_MODEL": "EvitaV500",
    "USE_STATUS": "Y",
    "RECORD_DATE": "2026-08-04 08:56:06",
    "MODIFY_DATE": "2021-03-16 14:00:00",
    "SERIAL_NUMBER": None,
    "BED_NO": None,
}


def epoch(text):
    """測試預期值：院方時間字串 → epoch 秒（本地時區，與解析器一致）"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


class ParseEntryTest(unittest.TestCase):

    def test_sample_entry(self):
        asset, pos = parse_entry(SAMPLE)
        self.assertEqual(asset, "27943")
        self.assertEqual(pos.bed, "CCU18")
        self.assertEqual(pos.recorded_at, epoch("2026-08-04 08:56:06"))

    def test_no_patient_data_in_result(self):
        """🚨 紅線：姓名與病歷號不得出現在解析結果的任何角落"""
        _, pos = parse_entry(SAMPLE)
        blob = repr(pos) + repr(list(pos))
        self.assertNotIn("測試病人", blob)
        self.assertNotIn("TEST0001", blob)
        self.assertEqual(set(pos._fields), {"bed", "recorded_at"})

    def test_halfwidth_and_fullwidth_colons(self):
        """院方字串全形半形混用；兩種都要認得"""
        for text in ("床號:MI09 姓名:某某 病歷號:X 記錄時間：2026-08-04 08:56:06",
                     "床號：MI09 姓名：某某 病歷號：X 記錄時間:2026-08-04 08:56:06"):
            _, pos = parse_entry(dict(SAMPLE, ON_POSITION=text))
            self.assertEqual(pos.bed, "MI09")

    def test_bed_with_position_suffix(self):
        """一般病房床號帶床位序號，不可在 '-' 處被截斷"""
        for bed in ("3520-1", "HP13-1", "0711-1"):
            text = f"床號：{bed} 姓名：某某 病歷號：X 記錄時間:2026-08-04 08:56:06"
            _, pos = parse_entry(dict(SAMPLE, ON_POSITION=text))
            self.assertEqual(pos.bed, bed)

    def test_record_date_fallback(self):
        """字串內沒有記錄時間 → 退回同筆的 RECORD_DATE"""
        entry = dict(SAMPLE, ON_POSITION="床號：CCU18 姓名：某某 病歷號：X",
                     RECORD_DATE="2026-08-04 09:00:00")
        _, pos = parse_entry(entry)
        self.assertEqual(pos.recorded_at, epoch("2026-08-04 09:00:00"))

    def test_skipped_entries(self):
        """資料不全的單筆一律跳過（都是正常現象，不是錯誤）"""
        cases = [
            dict(SAMPLE, ON_POSITION=None),                    # 機器閒置中
            dict(SAMPLE, ON_POSITION="姓名：某某 病歷號：X"),    # 沒有床號
            dict(SAMPLE, DEVICE_NO=None),                      # 沒有財編
            dict(SAMPLE, ON_POSITION="床號：CCU18", RECORD_DATE=None),   # 沒有時間
            dict(SAMPLE, ON_POSITION="床號：CCU18 記錄時間:壞掉的時間",
                 RECORD_DATE="也是壞的"),
        ]
        for entry in cases:
            asset, pos = parse_entry(entry)
            self.assertIsNone(pos, entry.get("ON_POSITION"))
            self.assertIsNone(asset)


class ParseDeviceListTest(unittest.TestCase):

    def test_indexes_by_asset(self):
        other = dict(SAMPLE, DEVICE_NO="27944",
                     ON_POSITION="床號：3520-1 姓名：某某 病歷號：X "
                                 "記錄時間:2026-08-04 10:00:00")
        positions = parse_device_list([SAMPLE, other])
        self.assertEqual(set(positions), {"27943", "27944"})
        self.assertEqual(positions["27944"].bed, "3520-1")

    def test_duplicate_asset_keeps_newest(self):
        older = dict(SAMPLE, ON_POSITION="床號：MI01 姓名：某某 病歷號：X "
                                         "記錄時間:2026-08-01 08:00:00")
        newer = dict(SAMPLE, ON_POSITION="床號：CCU18 姓名：某某 病歷號：X "
                                         "記錄時間:2026-08-04 08:56:06")
        for order in ([older, newer], [newer, older]):
            positions = parse_device_list(order)
            self.assertEqual(positions["27943"].bed, "CCU18")

    def test_bad_shapes_do_not_raise(self):
        self.assertEqual(parse_device_list(None), {})
        self.assertEqual(parse_device_list("不是清單"), {})
        self.assertEqual(parse_device_list([None, 42, "x"]), {})

    def test_wrapped_list(self):
        """回傳被包進物件時仍認得（Tolerant Reader）"""
        self.assertIn("27943", parse_device_list({"data": [SAMPLE]}))

    def test_one_bad_entry_does_not_kill_the_rest(self):
        positions = parse_device_list([{"DEVICE_NO": None}, SAMPLE, 42])
        self.assertEqual(set(positions), {"27943"})


class _Handler(BaseHTTPRequestHandler):
    """本地假 maya；status/body 由 server 屬性控制"""

    def do_GET(self):
        body = self.server.body.encode("utf-8")
        self.send_response(self.server.code)
        self.send_header("Content-Type", self.server.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class FetchTest(unittest.TestCase):
    """🚨 只打本地 127.0.0.1 假伺服器，絕不碰院方系統"""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.code = 200
        self.server.ctype = "text/plain"      # 院方不保證回正確的 Content-Type
        self.server.body = json.dumps([SAMPLE], ensure_ascii=False)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/getDeviceList"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def fetch(self):
        return asyncio.run(fetch_positions(self.url, timeout=5.0))

    def test_fetch_parses_positions(self):
        positions = self.fetch()
        self.assertEqual(positions["27943"].bed, "CCU18")

    def test_http_error_returns_none(self):
        self.server.code = 500
        self.assertIsNone(self.fetch())

    def test_bad_json_returns_none(self):
        self.server.body = "<html>維護中</html>"
        self.assertIsNone(self.fetch())

    def test_empty_list_is_not_none(self):
        """問到了但沒有任何位置資訊 → 空 dict（與「沒問到」是兩件事）"""
        self.server.body = "[]"
        self.assertEqual(self.fetch(), {})

    def test_connection_refused_returns_none(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertIsNone(self.fetch())


if __name__ == "__main__":
    unittest.main(verbosity=2)
