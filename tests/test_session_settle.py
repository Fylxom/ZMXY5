"""按开放时段的定时：刚打开 / 改设置时已经开场过的这一场不补做；“都做”时前一场做满了后面不登录；只选中午不补晚上。
斗神大会的开放时段读数据表快照：每天 12:00–14:00、19:00–21:00。"""

import unittest
from datetime import datetime

from zmxy import timeplan

TID = "pvp_match"
DAY = "2026-10-08"


def at(h: int, m: int) -> datetime:
    return datetime(2026, 10, 8, h, m)


def cfg(session: str = "both", delay: int = 5) -> dict:
    return {"on": True, "session": session, "delay": delay}


class SessionSettleTest(unittest.TestCase):
    def test_switch_on_mid_session_waits_for_next(self) -> None:
        c = cfg()
        timeplan.settle(TID, c, at(12, 30))  # 中午场开场 30 分钟后才打开
        self.assertFalse(timeplan.due(TID, c, at(12, 31)))
        self.assertTrue(timeplan.due(TID, c, at(19, 5)))  # 晚上场照常

    def test_switch_on_before_delay_still_runs(self) -> None:
        c = cfg()
        timeplan.settle(TID, c, at(12, 2))  # 开场后 5 分钟还没到
        self.assertTrue(timeplan.due(TID, c, at(12, 5)))

    def test_raise_delay_after_skip_runs_today(self) -> None:
        c = cfg()
        timeplan.settle(TID, c, at(12, 30))
        c["delay"] = 60  # 改成开场后 60 分钟：这一场还没到点
        timeplan.unskip(TID, c, at(12, 30))
        timeplan.settle(TID, c, at(12, 30))
        self.assertFalse(timeplan.due(TID, c, at(12, 59)))
        self.assertTrue(timeplan.due(TID, c, at(13, 0)))

    def test_change_after_real_run_does_not_redo(self) -> None:
        c = cfg()
        self.assertTrue(timeplan.due(TID, c, at(12, 5)))  # 真做了
        c["delay"] = 60
        timeplan.unskip(TID, c, at(12, 6))
        timeplan.settle(TID, c, at(12, 6))
        self.assertFalse(timeplan.due(TID, c, at(13, 0)))

    def test_noon_only_never_catches_up_in_evening(self) -> None:
        c = cfg("noon")
        self.assertFalse(timeplan.due(TID, c, at(19, 30)))  # 中午场错过了，晚上不补
        self.assertIsNone(timeplan.schedule_retry(TID, c, "u#1", at(19, 30)))

    def test_full_is_remembered_for_the_day(self) -> None:
        c = cfg()
        self.assertTrue(timeplan.due(TID, c, at(12, 5)))
        timeplan.mark_full(c, DAY, "u#1")
        self.assertEqual(timeplan.full(c, DAY), {"u#1"})
        self.assertTrue(timeplan.due(TID, c, at(19, 5)))  # 晚上场照常到点，排队时跳过做满的存档
        self.assertEqual(timeplan.full(c, DAY), {"u#1"})  # 到点不会冲掉做满的记录
        self.assertEqual(timeplan.full(c, "2026-10-09"), set())


if __name__ == "__main__":
    unittest.main()


class ItemDueTest(unittest.TestCase):
    """定时执行端：任务回报做满 → 记下；下一场排队时跳过它，写一句说明。"""

    def test_full_save_is_not_queued(self) -> None:
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from zmxy import core, globalcfg, servertime
        from zmxy.mainwin.timers import TimersMixin
        from zmxy.store import Store

        st = Store(Path(tempfile.mkdtemp()) / "s.json")
        acc = {"username": "u", "password": "p"}
        st.accounts.append(acc)
        globalcfg.section(st, TID).update(cfg())
        globalcfg.entry(st, TID, "u")["slots"] = [1, 2]
        notes = []
        w = SimpleNamespace(store=st, timed_items={"账号 · 存档 1": (acc, 1, TID)}, _title=lambda t: t,
                            _timed_note=lambda *a: notes.append(a))
        with mock.patch.object(servertime, "clock", return_value=at(12, 30)):
            TimersMixin._timed_full(w, "账号 · 存档 1", core.TaskResult(TID, core.SUBMITTED, "", day_done=True))
            TimersMixin._timed_full(w, "账号 · 存档 1", core.TaskResult(TID, core.SUBMITTED, ""))  # 没做满的不记
            queued = TimersMixin._item_due(w, TID)
        self.assertEqual([slot for _a, slot, _t in queued], [2])
        self.assertEqual(len(notes), 1)
        self.assertIn("这一场不登录", notes[0][2])
