import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from monitor.crypto import hash_password
from monitor.transport.device_auth import DeviceRegistry
from tools import fake_pi


class FakePiAlarmProbabilityTest(unittest.TestCase):
    @patch("tools.fake_pi.random.random", return_value=0.029)
    def test_triggers_below_probability(self, _random):
        self.assertTrue(fake_pi.should_trigger_alarm(0.03))

    @patch("tools.fake_pi.random.random", return_value=0.03)
    def test_does_not_trigger_at_probability_boundary(self, _random):
        self.assertFalse(fake_pi.should_trigger_alarm(0.03))

    @patch("tools.fake_pi.random.random", return_value=0.99)
    def test_force_bypasses_probability_for_smoke_test(self, random_mock):
        self.assertTrue(fake_pi.should_trigger_alarm(0.0, force=True))
        random_mock.assert_not_called()


class FakePiLocalRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "devices.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, devices):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"devices": devices}, f)

    @staticmethod
    def _args(device="fake-01", host="127.0.0.1"):
        return SimpleNamespace(device=device, host=host, token="")

    def test_registers_token_that_server_accepts_without_plaintext_on_disk(self):
        self._write([])
        args = self._args()

        fake_pi.register_local_devices([args], self.path)

        self.assertTrue(args.token)
        self.assertTrue(DeviceRegistry(self.path).verify("fake-01", args.token))
        with open(self.path, "r", encoding="utf-8") as f:
            saved = f.read()
        self.assertNotIn(args.token, saved)
        self.assertIn(fake_pi.AUTO_REGISTER_NOTE, saved)

    def test_multiple_fake_devices_share_one_hash_calculation(self):
        self._write([])
        args1 = self._args(device="MI-1")
        args2 = self._args(device="MI-2")

        with patch("tools.fake_pi.hash_password",
                   wraps=hash_password) as hash_mock:
            fake_pi.register_local_devices([args1, args2], self.path)

        self.assertEqual(hash_mock.call_count, 1)
        self.assertEqual(args1.token, args2.token)
        registry = DeviceRegistry(self.path)
        self.assertTrue(registry.verify("MI-1", args1.token))
        self.assertTrue(registry.verify("MI-2", args2.token))

    def test_preserves_unrelated_existing_device(self):
        self._write([{
            "device_id": "rt01",
            "note": "正式裝置",
            "enabled": True,
            "token_hash": hash_password("real-token"),
        }])
        args = self._args()

        fake_pi.register_local_devices([args], self.path)

        registry = DeviceRegistry(self.path)
        self.assertTrue(registry.verify("rt01", "real-token"))
        self.assertTrue(registry.verify("fake-01", args.token))

    def test_refuses_to_overwrite_non_fake_existing_device(self):
        self._write([{
            "device_id": "rt01",
            "note": "正式裝置",
            "enabled": True,
            "token_hash": hash_password("real-token"),
        }])
        args = self._args(device="rt01")

        with self.assertRaisesRegex(ValueError, "拒絕覆寫"):
            fake_pi.register_local_devices([args], self.path)
        self.assertEqual(args.token, "")
        self.assertTrue(DeviceRegistry(self.path).verify("rt01", "real-token"))

    def test_refuses_remote_host(self):
        self._write([])
        args = self._args(host="192.168.0.50")

        with self.assertRaisesRegex(ValueError, "只允許"):
            fake_pi.register_local_devices([args], self.path)

    def test_refuses_to_create_registry_implicitly(self):
        args = self._args()

        with self.assertRaisesRegex(ValueError, "找不到裝置權杖檔"):
            fake_pi.register_local_devices([args], self.path)

    def test_local_registry_enables_automatic_registration(self):
        self._write([])
        args = self._args()
        args.register_local = False
        args.devices_file = self.path

        self.assertTrue(fake_pi.should_register_local(args))

    def test_manual_token_disables_automatic_registration(self):
        self._write([])
        args = self._args()
        args.token = "manual-token"
        args.register_local = False
        args.devices_file = self.path

        self.assertFalse(fake_pi.should_register_local(args))

    def test_remote_host_does_not_automatically_register(self):
        self._write([])
        args = self._args(host="192.168.0.50")
        args.register_local = False
        args.devices_file = self.path

        self.assertFalse(fake_pi.should_register_local(args))


if __name__ == "__main__":
    unittest.main()
