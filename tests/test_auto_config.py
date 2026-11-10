"""周常页“自动配置”：每个任务只读地算出这个存档现在拥有的最大次数。全部离线。"""

import unittest
from dataclasses import replace
from unittest import mock

from zmxy import core, login, protocol as p
from zmxy.core import Session, TaskContext, load_tasks
from tests.support import FakeConn
from tests.test_weekly_tasks import tower_state

QUERY = "scene.demonTowerHandler.switchToLayer"
WING = "scene.copyHandler.getDungeonWingId"
TOWER = tower_state(count=3)


def auto(task_id, conn, info):
    s = Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s.info = conn, info
    s._link = login.Link(conn, s.config, [], {"time": 1_000_000})
    spec = load_tasks()[task_id]
    s.bind(spec)
    return spec.auto(TaskContext(s, {}))


class AutoTimesTests(unittest.TestCase):
    def test_fixed_and_week_boxes(self):
        self.assertEqual(auto("star_maze", FakeConn(), {"everyWeekSetting": {"rewardMazeCnt": 0}}), 1)
        self.assertEqual(auto("star_maze", FakeConn(), {"everyWeekSetting": {"rewardMazeCnt": 999}}), 0)
        self.assertEqual(auto("star_maze", FakeConn(), {"everyWeekSetting": {}}), 1)  # 服务器不下发 0 值
        self.assertIsNone(auto("star_maze", FakeConn(), {}))

    def test_ten_jue_uses_reward_count(self):
        self.assertEqual(auto("ten_jue", FakeConn(), {"haloRewardCnt": 2}), 2)
        self.assertIsNone(auto("ten_jue", FakeConn(), {}))

    def test_demon_tower_reads_count_without_writing(self):
        conn = FakeConn({QUERY: TOWER})
        self.assertEqual(auto("demon_tower", conn, {}), 3)
        self.assertEqual(conn.routes(), [QUERY])
        self.assertIsNone(auto("demon_tower", FakeConn({QUERY: {"demonTower": {}}}), {}))

    def test_tree_king_reads_remaining(self):
        self.assertEqual(auto("tree_king_nightmare", FakeConn({WING: {"dungeonId": min(__import__("zmxy.tasks.tree_king_nightmare", fromlist=["x"]).bosses()), "cnt": 2}}), {}), 2)

    def test_ancient_uses_shared_pool(self):
        self.assertEqual(auto("ancient_wilderness", FakeConn(), {"dungeonTreasure": {"1400100": {"remainCnt": 4}}}), 4)
        self.assertEqual(auto("ancient_wilderness", FakeConn(), {"dungeonTreasure": {"1400100": {"remainCnt": 50}}}), 10)  # 夹到每关次数上限
        self.assertIsNone(auto("ancient_wilderness", FakeConn(), {}))

    def test_world_boss_reads_shared_reward_count(self):
        self.assertEqual(auto("world_boss", FakeConn(), {"worldBoss": {"count": 4}}), 4)
        self.assertEqual(auto("world_boss", FakeConn(), {"worldBoss": {"count": 0}}), 0)
        self.assertIsNone(auto("world_boss", FakeConn(), {"worldBoss": {}}))

    def test_teacher_trial_weekly_limit_minus_used(self):
        info = {"everyWeekSetting": {"teacherChallengeWeekNum": 5, "studentChallengeWeekNum": 1}}
        self.assertEqual(auto("teacher_trial", FakeConn(), info), 13)
        self.assertIsNone(auto("teacher_trial_student", FakeConn(), info))  # 没选徒弟：不改
        self.assertEqual([load_tasks()[t].times_default for t in ("teacher_trial", "teacher_trial_student")], [0, 0])  # 不改时次数框是 0，不是 1

    def test_student_reward_count_is_read_from_the_student_save(self):
        """徒弟奖励的次数记在徒弟的存档上：师傅这边是 0，徒弟那边已用完 → 0 次。"""
        from contextlib import contextmanager
        from zmxy.tasks import teacher_trial
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = FakeConn(), {"everyWeekSetting": {"studentChallengeWeekNum": 0}}
        s._link = login.Link(s._conn, s.config, [], {"time": 1_000_000})
        peer = Session(p.ClientConfig(uid=2, username="s"))
        peer._conn, peer.info = FakeConn(), {"everyWeekSetting": {"studentChallengeWeekNum": 2}}
        opened = []

        @contextmanager
        def open_peer(username, slot):
            opened.append((username, slot))
            yield peer

        s.open_peer = open_peer
        spec = load_tasks()["teacher_trial_student"]
        s.bind(spec)
        self.assertEqual(spec.auto(TaskContext(s, {"student": "s|3"})), 0)
        self.assertEqual(opened, [("s", 3)])


class ReadAutoTests(unittest.TestCase):
    def test_clamps_and_reports_per_task(self):
        from unittest import mock
        specs = [load_tasks()[t] for t in ("ten_jue", "star_maze", "tree_king_nightmare", "world_boss")]

        class FakeSession(Session):
            world_count = 12

            def __init__(self, *a, **k):
                self.info, self._conn = {"haloRewardCnt": 4, "worldBoss": {"count": self.world_count}}, FakeConn({WING: "无权限"})
                self.config = p.ClientConfig(uid=1, username="u")
                self._link = login.Link(self._conn, self.config, [], {"time": 1_000_000})
                self.carry, self.spent = {}, __import__("collections").defaultdict(int)
                self._hooks, self._pumping = {}, False

            def __enter__(self): return self
            def __exit__(self, *a): return False

        account = mock.Mock()
        with mock.patch("zmxy.session.Session", FakeSession):
            out = core.read_auto(account, specs)
        self.assertEqual(out["ten_jue"], 4)
        self.assertEqual(out["world_boss"], 9)  # 服务器剩 12 次，但任务次数框上限是 9
        with mock.patch("zmxy.session.Session", FakeSession), mock.patch("zmxy.online.growth_week", return_value=3), \
                mock.patch("zmxy.state.growth_reserve", return_value=(4, 3)) as keep:
            self.assertEqual(core.read_auto(account, specs, reserve=True)["ten_jue"], 1)  # 拥有 4 次，今天留 3 次
            self.assertEqual(keep.call_args.kwargs, {"live": 3})  # 线上现在的养成周要传进去（更新前一直留）
            keep.return_value = (4, 5)
            self.assertEqual(core.read_auto(account, specs, reserve=True)["ten_jue"], 0)  # 等更新的那天：全部留着
            FakeSession.world_count = 5
            keep.return_value = (4, 3)
            self.assertEqual(core.read_auto(account, specs, reserve=True)["world_boss"], 2)  # 翅膀光环周前留 3 次
            keep.return_value = (4, 5)
            self.assertEqual(core.read_auto(account, specs, reserve=True)["world_boss"], 0)  # 更新当天留满
            keep.return_value = (3, 2)
            self.assertEqual(core.read_auto(account, specs, reserve=True)["world_boss"], 5)  # 留给别周的次数不扣
            FakeSession.world_count = 12
            self.assertEqual(core.read_auto(account, specs)["ten_jue"], 4)  # 开关关着：照旧  # 翅膀光环周前：十绝阵留次数
        self.assertIsNone(out["star_maze"])           # 读不到每周状态
        self.assertIsInstance(out["tree_king_nightmare"], str)  # 服务器拒绝 → 错误说明

    def test_every_weekly_task_with_times_has_auto(self):
        from zmxy.mainwin.rules import HIDDEN, OPEN_HOURS
        for sp in load_tasks().values():
            if sp.group == "周常" and sp.times_max and sp.id not in HIDDEN + OPEN_HOURS and sp.id not in ("pet_catch", "star_seal"):  # 宠物捕捉卡片（含诸星封印行）是周常页自己的模块，次数不用自动配置
                self.assertIsNotNone(sp.auto, sp.id)


class WeeklyAutoRunTests(unittest.TestCase):
    def test_limited_shop_runs_without_a_count_field(self):
        from zmxy.mainwin.rules import weekly_auto_run_ids
        from zmxy.tasks import limited_shop, last_rewards

        weekly = [sp.id for sp in load_tasks().values() if sp.group == "周常"]
        self.assertIn(limited_shop.ID, weekly)
        self.assertIn(limited_shop.ID, weekly_auto_run_ids(weekly, {}, False))
        self.assertNotIn(last_rewards.ID, weekly_auto_run_ids(weekly, {}, False))
        self.assertIn(last_rewards.ID, weekly_auto_run_ids(weekly, {}, True))

    def test_other_weeklies_still_require_a_positive_count(self):
        from zmxy.mainwin.rules import weekly_auto_run_ids
        from zmxy.tasks import limited_shop

        weekly = ["ten_jue", limited_shop.ID]
        self.assertEqual(weekly_auto_run_ids(weekly, {"ten_jue": "0"}, False), [limited_shop.ID])
        self.assertEqual(weekly_auto_run_ids(weekly, {"ten_jue": "2"}, False), ["ten_jue", limited_shop.ID])

    def test_world_boss_uses_the_weekly_count_filter(self):
        from zmxy.mainwin.rules import weekly_auto_run_ids
        from zmxy.tasks import boss_raid

        self.assertEqual(weekly_auto_run_ids([boss_raid.WORLD_ID], {boss_raid.WORLD_ID: "3"}, False), [boss_raid.WORLD_ID])
        self.assertEqual(weekly_auto_run_ids([boss_raid.WORLD_ID], {boss_raid.WORLD_ID: "0"}, False), [])


class ReserveTests(unittest.TestCase):
    """留次数：养成周开始前 4 天内，对应任务名变红、自动配置少填当天要留的次数。"""

    ROWS = [{"name": n + "养成", "underTime": end} for n, end in (
        ("翅膀光环", "2026/9/14-23:59:59"), ("装备宝石", "2026/9/21-23:59:59"),
        ("幻兽法宝", "2026/9/28-23:59:59"), ("灵宠宝石", "2026/10/8-23:59:59"))]

    def at(self, *d):
        from datetime import datetime, timedelta, timezone
        return datetime(2026, *d, tzinfo=timezone(timedelta(hours=8))).timestamp()

    def test_four_days_before_next_week(self):
        from zmxy import state
        self.assertIsNone(state.growth_reserve(self.at(10, 4, 23), self.ROWS))
        self.assertEqual(state.growth_reserve(self.at(10, 5, 0), self.ROWS), (4, 1))   # 下一周是翅膀光环；10/9 开始，10/5 留 1 次
        self.assertEqual(state.growth_reserve(self.at(10, 6, 12), self.ROWS), (4, 2))
        self.assertEqual(state.growth_reserve(self.at(10, 8, 23), self.ROWS), (4, 4))
        self.assertIsNone(state.growth_reserve(None, self.ROWS))

    def test_second_week_runs_from_monday_to_the_end(self):
        """灵宠宝石周 9/29（周二）开始、10/8 结束：第二周 = 10/5（周一）起到结束；更新顺延时线上确认了才接着算。"""
        from zmxy import state
        self.assertIsNone(state.growth_second_week(self.at(10, 4, 23), self.ROWS))
        self.assertEqual(state.growth_second_week(self.at(10, 5, 0), self.ROWS), 3)
        self.assertEqual(state.growth_second_week(self.at(10, 8, 23), self.ROWS), 3)
        self.assertIsNone(state.growth_second_week(self.at(10, 9, 1), self.ROWS))
        self.assertEqual(state.growth_second_week(self.at(10, 9, 9), self.ROWS, live=3), 3)
        self.assertIsNone(state.growth_second_week(self.at(10, 6, 9), self.ROWS, live=4))
        self.assertIsNone(state.growth_second_week(None, self.ROWS))

    def test_keeps_everything_until_the_game_updates(self):
        """10/9 才更新、又不是 0 点更新：更新前次数已经加上了也全部留着，线上换到下一周才放开。"""
        from zmxy import state
        self.assertEqual(state.growth_reserve(self.at(10, 9, 1), self.ROWS), (4, 5))            # 不知道线上：留
        self.assertEqual(state.growth_reserve(self.at(10, 9, 9), self.ROWS, live=3), (4, 5))    # 线上还是灵宠宝石周：留
        self.assertEqual(state.growth_reserve(self.at(10, 10, 9), self.ROWS, live=3), (4, 5))   # 更新顺延一天：接着留
        self.assertIsNone(state.growth_reserve(self.at(10, 9, 11), self.ROWS, live=4))          # 更新了：不用留
        self.assertIsNone(state.growth_reserve(self.at(10, 8, 11), self.ROWS, live=4))          # 提前更新了也一样
        self.assertEqual(state.growth_reserve(self.at(10, 8, 11), self.ROWS, live=3), (4, 4))

    def test_wraps_to_first_week(self):
        from zmxy import state
        rows = [dict(r) for r in self.ROWS]
        rows[0]["underTime"] = "2026/10/15-23:59:59"  # 现在是翅膀光环周 → 下一周装备宝石
        self.assertEqual(state.growth_reserve(self.at(10, 13, 12), rows), (1, 2))

    def test_tasks_declare_their_week(self):
        tasks = load_tasks()
        self.assertEqual({t: sp.reserve for t, sp in tasks.items() if sp.reserve}, {"ancient_wilderness": 1, "demon_tower": 2, "ten_jue": 4, "world_boss": 4})
        self.assertEqual({t: sp.reserve_gain for t, sp in tasks.items() if sp.reserve}, {"ancient_wilderness": 2, "demon_tower": 1, "ten_jue": 1, "world_boss": 1})  # 上古蛮荒每天涨 2 次、最多 10 次

    def test_ancient_keeps_twice_as_many(self):
        """上古蛮荒每天涨 2 次：别的任务留 1 次的那天它留 2 次。"""
        from zmxy import core
        spec = load_tasks()["ancient_wilderness"]

        class FakeSession:
            def __init__(self, *a, **k): self.now = 0
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def bind(self, spec): pass

        fake = replace(spec, auto=lambda ctx: 9)
        with mock.patch("zmxy.session.Session", FakeSession), mock.patch("zmxy.online.growth_week", return_value=4), \
                mock.patch("zmxy.state.growth_reserve", return_value=(1, 1)) as keep:
            self.assertEqual(core.read_auto(mock.Mock(), [fake], reserve=True)["ancient_wilderness"], 7)  # 拥有 9 次，留 2 次
            keep.return_value = (1, 4)
            self.assertEqual(core.read_auto(mock.Mock(), [fake], reserve=True)["ancient_wilderness"], 1)  # 前一天留 8 次


if __name__ == "__main__":
    unittest.main()
