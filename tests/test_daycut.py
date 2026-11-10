"""过 0 点保护：一次执行只用开始那天的次数。"""

import unittest
from collections import Counter
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

from zmxy import core, daycut, dungeon, protocol as p, quota
from zmxy.servertime import CN


def at(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())


class ReasonTests(unittest.TestCase):
    def test_same_day_passes_and_next_day_stops(self):
        s = SimpleNamespace(day=date(2026, 11, 8), now=at("2026-11-08 23:59:30"))
        self.assertIsNone(daycut.reason(s))
        s.now = at("2026-11-09 00:00:01")
        why = daycut.reason(s)
        self.assertIn("已过 0 点", why)
        self.assertIn("11/08", why)
        self.assertIn("11/09", why)

    def test_chest_margin_stops_just_before_midnight(self):
        s = SimpleNamespace(day=date(2026, 11, 8), now=at("2026-11-08 23:59:56"))
        self.assertIn("快到 0 点", daycut.reason(s, daycut.CHEST_MARGIN))
        s.now = at("2026-11-08 23:59:50")
        self.assertIsNone(daycut.reason(s, daycut.CHEST_MARGIN))

    def test_unknown_day_or_time_never_blocks(self):
        self.assertIsNone(daycut.reason(SimpleNamespace(day=None, now=at("2026-11-09 00:00:01"))))
        self.assertIsNone(daycut.reason(SimpleNamespace(day=date(2026, 11, 8), now=None)))
        self.assertIsNone(daycut.reason(object()))


class RunnerTests(unittest.TestCase):
    clock = {"now": 0}

    class FakeSession:
        def __init__(self, *a, **k):
            self.carry = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def bind(self, spec):
            pass

        @property
        def now(self):
            return RunnerTests.clock["now"]

    def run_tasks(self, specs, day=None):
        registry = {sp.id: sp for sp in specs}
        acc = core.Account("a", p.ClientConfig(uid=1, username="u"), tuple(registry))
        with mock.patch("zmxy.session.Session", self.FakeSession), mock.patch("zmxy.runner.load_tasks", return_value=registry):
            return core.run_account(acc, day=day)

    def test_tasks_after_midnight_are_skipped_except_overnight(self):
        self.clock["now"] = at("2026-11-08 23:59:50")
        calls = []

        def cross(ctx):
            calls.append("t0")
            self.clock["now"] = at("2026-11-09 00:00:05")  # 做第一个任务时过了 0 点
            return core.TaskResult("t0", "submitted")

        specs = [core.TaskSpec("t0", "t0", cross),
                 core.TaskSpec("t1", "t1", lambda ctx: calls.append("t1") or core.TaskResult("t1", "submitted")),
                 core.TaskSpec("t2", "t2", lambda ctx: calls.append("t2") or core.TaskResult("t2", "submitted"), overnight=True)]
        out = {r.task: r for r in self.run_tasks(specs)}
        self.assertEqual(calls, ["t0", "t2"])
        self.assertEqual(out["t1"].status, "skipped")
        self.assertIn("已过 0 点", out["t1"].detail)

    def test_later_saves_keep_the_day_the_execution_started(self):
        self.clock["now"] = at("2026-11-09 00:00:05")  # 这个存档 0 点后才登录，但这次执行是 11/08 开始的
        day = [date(2026, 11, 8)]
        out = self.run_tasks([core.TaskSpec("t1", "t1", lambda ctx: core.TaskResult("t1", "submitted"))], day=day)
        self.assertEqual(out[0].status, "skipped")

    def test_first_login_fills_the_shared_day(self):
        self.clock["now"] = at("2026-11-08 12:00:00")
        day = [None]
        self.run_tasks([core.TaskSpec("t1", "t1", lambda ctx: core.TaskResult("t1", "submitted"))], day=day)
        self.assertEqual(day, [date(2026, 11, 8)])


class RunLevelTests(unittest.TestCase):
    def run_level(self, kind, start, step):
        session = SimpleNamespace(day=date(2026, 11, 8), now=at(start), spent=Counter())
        ctx = SimpleNamespace(session=session, info={}, stopped=False, log=lambda *a: None)
        lv = SimpleNamespace(mid=1, kind=kind, name="关")
        made = []

        def attempt(*a, **k):
            made.append(session.now)
            session.now += step

        with mock.patch.object(dungeon, "_lock_reason", return_value=None), \
             mock.patch.object(dungeon.quota, "of", return_value=quota.Quota(("k",), 10)), \
             mock.patch.object(dungeon, "first_card_is_free", return_value=True), \
             mock.patch.object(dungeon, "_attempt", side_effect=attempt):
            done, why = dungeon.run_level(ctx, lv, 10, dungeon.Gains())
        return done, why, made

    def test_elite_stops_at_midnight_not_before(self):
        done, why, made = self.run_level(dungeon.ELITE, "2026-11-08 23:59:40", 15)
        self.assertEqual(done, 2)  # 23:59:40、23:59:55 进去的算当天；00:00:10 不再进
        self.assertIn("已过 0 点", why)

    def test_nightmare_keeps_a_margin_before_midnight(self):
        done, why, made = self.run_level(dungeon.NIGHTMARE, "2026-11-08 23:59:47", 4)
        self.assertEqual(done, 2)  # 23:59:47、23:59:51 开始；23:59:55 离 0 点只剩 5 秒，不再开
        self.assertIn("快到 0 点", why)


if __name__ == "__main__":
    unittest.main()
