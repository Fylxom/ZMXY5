"""每日开放时段（state.in_windows）：只认“时:分:秒”，读不懂就返回 None，由任务停下提示。"""

import unittest
from datetime import datetime, timedelta, timezone

from zmxy import state

CN = timezone(timedelta(hours=8))


def at(h, m):
    return int(datetime(2026, 11, 8, h, m, tzinfo=CN).timestamp())


class WindowTests(unittest.TestCase):
    def test_full_time_windows(self):
        text = "12:00:00-14:00:00;19:00:00-21:00:00"
        self.assertTrue(state.in_windows(text, at(13, 0)))
        self.assertTrue(state.in_windows(text, at(20, 59)))
        self.assertFalse(state.in_windows(text, at(15, 0)))
        self.assertTrue(state.in_windows("9:00:00-21:00:00", at(9, 30)))  # 一位数的时也认

    def test_window_without_seconds_is_unreadable_not_shifted(self):
        self.assertIsNone(state.in_windows("12:00-14:00", at(13, 0)))
        self.assertIsNone(state.in_windows("12:00:00-14:00", at(13, 0)))
        self.assertIsNone(state.in_windows("12:70:00-14:00:00", at(13, 0)))


if __name__ == "__main__":
    unittest.main()
