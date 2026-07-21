import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
