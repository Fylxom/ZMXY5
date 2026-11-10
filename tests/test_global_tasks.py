"""定时（globalcfg）、服务器时钟（servertime）。"""

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from zmxy import globalcfg, runner, servertime, state
from zmxy.core import BLOCKED, SKIPPED, TaskResult
from zmxy.store import Store
from zmxy.tasks import ancient, level_pick

from tests.support import FakeConn, run

TREASURE = {"dungeonTreasure": {"1400100": {"remainCnt": 9}}}


class ServerClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = servertime.path_override
        servertime.path_override = Path(self.tmp.name) / "server_clock.json"
        servertime.reset()

    def tearDown(self):
        servertime.path_override = self.old
        servertime.reset()
        self.tmp.cleanup()

    def test_unknown_is_local(self):
        self.assertEqual(servertime.offset(), 0.0)
        self.assertFalse(servertime.known())

    def test_learn_persists_offset(self):
        servertime.learn(time.time() + 90)
        self.assertTrue(servertime.known())
        self.assertAlmostEqual(servertime.now() - time.time(), 90, delta=2)
        saved = json.loads(servertime.path_override.read_text(encoding="utf-8"))["offset"]
        self.assertAlmostEqual(saved, 90, delta=2)
        servertime.reset()  # 新进程（Windows 后台唤起）读上次存的
        self.assertAlmostEqual(servertime.offset(), 90, delta=2)
        self.assertTrue(servertime.known())

    def test_clock_is_beijing(self):
        at = datetime(2026, 11, 3, 0, 5, tzinfo=servertime.CN).timestamp()  # 不管本机时区，都按北京时间显示
        self.assertEqual(servertime.clock(at), datetime(2026, 11, 3, 0, 5))

    def test_defer_follows_server_clock(self):
        """联盟贡献 00:15 的顺延按服务器时间：本机慢、服务器已过 00:15 就不再等。"""
        spec = mock.Mock(defer_until=(0, 15))
        self.assertEqual(runner._defer_deadline(spec, datetime(2026, 11, 8, 0, 10)), datetime(2026, 11, 8, 0, 15))
        self.assertIsNone(runner._defer_deadline(spec, datetime(2026, 11, 8, 0, 16)))


class LevelNameTest(unittest.TestCase):
    def test_split_char_joined(self):
        """数据表把“邽”拆成 圭+阝 两个字，显示时合成一个字。"""
        self.assertIn((1400110, "异·邽山"), ancient.catalog())


class GrowthKeepTest(unittest.TestCase):
    def keep(self, reserve):
        with mock.patch.object(state, "growth_reserve", return_value=reserve):
            return ancient.growth_keep(0, None)

    def test_rules(self):
        self.assertEqual(self.keep(None), 0)  # 不在留次数期：全打
        self.assertEqual(self.keep((2, 3)), 0)  # 留给别的养成周的，上古蛮荒不管
        self.assertEqual(self.keep((1, 1)), 2)  # 装备宝石周前 4 天：留 2 次
        self.assertEqual(self.keep((1, 4)), 8)  # 前 1 天：留 8 次，0 点再涨 2 次正好 10 次
        self.assertIsNone(self.keep((1, state.RESERVE_CAP)))  # 开始那天还没更新：一次都不打


class AncientPlanTest(unittest.TestCase):
    def plan(self, plan, keep=0, info=TREASURE):
        seen = {}

        def capture(ctx, *a):
            seen["times"] = ctx.options["times"]
            return TaskResult("ancient_wilderness", "ok", "")
        with mock.patch.object(level_pick, "run_levels", side_effect=capture), \
                mock.patch.object(ancient, "growth_keep", return_value=keep), \
                mock.patch.object(ancient.online, "growth_week", return_value=None):
            result = run("ancient_wilderness", FakeConn(), info, {"levels": [1400101], "plan": plan})
        return seen.get("times"), result

    def test_all_uses_everything(self):
        self.assertEqual(self.plan("all")[0], 9)

    def test_growth_keeps_reserve(self):
        self.assertEqual(self.plan("growth", keep=4)[0], 5)

    def test_growth_waits_for_update(self):
        times, result = self.plan("growth", keep=None)
        self.assertIsNone(times)
        self.assertEqual(result.status, SKIPPED)
        self.assertIn("还没更新", result.detail)

    def test_nothing_left_after_reserve(self):
        times, result = self.plan("growth", keep=10)
        self.assertIsNone(times)
        self.assertEqual(result.status, SKIPPED)

    def test_unreadable_count_blocks(self):
        times, result = self.plan("all", info={})
        self.assertIsNone(times)
        self.assertEqual(result.status, BLOCKED)

    def test_page_options_unchanged(self):
        """没有 plan（日常 / 周常页手动执行）：照页面上的次数。"""
        seen = {}
        with mock.patch.object(level_pick, "run_levels", side_effect=lambda ctx, *a: seen.setdefault("times", ctx.options.get("times"))):
            run("ancient_wilderness", FakeConn(), TREASURE, {"levels": [1400101], "times": 3})
        self.assertEqual(seen["times"], 3)


class GlobalConfigTest(unittest.TestCase):
    """按时刻的定时项（日常和活动、联盟贡献、周常）和选账号 / 存档。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "settings.json")
        self.store.accounts.extend([{"name": "A", "username": "a", "saves": {}}, {"name": "B", "username": "b", "saves": {}},
                                    {"name": "A2", "username": "a", "saves": {}}])  # 同一登录名只算一次

    def tearDown(self):
        self.tmp.cleanup()

    def arm(self, item, at="00:20"):
        globalcfg.section(self.store, item).update(on=True, at=at)

    def users(self, item):
        return [(acc["username"], slots) for acc, slots in globalcfg.targets(self.store, item)]

    def test_targets(self):
        globalcfg.entry(self.store, globalcfg.DAILY, "a")["slots"] = [3, 1, 9, True]  # 9、True 不算
        globalcfg.entry(self.store, globalcfg.DAILY, "b")  # 默认一个都没选
        globalcfg.entry(self.store, globalcfg.ALLIANCE, "b")["on"] = True
        self.assertEqual(self.users(globalcfg.DAILY), [("a", [1, 3])])
        self.assertEqual(self.users(globalcfg.ALLIANCE), [("b", [None])])  # 账号级：存档用页面上它自己选的
        self.assertEqual(globalcfg.entry(self.store, globalcfg.ALLIANCE, "a"), {"on": False})
        self.assertEqual(globalcfg.entry(self.store, "pvp_match", "a"), {"slots": []})

    def test_enabled_needs_switch_and_target(self):
        self.assertFalse(globalcfg.enabled(self.store))
        self.arm(globalcfg.ALLIANCE)
        self.assertFalse(globalcfg.enabled(self.store))  # 没有账号开着
        globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"] = True
        self.assertTrue(globalcfg.enabled(self.store))
        globalcfg.section(self.store, globalcfg.ALLIANCE)["at"] = "25:00"
        self.assertFalse(globalcfg.enabled(self.store))

    def test_due_once_per_day_any_time_after(self):
        self.arm(globalcfg.ALLIANCE)
        globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"] = True
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 8, 0, 19)), [])
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 8, 0, 21)), [globalcfg.ALLIANCE])
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 8, 0, 22)), [])
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 9, 0, 10)), [])  # 新的一天，还没到时间
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 9, 21, 0)), [globalcfg.ALLIANCE])  # 关机错过：晚上开机也补做
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 9, 23, 0)), [])
        globalcfg.unmark(self.store, globalcfg.ALLIANCE)  # 一直没联网没做成：之后还会再到点
        self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 9, 23, 30)), [globalcfg.ALLIANCE])

    def test_weekly_weekdays(self):
        """周常：没选星期、也没开按养成周就不算开着；只在选中的星期到点。"""
        globalcfg.entry(self.store, globalcfg.WEEKLY, "b")["slots"] = [2]
        self.arm(globalcfg.WEEKLY, "06:00")
        self.assertFalse(globalcfg.enabled(self.store))
        globalcfg.section(self.store, globalcfg.WEEKLY)["weekdays"] = [1, 6]  # 周二、周日
        with mock.patch.object(globalcfg, "growth_day", return_value=None):
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 9, 23, 0)), [])  # 周一
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 5, 59)), [])
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 6, 1)), [globalcfg.WEEKLY])
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 7, 0)), [])

    def test_weekly_growth_start_and_end_days(self):
        """开着按养成周：养成周开始那天、最后一天不管选没选都做；新的一周照表开始了但游戏还没更新也算（到点后再等更新）。"""
        globalcfg.entry(self.store, globalcfg.WEEKLY, "a")["slots"] = [1]
        self.arm(globalcfg.WEEKLY, "06:00")
        cfg = globalcfg.section(self.store, globalcfg.WEEKLY)
        cfg["growth"] = True
        self.assertTrue(globalcfg.enabled(self.store))  # 只开按养成周、不选星期也行
        with mock.patch.object(globalcfg, "waiting_update", return_value=False):
            for day, edge, want in ((9, None, []), (10, "start", [globalcfg.WEEKLY]), (11, "end", [globalcfg.WEEKLY])):
                with mock.patch.object(globalcfg, "growth_day", return_value=edge):
                    self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, day, 6, 30)), want)
        with mock.patch.object(globalcfg, "growth_day", return_value=None), mock.patch.object(globalcfg, "waiting_update", return_value=True):
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 12, 6, 30)), [globalcfg.WEEKLY])  # 更新顺延的那天
        cfg["growth"] = False
        with mock.patch.object(globalcfg, "growth_day", return_value="start"):
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 13, 6, 30)), [])  # 没开按养成周：只看星期

    def test_growth_day_from_table(self):
        """照表：上一周结束的第二天是开始那天，这一周表里的结束那天是最后一天。"""
        ends = {1: datetime(2026, 10, 20, 23, 59, 59), 2: datetime(2026, 10, 27, 23, 59, 59),
                3: datetime(2026, 11, 3, 23, 59, 59), 4: datetime(2026, 11, 10, 23, 59, 59)}
        with mock.patch.object(state, "_growth_ends", return_value=ends):
            day = lambda d: globalcfg.growth_day(datetime(2026, 11, d, 12, 0))
            self.assertEqual(day(4), "start")   # 翅膀光环周开始（表已更新到这一周）
            self.assertEqual(day(10), "end")    # 这一周表里的最后一天
            self.assertEqual(day(11), "start")  # 下一周开始（表还没更新）
            self.assertIsNone(day(7))
        with mock.patch.object(state, "_growth_ends", return_value=None):
            self.assertIsNone(globalcfg.growth_day(datetime(2026, 11, 4, 12, 0)))

    def test_check_same_day_only(self):
        globalcfg.entry(self.store, globalcfg.WEEKLY, "a")["slots"] = [1]
        self.arm(globalcfg.WEEKLY, "06:00")
        globalcfg.section(self.store, globalcfg.WEEKLY)["weekdays"] = list(range(7))
        with mock.patch.object(globalcfg, "growth_day", return_value=None):
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 8, 0)), [globalcfg.WEEKLY])
            self.assertEqual(globalcfg.schedule_check(self.store, globalcfg.WEEKLY, datetime(2026, 11, 10, 8, 0)), "08:30")
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 8, 29)), [])
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 8, 30)), [globalcfg.WEEKLY])
            self.assertEqual(globalcfg.due(self.store, datetime(2026, 11, 10, 8, 31)), [])  # 重看过就没了
        self.assertEqual(globalcfg.schedule_check(self.store, globalcfg.WEEKLY, datetime(2026, 11, 10, 0, 15)), "08:00")  # 8 点以前约在 8 点
        self.assertIsNone(globalcfg.schedule_check(self.store, globalcfg.WEEKLY, datetime(2026, 11, 10, 23, 50)))  # 跨天不约

    def test_covers_alliance(self):
        """联盟贡献定时开着的账号，日常和活动定时跳过它的联盟贡献。"""
        self.arm(globalcfg.ALLIANCE)
        globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"] = True
        acc, other = self.store.accounts[0], self.store.accounts[1]
        self.assertTrue(globalcfg.covers(self.store, acc, "alliance"))
        self.assertFalse(globalcfg.covers(self.store, other, "alliance"))
        self.assertFalse(globalcfg.covers(self.store, acc, "vip_gift"))
        globalcfg.section(self.store, globalcfg.ALLIANCE)["on"] = False
        self.assertFalse(globalcfg.covers(self.store, acc, "alliance"))

    def test_weekly_plan_ids(self):
        """周常定时：读到的次数大于 0 的才做，上届奖励没领完才领。"""
        from zmxy.mainwin.timers import WeeklyPlan, weekly_plan_ids
        from zmxy.tasks import last_rewards
        plan = WeeklyPlan({}, [], [], [], False, ["ancient_wilderness", "ten_jue", last_rewards.ID], frozenset(),
                          {2: frozenset({"ancient_wilderness"})})
        value = {"ancient_wilderness": 0, "ten_jue": 0, last_rewards.ID: ("文字", False, {}, 1)}
        self.assertEqual(weekly_plan_ids(plan, 1, value)[0], [])
        value = {"ancient_wilderness": 5, "ten_jue": 2, last_rewards.ID: ("文字", True, {}, 1)}
        ids, times = weekly_plan_ids(plan, 1, value)
        self.assertEqual(sorted(ids), sorted(["ancient_wilderness", "ten_jue", last_rewards.ID]))
        self.assertEqual(times["ten_jue"], 2)
        ids, _ = weekly_plan_ids(plan, 2, value)  # 存档 2 的上古蛮荒归上古蛮荒定时管
        self.assertEqual(sorted(ids), sorted(["ten_jue", last_rewards.ID]))

    def test_covers_ancient(self):
        """上古蛮荒定时勾了的存档，周常定时跳过上古蛮荒；没勾的存档照做。"""
        self.arm(globalcfg.ANCIENT, "12:00")
        globalcfg.entry(self.store, globalcfg.ANCIENT, "a")["slots"] = [2]
        acc = self.store.accounts[0]
        self.assertTrue(globalcfg.covers(self.store, acc, globalcfg.ANCIENT, 2))
        self.assertFalse(globalcfg.covers(self.store, acc, globalcfg.ANCIENT, 1))
        globalcfg.section(self.store, globalcfg.ANCIENT)["on"] = False
        self.assertFalse(globalcfg.covers(self.store, acc, globalcfg.ANCIENT, 2))

    def test_ancient_waits_only_for_gem_week(self):
        """上古蛮荒按养成周只等装备宝石周的更新。"""
        with mock.patch.object(state, "growth_reserve", return_value=(2, state.RESERVE_CAP)):
            self.assertTrue(globalcfg.waiting_update(0, None))
            self.assertFalse(globalcfg.waiting_update(0, None, globalcfg.growth_week_of(globalcfg.ANCIENT)))
        with mock.patch.object(state, "growth_reserve", return_value=(globalcfg.ANCIENT_WEEK, state.RESERVE_CAP)):
            self.assertTrue(globalcfg.waiting_update(0, None, globalcfg.growth_week_of(globalcfg.ANCIENT)))

    def rows(self):
        return [(acc["username"], slot) for acc, slot in globalcfg.rows(self.store)]

    def test_table_rows(self):
        """表：新账号什么都不勾、不列出；勾了的存档和手动加的行列出来，按账号顺序、存档号排。"""
        self.assertEqual(self.rows(), [])
        globalcfg.entry(self.store, globalcfg.DAILY, "b")["slots"] = [3]
        globalcfg.add_row(self.store, "a", 5)
        globalcfg.add_row(self.store, "a", 5)
        globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"] = True  # 账号级：页面上选的存档那一行
        self.store.wide_cfg("a", globalcfg.ALLIANCE)["record"] = 2
        self.assertEqual(self.rows(), [("a", 2), ("a", 5), ("b", 3)])
        self.assertTrue(globalcfg.ticked(self.store, globalcfg.ALLIANCE, "a", 2))
        self.assertFalse(globalcfg.ticked(self.store, globalcfg.ALLIANCE, "a", 5))
        self.assertTrue(globalcfg.ticked(self.store, globalcfg.DAILY, "b", 3))

    def test_remove_row(self):
        """去掉一行：这一行的勾全取消；账号级的勾在别的行就不动。"""
        globalcfg.entry(self.store, globalcfg.DAILY, "a")["slots"] = [1, 2]
        globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"] = True
        self.store.wide_cfg("a", globalcfg.ALLIANCE)["record"] = 2
        globalcfg.add_row(self.store, "a", 1)
        globalcfg.remove_row(self.store, "a", 1)
        self.assertEqual(self.rows(), [("a", 2)])
        self.assertEqual(globalcfg.entry(self.store, globalcfg.DAILY, "a")["slots"], [2])
        self.assertIs(globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"], True)
        globalcfg.remove_row(self.store, "a", 2)
        self.assertEqual(self.rows(), [])
        self.assertIs(globalcfg.entry(self.store, globalcfg.ALLIANCE, "a")["on"], False)

    def test_unpicked(self):
        """该选的没选：有网格的看网格那一项；特别的几个看 needs（幻兽两个选一个就行，请教船只和材料都要选）。"""
        from zmxy import core
        from zmxy.taskdef import unpicked
        specs = core.load_tasks()
        self.assertTrue(unpicked(specs["ancient_wilderness"], {}))
        self.assertTrue(unpicked(specs["ancient_wilderness"], {"levels": []}))
        self.assertFalse(unpicked(specs["ancient_wilderness"], {"levels": [1400101]}))
        self.assertFalse(unpicked(specs["daily_quest"], {}))  # 没选关也照打精英拿强化石
        self.assertFalse(unpicked(specs["wish_pool"], None))  # 没有选择的任务
        self.assertTrue(unpicked(specs["fairy_feed"], {"fairy_p1": None}))
        self.assertFalse(unpicked(specs["fairy_feed"], {"fairy_p2": 7}))
        self.assertTrue(unpicked(specs["tongtian_consult"], {"materials": [1]}))
        self.assertFalse(unpicked(specs["tongtian_consult"], {"materials": [1], "ships": [2]}))
        self.assertTrue(unpicked(specs["teacher_trial"], {"student": ""}))

    def test_growth_keep_per_week(self):
        """留次数只看自己那一周：世界Boss留给翅膀光环周（每天 1 次），上古蛮荒留给装备宝石周（每天 2 次）。"""
        with mock.patch.object(state, "growth_reserve", return_value=(4, 2)):
            self.assertEqual(state.growth_keep(0, None, globalcfg.WORLD_WEEK), 2)
            self.assertEqual(state.growth_keep(0, None, globalcfg.ANCIENT_WEEK, 2), 0)
        with mock.patch.object(state, "growth_reserve", return_value=(4, state.RESERVE_CAP)):
            self.assertIsNone(state.growth_keep(0, None, globalcfg.WORLD_WEEK))

    def test_queue_ids(self):
        self.assertEqual(globalcfg.kind_of(globalcfg.queue_id(globalcfg.ALLIANCE)), globalcfg.ALLIANCE)
        self.assertIsNone(globalcfg.kind_of("alliance"))
        self.assertIsNone(globalcfg.kind_of("global:pvp_match"))  # 按开放时段的不走 global: 前缀


if __name__ == "__main__":
    unittest.main()
