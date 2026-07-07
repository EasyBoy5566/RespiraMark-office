# -*- coding: utf-8 -*-
"""
test_ingest_device_mode — ingest.py 與 DeviceRegistry 的實際接線測試
======================================================================
test_device_auth.py 只測 DeviceRegistry 本身邏輯；這裡測 ingest.py 的
handle_ingest() 真的有正確呼叫它並依結果斷線/放行——包含每台獨立 token
模式（devices.json 存在）與退回模式（devices.json 不存在時用 ingest_token）。

用真正的 asyncio TCP server（bind port 0，OS 自動配發，不佔用固定 port），
純標準庫 + TelemetryHub，不需要 TLS/憑證/帳號，比 smoke_test.py 快很多。

用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.crypto import hash_password
from monitor.domain.hub import TelemetryHub
from monitor.transport.device_auth import DeviceRegistry
from monitor.transport.ingest import start_ingest


async def try_hello(port: int, device: str, token: str) -> bool:
    """送出 hello，回傳是否被伺服器斷線（True=被拒絕、False=驗證通過仍存活）"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    line = json.dumps({"type": "hello", "v": 1, "device": device,
                       "patient": "T", "token": token}) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()
    try:
        data = await asyncio.wait_for(reader.read(1), timeout=1.0)
        closed = data == b""
    except asyncio.TimeoutError:
        closed = False
    writer.close()
    return closed


class DeviceTokenModeTest(unittest.IsolatedAsyncioTestCase):
    """devices.json 存在：每台裝置獨立 token"""

    async def asyncSetUp(self):
        fd, self.devices_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.devices_path, "w", encoding="utf-8") as f:
            json.dump({"devices": [
                {"device_id": "pi-a", "enabled": True,
                 "token_hash": hash_password("token-a")},
                {"device_id": "pi-b", "enabled": False,
                 "token_hash": hash_password("token-b")},
            ]}, f)
        registry = DeviceRegistry(self.devices_path)
        self.hub = TelemetryHub()
        self.server = await start_ingest(self.hub, 0, devices=registry,
                                         hello_timeout=2.0, idle_timeout=2.0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        try:
            os.remove(self.devices_path)
        except OSError:
            pass

    async def test_correct_device_token_accepted(self):
        self.assertFalse(await try_hello(self.port, "pi-a", "token-a"))

    async def test_wrong_token_rejected(self):
        self.assertTrue(await try_hello(self.port, "pi-a", "wrong"))

    async def test_impersonation_with_other_devices_token_rejected(self):
        self.assertTrue(await try_hello(self.port, "pi-b", "token-a"))

    async def test_disabled_device_rejected_even_with_correct_token(self):
        self.assertTrue(await try_hello(self.port, "pi-b", "token-b"))

    async def test_unknown_device_rejected(self):
        self.assertTrue(await try_hello(self.port, "pi-unknown", "token-a"))


class LegacySharedTokenFallbackTest(unittest.IsolatedAsyncioTestCase):
    """devices.json 不存在：退回單一共用 ingest_token（向後相容）"""

    async def asyncSetUp(self):
        missing_registry = DeviceRegistry(os.path.join(
            tempfile.gettempdir(), "respiramark_test_no_such_devices.json"))
        self.assertFalse(missing_registry.exists())   # 前提假設
        self.hub = TelemetryHub()
        self.server = await start_ingest(self.hub, 0, token="LEGACYTOK",
                                         devices=missing_registry,
                                         hello_timeout=2.0, idle_timeout=2.0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_correct_shared_token_accepted(self):
        self.assertFalse(await try_hello(self.port, "any-device", "LEGACYTOK"))

    async def test_wrong_shared_token_rejected(self):
        self.assertTrue(await try_hello(self.port, "any-device", "WRONG"))


if __name__ == "__main__":
    unittest.main()
