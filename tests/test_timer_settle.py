"""打开定时 / 改时间时，今天已经过了的时间不补做；关机错过的照常补做。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from zmxy import globalcfg as g
from zmxy.store import Store

NOW = datetime(2026, 11, 10, 15, 4)


def store(on: bool) -> Store:
    st = Store(Path(tempfile.mkdtemp()) / "s.json")
    st.accounts.append({"username": "u", "password": "p"})
    g.section(st, g.DAILY).update(on=on, at="12:00")
    g.entry(st, g.DAILY, "u")["slots"] = [1]
    return st


class SettleTest(unittest.TestCase):
    def test_switch_on_after_time_waits_for_tomorrow(self) -> None:
        st = store(True)
        g.settle(st, g.DAILY, NOW)
        self.assertEqual(g.due(st, NOW), [])
        self.assertEqual(g.due(st, datetime(2026, 11, 11, 12, 0)), [g.DAILY])

    def test_later_time_still_runs_today(self) -> None:
        st = store(True)
        g.section(st, g.DAILY)["at"] = "18:00"
        g.settle(st, g.DAILY, NOW)
        self.assertEqual(g.due(st, NOW), [])
        self.assertEqual(g.due(st, NOW.replace(hour=18)), [g.DAILY])

    def test_missed_while_off_still_catches_up(self) -> None:
        self.assertEqual(g.due(store(True), NOW), [g.DAILY])  # 设置早就开着，没走 settle：关机错过开机补做

    def test_clock_key_changes_when_armed(self) -> None:
        st = store(False)
        before = g.clock_key(st, g.DAILY, NOW)
        g.section(st, g.DAILY)["on"] = True
        self.assertNotEqual(before, g.clock_key(st, g.DAILY, NOW))

    def _type(self, st: Store, clocks: tuple[str, ...], now: datetime) -> None:
        """照时间框一个数字一个数字敲：每敲一下都改一次时间、settle 一次。"""
        for clock in clocks:
            g.retime(st, g.DAILY, clock, now)
            g.settle(st, g.DAILY, now)

    def test_retime_after_run_waits_for_tomorrow(self) -> None:
        st = store(True)
        noon = datetime(2026, 11, 10, 12, 0, 5)
        self.assertEqual(g.due(st, noon), [g.DAILY])  # 12:00 真做了
        self._type(st, ("00:01", "00:12", "01:20", "12:01"), noon)  # 12:00 当场改成 12:01
        self.assertEqual(g.due(st, datetime(2026, 11, 10, 12, 1, 5)), [])
        self.assertEqual(g.due(st, datetime(2026, 11, 11, 12, 1)), [g.DAILY])

    def test_retime_after_skip_runs_today(self) -> None:
        st = store(True)
        g.settle(st, g.DAILY, NOW)  # 15:04 打开，12:00 已过：跳过
        self._type(st, ("00:01", "00:18", "01:80", "18:00"), NOW)
        self.assertEqual(g.due(st, NOW), [])
        self.assertEqual(g.due(st, NOW.replace(hour=18)), [g.DAILY])


if __name__ == "__main__":
    unittest.main()
