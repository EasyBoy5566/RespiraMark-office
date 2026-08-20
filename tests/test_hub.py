# -*- coding: utf-8 -*-
"""
test_hub — TelemetryHub（領域層）單元測試
==========================================
鎖住 hub.py 最容易在未來改動時弄壞的邏輯：
conn_seq 連線世代、watchdog 逾時判離線、max_devices 上限、
佇列滿丟最舊（絕不回壓）、未知訊息忽略、snapshot 組裝。

純標準庫 unittest，不需要網路與 aiohttp。用法（專案根目錄）：
    python -m unittest discover -s tests -v
或  python tests/test_hub.py
"""

import asyncio
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.domain.hub import TelemetryHub


def hello_msg(device="pi-01", patient="TEST001"):
    return {"type": "hello", "v": 1, "device": device, "patient": patient}


def drain(q):
    """取出佇列中所有訊息並解析成 dict list"""
    out = []
    while True:
        try:
            out.append(json.loads(q.get_nowait()))
        except asyncio.QueueEmpty:
            return out


class HelloTest(unittest.TestCase):
    def test_hello_registers_device(self):
        hub = TelemetryHub()
        device, seq = hub.device_hello(hello_msg())
        self.assertEqual(device, "pi-01")
        st = hub.devices["pi-01"]
        self.assertTrue(st.online)
        self.assertEqual(st.patient, "TEST001")
        self.assertEqual(st.conn_seq, seq)

    def test_hello_broadcasts_link_online(self):
        hub = TelemetryHub()
        q = hub.add_viewer()
        hub.device_hello(hello_msg())
        links = [m for m in drain(q) if m["type"] == "link"]
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0]["online"])
        self.assertEqual(links[0]["patient"], "TEST001")


class ConnectionMetaTest(unittest.TestCase):
    """連線時刻與 Pi 軟體版本（PROTOCOL.md「裝置連線時刻與軟體版本」）"""

    def test_hello_records_connected_at_and_version(self):
        hub = TelemetryHub()
        before = time.time()
        hub.device_hello(dict(hello_msg(), app_version="1.2.3"))
        st = hub.devices["pi-01"]
        self.assertGreaterEqual(st.connected_at, before)
        self.assertEqual(st.app_version, "1.2.3")

    def test_missing_app_version_is_empty_string(self):
        """舊版 Pi 不送 app_version：必須是空字串而非 None——
        前端直接把它塞進畫面，None 會顯示成 "null" """
        hub = TelemetryHub()
        hub.device_hello(hello_msg())
        self.assertEqual(hub.devices["pi-01"].app_version, "")

    def test_snapshot_carries_connection_meta(self):
        hub = TelemetryHub()
        hub.device_hello(dict(hello_msg(), app_version="1.2.3"))
        dev = hub.snapshot()["devices"][0]
        self.assertEqual(dev["app_version"], "1.2.3")
        self.assertEqual(dev["connected_at"], hub.devices["pi-01"].connected_at)

    def test_link_online_carries_connection_meta(self):
        hub = TelemetryHub()
        q = hub.add_viewer()
        hub.device_hello(dict(hello_msg(), app_version="1.2.3"))
        link = [m for m in drain(q) if m["type"] == "link"][0]
        self.assertEqual(link["app_version"], "1.2.3")
        self.assertGreater(link["connected_at"], 0)

    def test_recovery_from_false_offline_keeps_connected_at(self):
        """watchdog 誤判離線後資料又進來 → 回復上線，但 connected_at 不得重設：
        TCP 連線從未中斷，重設會讓連線時長歸零而失真"""
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        st = hub.devices["pi-01"]
        original = st.connected_at
        st.online = False                       # 模擬 watchdog 逾時判離線
        hub.device_message("pi-01", seq, {"type": "params", "mode": "VC-SIMV"})
        self.assertTrue(st.online)
        self.assertEqual(st.connected_at, original)

    def test_reconnect_updates_connected_at(self):
        """真正重連（收到新的 hello）才重設連線起點"""
        hub = TelemetryHub()
        hub.device_hello(hello_msg())
        first = hub.devices["pi-01"].connected_at
        time.sleep(0.02)
        hub.device_hello(hello_msg())
        self.assertGreater(hub.devices["pi-01"].connected_at, first)


class ConnSeqTest(unittest.TestCase):
    """連線世代：同裝置重連後，舊 TCP 連線的殘留事件必須全部失效"""

    def test_reconnect_gets_new_seq(self):
        hub = TelemetryHub()
        _, seq1 = hub.device_hello(hello_msg())
        _, seq2 = hub.device_hello(hello_msg())
        self.assertNotEqual(seq1, seq2)

    def test_stale_message_ignored(self):
        hub = TelemetryHub()
        _, seq1 = hub.device_hello(hello_msg())
        hub.device_hello(hello_msg())               # 新連線取代
        hub.device_message("pi-01", seq1, {"type": "params", "mode": "VC-SIMV"})
        self.assertNotIn("params", hub.devices["pi-01"].latest)

    def test_stale_disconnect_does_not_mark_offline(self):
        hub = TelemetryHub()
        _, seq1 = hub.device_hello(hello_msg())
        hub.device_hello(hello_msg())               # 新連線取代
        hub.device_disconnected("pi-01", seq1)      # 舊連線的斷線事件
        self.assertTrue(hub.devices["pi-01"].online)

    def test_current_disconnect_marks_offline(self):
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        q = hub.add_viewer()
        hub.device_disconnected("pi-01", seq)
        self.assertFalse(hub.devices["pi-01"].online)
        links = [m for m in drain(q) if m["type"] == "link"]
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]["online"])

    def test_disconnect_before_hello_is_noop(self):
        hub = TelemetryHub()
        hub.device_disconnected(None, None)         # 不得拋例外


class MaxDevicesTest(unittest.TestCase):
    def test_new_device_beyond_cap_rejected(self):
        hub = TelemetryHub(max_devices=2)
        self.assertIsNotNone(hub.device_hello(hello_msg("pi-01")))
        self.assertIsNotNone(hub.device_hello(hello_msg("pi-02")))
        self.assertIsNone(hub.device_hello(hello_msg("pi-03")))
        self.assertNotIn("pi-03", hub.devices)

    def test_existing_device_reconnect_at_cap_accepted(self):
        hub = TelemetryHub(max_devices=2)
        hub.device_hello(hello_msg("pi-01"))
        hub.device_hello(hello_msg("pi-02"))
        self.assertIsNotNone(hub.device_hello(hello_msg("pi-01")))


class MessageTest(unittest.TestCase):
    def test_stateful_message_stored_and_broadcast(self):
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        q = hub.add_viewer()
        hub.device_message("pi-01", seq, {"type": "params", "mode": "VC-SIMV"})
        self.assertEqual(hub.devices["pi-01"].latest["params"]["mode"], "VC-SIMV")
        sent = [m for m in drain(q) if m["type"] == "params"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["device"], "pi-01")   # 轉發時附上來源

    def test_wave_broadcast_but_not_stored(self):
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        q = hub.add_viewer()
        hub.device_message("pi-01", seq, {"type": "wave", "p": [1], "f": [2], "v": [3]})
        self.assertNotIn("wave", hub.devices["pi-01"].latest)
        self.assertEqual(len([m for m in drain(q) if m["type"] == "wave"]), 1)

    def test_unknown_type_ignored(self):
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        q = hub.add_viewer()
        hub.device_message("pi-01", seq, {"type": "surprise"})
        self.assertEqual(drain(q), [])
        self.assertNotIn("surprise", hub.devices["pi-01"].latest)

    def test_ping_updates_last_seen_only(self):
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        hub.devices["pi-01"].last_seen = 0.0
        q = hub.add_viewer()
        hub.device_message("pi-01", seq, {"type": "ping"})
        self.assertGreater(hub.devices["pi-01"].last_seen, 0.0)
        self.assertEqual(drain(q), [])              # 心跳不轉發

    def test_message_revives_watchdog_false_positive(self):
        """watchdog 誤判離線後資料又進來 → 回復上線並廣播 link"""
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        hub.devices["pi-01"].online = False
        q = hub.add_viewer()
        hub.device_message("pi-01", seq, {"type": "params", "mode": "VC-SIMV"})
        self.assertTrue(hub.devices["pi-01"].online)
        links = [m for m in drain(q) if m["type"] == "link"]
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0]["online"])


class WatchdogTest(unittest.TestCase):
    def test_stale_device_marked_offline(self):
        async def run():
            hub = TelemetryHub(offline_timeout=0.5)
            hub.device_hello(hello_msg())
            hub.devices["pi-01"].last_seen = time.time() - 10   # 假裝很久沒資料
            q = hub.add_viewer()
            drain(q)                                            # 清掉 hello 的 link
            task = asyncio.ensure_future(hub.watchdog())
            await asyncio.sleep(1.2)                # watchdog 每 1 秒檢查一次
            task.cancel()
            return hub.devices["pi-01"].online, drain(q)
        online, msgs = asyncio.run(run())
        self.assertFalse(online)
        links = [m for m in msgs if m["type"] == "link"]
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]["online"])


class BroadcastTest(unittest.TestCase):
    def test_full_queue_drops_oldest_never_blocks(self):
        hub = TelemetryHub()
        q = hub.add_viewer()
        cap = q.maxsize
        for i in range(cap + 5):                    # 超出容量 5 則
            hub.broadcast({"type": "wave", "i": i})
        self.assertEqual(q.qsize(), cap)            # 不超載、不阻塞
        msgs = drain(q)
        self.assertEqual(msgs[0]["i"], 5)           # 最舊的 5 則被丟掉
        self.assertEqual(msgs[-1]["i"], cap + 4)    # 最新的保住


class AlarmLogWiringTest(unittest.TestCase):
    """確認 Hub 真的有把 alarm 訊息接到 AlarmLog（邏輯本身測試見 test_alarm_log.py）"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rm_hub_alarmlog_test_")
        self.hubs = []

    def make_hub(self):
        hub = TelemetryHub(alarm_db_path=os.path.join(self.dir, "alarm.sqlite3"))
        self.hubs.append(hub)
        return hub

    def tearDown(self):
        for hub in self.hubs:
            hub.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_alarm_message_reaches_alarm_log(self):
        hub = self.make_hub()
        _, seq = hub.device_hello(hello_msg())
        hub.device_message("pi-01", seq, {"type": "alarm", "alarms": [
            {"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}],
            "ts": 1_700_000_000})
        rows = hub.alarm_history("pi-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["start_reason"], "observed_active")
        self.assertIsNotNone(rows[0]["source_started_at"])
        self.assertIn("episode_id,device_id", hub.alarm_history_csv("pi-01"))

    def test_remove_device_forgets_active_alarms(self):
        hub = self.make_hub()
        device, seq = hub.device_hello(hello_msg())
        hub.device_message(device, seq, {"type": "alarm", "alarms": [
            {"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}]})
        hub.device_disconnected(device, seq)
        hub.remove_device(device)
        # 裝置重新連上、同一顆警報再送一次 → 建立新 episode；上一段保持結束不明。
        device2, seq2 = hub.device_hello(hello_msg())
        hub.device_message(device2, seq2, {"type": "alarm", "alarms": [
            {"cp": 1, "code": "10", "prio": 28, "text": "PAW HIGH"}]})
        rows = hub.alarm_history("pi-01")
        self.assertEqual([row["status"] for row in rows], ["active", "unknown"])
        self.assertEqual(rows[1]["end_reason"], "device_offline")


class SysLogWiringTest(unittest.TestCase):
    """確認 Hub 會節流寫入 SysLog，下載時才產生 CSV。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rm_hub_syslog_test_")
        self.db_path = os.path.join(self.dir, "sys.sqlite3")
        self.hub = TelemetryHub(sys_db_path=self.db_path,
                                sys_persist_interval=60.0)

    def tearDown(self):
        self.hub.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_sys_message_reaches_sqlite_and_keeps_csv_shape(self):
        device, seq = self.hub.device_hello(hello_msg())
        msg = {"type": "sys", "ts": 1_700_000_000, "cpu": 12.3,
               "mem": 45.6, "temp": 52.1, "disk_pct": 61.2,
               "disk_free": 10.5, "throttled": "0x0", "uptime": 3600}
        self.hub.device_message(device, seq, msg)
        self.hub.device_message(device, seq, dict(msg, cpu=99.0))

        content = self.hub.sys_history_csv(device)
        self.assertTrue(content.startswith("time,cpu,mem,temp,disk_pct,disk_free,"))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(content)))), 1)


class SnapshotTest(unittest.TestCase):
    def test_snapshot_merges_latest_stateful(self):
        hub = TelemetryHub()
        _, seq = hub.device_hello(hello_msg())
        hub.device_message("pi-01", seq, {"type": "params", "mode": "VC-SIMV"})
        hub.device_message("pi-01", seq, {"type": "status", "state": "connected"})
        snap = hub.snapshot()
        self.assertEqual(snap["type"], "snapshot")
        d = {x["device"]: x for x in snap["devices"]}["pi-01"]
        self.assertEqual(d["patient"], "TEST001")
        self.assertTrue(d["online"])
        self.assertEqual(d["params"]["mode"], "VC-SIMV")
        self.assertEqual(d["status"]["state"], "connected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
