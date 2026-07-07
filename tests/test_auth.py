# -*- coding: utf-8 -*-
"""
test_auth — SessionStore / LoginRateLimiter 單元測試（IMPROVEMENT_PLAN.md W-107）
====================================================================================
鎖住 session 絕對逾時、定期掃除、總量上限淘汰，與登入鎖定雙鍵計數
（同帳號跨 IP 各自累計、同 IP 對多帳號亂猜另有總量上限）這幾個
最容易在未來改動時弄壞的邏輯。

用假時鐘（mock time.time）讓逾時測試不必真的等待、也不會因執行環境
變慢而變得不穩定。用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.web.auth import FAIL_MAX, FAIL_MAX_IP, LoginRateLimiter, SessionStore


def fake_clock(start: float = 1000.0):
    """回傳 (patcher, tick) — tick(n) 把假時鐘往前推 n 秒"""
    t = [start]
    patcher = mock.patch("monitor.web.auth.time.time", side_effect=lambda: t[0])

    def tick(n):
        t[0] += n
    return patcher, tick


class SessionIdleExpiryTest(unittest.TestCase):
    def test_expires_after_idle_window(self):
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=1.0)   # 60 秒閒置逾時
            token = store.create("alice", "viewer")
            tick(61)
            self.assertIsNone(store.get(token))

    def test_sliding_resets_on_each_access(self):
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=1.0)
            token = store.create("alice", "viewer")
            tick(50)
            self.assertIsNotNone(store.get(token))    # 展延閒置計時
            tick(50)                                  # 距上次 get() 只過 50 秒，未逾 60
            self.assertIsNotNone(store.get(token))

    def test_zero_idle_means_never_expire(self):
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=0)
            token = store.create("alice", "viewer")
            tick(10_000_000)
            self.assertIsNotNone(store.get(token))


class SessionAbsoluteExpiryTest(unittest.TestCase):
    def test_expires_even_with_continuous_activity(self):
        patcher, tick = fake_clock()
        with patcher:
            # 閒置逾時開很寬（1 小時），絕對逾時很短（100 秒）
            store = SessionStore(idle_minutes=60.0, absolute_hours=100 / 3600)
            token = store.create("alice", "viewer")
            tick(50)
            self.assertIsNotNone(store.get(token))    # 50s，尚未超過絕對逾時
            tick(60)                                  # 累計 110s > 100s 絕對逾時
            self.assertIsNone(store.get(token))

    def test_zero_absolute_means_never_expire(self):
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=0, absolute_hours=0)
            token = store.create("alice", "viewer")
            tick(10_000_000)
            self.assertIsNotNone(store.get(token))


class SessionSweepTest(unittest.TestCase):
    def test_sweep_removes_expired_without_being_accessed(self):
        """get() 只在被存取時才發現過期；sweep() 要能主動清掉沒人碰的舊 session"""
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=1.0)
            store.create("alice", "viewer")
            store.create("bob", "viewer")
            tick(61)
            removed = store.sweep()
        self.assertEqual(removed, 2)

    def test_sweep_keeps_still_valid_sessions(self):
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=1.0)
            token = store.create("alice", "viewer")
            tick(30)
            removed = store.sweep()
            self.assertEqual(removed, 0)
            self.assertIsNotNone(store.get(token))


class SessionMaxTest(unittest.TestCase):
    def test_exceeding_max_evicts_oldest(self):
        patcher, tick = fake_clock()
        with patcher:
            store = SessionStore(idle_minutes=0, max_sessions=2)
            oldest = store.create("a", "viewer")
            tick(1)
            middle = store.create("b", "viewer")
            tick(1)
            newest = store.create("c", "viewer")   # 超過上限 2，應淘汰最舊的一筆
        self.assertIsNone(store.get(oldest))
        self.assertIsNotNone(store.get(middle))
        self.assertIsNotNone(store.get(newest))


class LoginRateLimiterTest(unittest.TestCase):
    def test_pair_blocked_after_fail_max(self):
        lim = LoginRateLimiter()
        for _ in range(FAIL_MAX):
            self.assertFalse(lim.blocked("1.2.3.4", "alice"))
            lim.fail("1.2.3.4", "alice")
        self.assertTrue(lim.blocked("1.2.3.4", "alice"))

    def test_different_username_same_ip_not_blocked_by_pair_limit(self):
        """同一 IP 但不同帳號：不該因為 alice 被鎖就連 bob 也鎖住
        （F-08：避免共用電腦/出口 IP 時一人鎖密碼害到其他人）"""
        lim = LoginRateLimiter()
        for _ in range(FAIL_MAX):
            lim.fail("1.2.3.4", "alice")
        self.assertTrue(lim.blocked("1.2.3.4", "alice"))
        self.assertFalse(lim.blocked("1.2.3.4", "bob"))

    def test_same_ip_many_usernames_blocked_by_ip_total(self):
        """同一 IP 對很多不同帳號亂猜：仍要被總量上限擋下"""
        lim = LoginRateLimiter()
        for i in range(FAIL_MAX_IP):
            lim.fail("1.2.3.4", f"user{i}")
        self.assertTrue(lim.blocked("1.2.3.4", "yet-another-user"))

    def test_different_ip_not_affected(self):
        lim = LoginRateLimiter()
        for _ in range(FAIL_MAX):
            lim.fail("1.2.3.4", "alice")
        self.assertFalse(lim.blocked("5.6.7.8", "alice"))

    def test_clear_only_clears_that_pair(self):
        lim = LoginRateLimiter()
        for _ in range(FAIL_MAX):
            lim.fail("1.2.3.4", "alice")
        lim.clear("1.2.3.4", "alice")
        self.assertFalse(lim.blocked("1.2.3.4", "alice"))


if __name__ == "__main__":
    unittest.main()
