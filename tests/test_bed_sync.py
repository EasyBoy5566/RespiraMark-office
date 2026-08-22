# -*- coding: utf-8 -*-
"""
test_bed_sync — 床號自動帶入的排程與規則（領域層）
====================================================
用假 fetch、假 callback 與假時鐘驗證 PROTOCOL.md「床號自動帶入」的狀態機，
不起伺服器、**不碰任何網路**。

鎖住最容易在未來改動時弄壞的判斷：

- 舊紀錄（記錄時間早於 A）**不能**被採用——那是上一位病人的床號，
  帶錯床比空白還糟
- 新紀錄採用後要**停止查詢**，不再打擾院方正式系統
- 時間容差的方向：偏向「接受」。若寫成必須晚於 A ＋ 容差，接機前先更新
  maya 的正確資料會被永遠拒絕、白白輪詢滿一小時
- 呼吸器斷線 = 取消；Pi 離線 = 暫停（Wi-Fi 抖一下不該讓床號永遠空著）
- 沒有查得動的裝置時**完全不發請求**
- 逾時要放棄，不能無止境輪詢

用法（專案根目錄）：
    python -m unittest tests.test_bed_sync -v
"""

import asyncio
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.domain.bed_sync import BedSync
from monitor.transport.maya_client import Position

NOW = 1_800_000_000.0        # 固定的假「現在」，讓時間比較的意圖一目了然


class FakeDirectory:
    """只提供 all_meta()——BedSync 用得到的就這一個"""

    def __init__(self, metas=None):
        self.metas = metas if metas is not None else {
            "pi-01": {"bed": "", "ward": "", "asset": "27943"}}

    def all_meta(self):
        return self.metas


class BedSyncTestBase(unittest.TestCase):

    def setUp(self):
        self.now = NOW
        self.online = {"pi-01": True}
        self.positions = {}          # 假 maya 的回傳；None = 這輪查詢失敗
        self.fetch_calls = 0
        self.applied = []            # [(device, bed)]
        self.apply_result = "ok"
        self.directory = FakeDirectory()
        self.sync = BedSync(directory=self.directory,
                            apply_bed=self.apply_bed,
                            is_online=lambda d: self.online.get(d, False),
                            fetch=self.fetch,
                            url="http://127.0.0.1:0/fake",
                            poll_interval=30.0,
                            max_duration=3600.0,
                            tolerance=120.0,
                            clock=lambda: self.now)

    async def fetch(self, url, timeout=None):
        self.fetch_calls += 1
        return self.positions

    def apply_bed(self, device, bed):
        self.applied.append((device, bed))
        return self.apply_result, {"device": device, "bed": bed}

    # ── 小工具 ──────────────────────────────────────────────────────

    def connect_vent(self, device="pi-01"):
        """呼吸器連上 → 記下 A = 此刻"""
        self.sync.on_hello(device)
        self.sync.on_status(device, "connected")

    def maya_says(self, bed, recorded_at, asset="27943"):
        self.positions = {asset: Position(bed, recorded_at)}

    def poll(self):
        asyncio.run(self.sync.poll_once())


class AdoptionTest(BedSyncTestBase):

    def test_fresh_record_is_adopted_and_stops_polling(self):
        self.connect_vent()
        self.maya_says("CCU18", NOW + 10)          # 接機後才更新 → 新資料
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])
        # 已完成 → 之後不再發任何請求
        calls = self.fetch_calls
        self.poll()
        self.assertEqual(self.fetch_calls, calls)

    def test_stale_record_is_ignored_and_keeps_polling(self):
        """上一位病人的紀錄絕不能被採用"""
        self.connect_vent()
        self.maya_says("MI01", NOW - 86400)        # 一天前
        self.poll()
        self.assertEqual(self.applied, [])
        self.poll()
        self.assertEqual(self.fetch_calls, 2)      # 仍在查

    def test_record_within_tolerance_before_a_is_adopted(self):
        """醫護常在接機前一兩分鐘就先更新 maya——容差內要接受"""
        self.connect_vent()
        self.maya_says("CCU18", NOW - 60)          # A 前 60 秒，容差 120
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])

    def test_record_outside_tolerance_before_a_is_rejected(self):
        self.connect_vent()
        self.maya_says("MI01", NOW - 600)          # A 前 10 分鐘，超出容差
        self.poll()
        self.assertEqual(self.applied, [])

    def test_unknown_asset_keeps_polling(self):
        """maya 上查無此財編 → 下輪再試（可能還沒登記）"""
        self.connect_vent()
        self.maya_says("CCU18", NOW + 10, asset="99999")
        self.poll()
        self.assertEqual(self.applied, [])
        self.assertEqual(self.fetch_calls, 1)

    def test_asset_match_ignores_case_and_space(self):
        self.directory.metas["pi-01"]["asset"] = "  a-27943 "
        self.connect_vent()
        self.maya_says("CCU18", NOW + 10, asset="A-27943")
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])

    def test_same_bed_is_not_rewritten(self):
        """床號沒變就不重寫清冊、不重複廣播"""
        self.directory.metas["pi-01"]["bed"] = "CCU18"
        self.connect_vent()
        self.maya_says("CCU18", NOW + 10)
        self.poll()
        self.assertEqual(self.applied, [])
        self.assertEqual(self.fetch_calls, 1)
        self.poll()                                # 仍視為完成，不再查
        self.assertEqual(self.fetch_calls, 1)

    def test_write_failure_does_not_retry(self):
        """清冊寫不進去（未登記／檔案問題）→ 記 log 後結束，不無限重試"""
        self.apply_result = "not_found"
        self.connect_vent()
        self.maya_says("CCU18", NOW + 10)
        self.poll()
        self.assertEqual(len(self.applied), 1)
        self.poll()
        self.assertEqual(len(self.applied), 1)


class LifecycleTest(BedSyncTestBase):

    def test_no_request_when_nothing_pending(self):
        self.poll()
        self.assertEqual(self.fetch_calls, 0)

    def test_vent_disconnect_cancels(self):
        self.connect_vent()
        self.sync.on_status("pi-01", "disconnected")
        self.poll()
        self.assertEqual(self.fetch_calls, 0)

    def test_repeated_connected_does_not_restart_a(self):
        """同一次連線重複收到 connected 不重新起算 A"""
        self.connect_vent()
        self.now = NOW + 300
        self.sync.on_status("pi-01", "connected")
        self.maya_says("CCU18", NOW + 10)          # 相對原始 A 是新資料
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])

    def test_reconnect_starts_new_session(self):
        """Pi 重連（可能換病人）→ 重新起算 A，舊紀錄不再算數"""
        self.connect_vent()
        self.maya_says("CCU18", NOW + 10)
        self.poll()
        self.assertEqual(len(self.applied), 1)
        self.now = NOW + 7200                      # 兩小時後換了病人
        self.connect_vent()
        self.poll()                                # maya 還是舊的那筆
        self.assertEqual(len(self.applied), 1)     # 不採用
        self.maya_says("MI01", self.now + 5)       # maya 更新了
        self.poll()
        self.assertEqual(self.applied[-1], ("pi-01", "MI01"))

    def test_offline_pauses_but_does_not_cancel(self):
        """Wi-Fi 抖一下不該讓床號永遠空著"""
        self.connect_vent()
        self.online["pi-01"] = False
        self.poll()
        self.assertEqual(self.fetch_calls, 0)      # 暫停期間不發請求
        self.online["pi-01"] = True
        self.maya_says("CCU18", NOW + 10)
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])

    def test_expires_after_max_duration(self):
        self.connect_vent()
        self.now = NOW + 3601
        self.poll()
        self.assertEqual(self.fetch_calls, 0)      # 已放棄，不再發請求
        self.maya_says("CCU18", self.now)
        self.poll()
        self.assertEqual(self.applied, [])

    def test_forget_removes_device(self):
        self.connect_vent()
        self.sync.forget("pi-01")
        self.poll()
        self.assertEqual(self.fetch_calls, 0)

    def test_fetch_failure_keeps_pending(self):
        self.connect_vent()
        self.positions = None                      # 這輪查不到
        self.poll()
        self.assertEqual(self.applied, [])
        self.maya_says("CCU18", NOW + 10)
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])


class TargetSelectionTest(BedSyncTestBase):

    def test_no_asset_means_no_request(self):
        """沒登記財編就查不了，不該為此打院方系統"""
        self.directory.metas["pi-01"]["asset"] = ""
        self.connect_vent()
        self.poll()
        self.assertEqual(self.fetch_calls, 0)

    def test_unregistered_device_means_no_request(self):
        self.directory.metas = {}
        self.connect_vent()
        self.poll()
        self.assertEqual(self.fetch_calls, 0)

    def test_asset_registered_later_starts_working(self):
        """先連線、管理員之後才登記財編 → 下一輪就查得到"""
        self.directory.metas["pi-01"]["asset"] = ""
        self.connect_vent()
        self.poll()
        self.assertEqual(self.fetch_calls, 0)
        self.directory.metas["pi-01"]["asset"] = "27943"
        self.maya_says("CCU18", NOW + 10)
        self.poll()
        self.assertEqual(self.applied, [("pi-01", "CCU18")])

    def test_one_request_covers_all_devices(self):
        """多台待查共用一個請求，不是每台各發一次"""
        self.directory.metas = {
            "pi-01": {"bed": "", "asset": "27943"},
            "pi-02": {"bed": "", "asset": "27944"}}
        self.online = {"pi-01": True, "pi-02": True}
        self.connect_vent("pi-01")
        self.connect_vent("pi-02")
        self.positions = {"27943": Position("CCU18", NOW + 10),
                          "27944": Position("3520-1", NOW + 10)}
        self.poll()
        self.assertEqual(self.fetch_calls, 1)
        self.assertEqual(sorted(self.applied),
                         [("pi-01", "CCU18"), ("pi-02", "3520-1")])


class PollLoopTest(BedSyncTestBase):

    def test_poll_loop_survives_unexpected_error(self):
        """輪詢工作絕不能因為未預期例外而死掉（伺服器要無人值守運行）"""
        async def boom(url, timeout=None):
            raise RuntimeError("院方系統回了完全沒想到的東西")

        self.sync._fetch = boom
        self.sync.poll_interval = 0.01
        self.connect_vent()

        async def run_briefly():
            task = asyncio.ensure_future(self.sync.run())
            await asyncio.sleep(0.05)
            still_running = not task.done()
            task.cancel()
            return still_running

        self.assertTrue(asyncio.run(run_briefly()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
