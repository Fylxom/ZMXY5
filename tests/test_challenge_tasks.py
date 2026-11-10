"""旧日常页第二块（快速养成、万能强化石、藏宝洞、竞技场、斗宠大会、灵宠试炼）。全部离线。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from zmxy import activity
import time
from zmxy import core, dungeon, login, protocol as p, quota, state
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from tests.support import CN, Fake, run, ts


_UNLOCK_PATCH = None


def setUpModule():
    """这个文件的用例不关心解锁状态（解锁判断另有 tests/test_unlock.py）。"""
    global _UNLOCK_PATCH
    from unittest import mock
    _UNLOCK_PATCH = mock.patch("zmxy.quota.locked", return_value=None)
    _UNLOCK_PATCH.start()


def tearDownModule():
    _UNLOCK_PATCH.stop()


class QuickDevelopTests(unittest.TestCase):
    OPT = {"mode": "噩梦"}

    def run_week(self, conn, info, options=None, *, week=1, stale=False, now=ts(2026, 10, 1)):
        ahead = "cg1_20261021_1" if stale else None
        with mock.patch("zmxy.tasks.quick_develop.online.newer_online", return_value=ahead), \
             mock.patch("zmxy.tasks.quick_develop.online.growth_week", return_value=week):
            return run("quick_develop", conn, info, options or self.OPT, now=now)

    def test_shared_group_quota(self):
        self.assertEqual(quota.of({"dungeon": {}}, 500124).left, 1)
        # 500123 与 500124 共用一次；500128/500129 是另一组
        used = {"dungeon": {"500123": {"dailyCount": 1}}}
        self.assertEqual(quota.of(used, 500124).left, 0)
        self.assertEqual(quota.of(used, 500129).left, 1)
        self.assertIsNone(quota.of({}, 500124).left)  # 没有副本记录容器：不知道

    def test_runs_both_open_bosses_of_chosen_difficulty(self):
        conn = Fake()
        r = self.run_week(conn, {"dungeon": {}})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [500124, 500129])
        self.assertEqual(conn.routes().count("finishSingleCopy"), 2)

    def test_normal_difficulty_uses_first_of_each_pair(self):
        conn = Fake()
        self.run_week(conn, {"dungeon": {}}, {"mode": "普通"})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [500123, 500128])

    def test_used_group_is_skipped_other_still_runs(self):
        conn = Fake()
        self.run_week(conn, {"dungeon": {"500123": {"dailyCount": 1}}})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [500129])

    def test_all_used_or_unknown_creates_nothing(self):
        conn = Fake()
        both = {"dungeon": {"500123": {"dailyCount": 1}, "500128": {"dailyCount": 1}}}
        self.assertEqual(self.run_week(conn, both).status, "skipped")
        self.assertEqual(self.run_week(conn, {}, week=1).status, "blocked")
        self.assertEqual(self.run_week(conn, {"dungeon": {}}, week=None, stale=True).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_current_data_uses_weekly_activity_tables_without_needing_online_week_lookup(self):
        conn = Fake()
        with mock.patch("zmxy.tasks.quick_develop.online.newer_online", return_value=None), \
             mock.patch("zmxy.tasks.quick_develop.online.growth_week", side_effect=AssertionError("current tables are authoritative")):
            result = run("quick_develop", conn, {"dungeon": {}}, self.OPT, now=ts(2026, 10, 1))
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [500124, 500129])

    def test_all_four_growth_weeks_use_fixed_pairs_only_as_stale_data_fallback(self):
        expected = {
            1: [500115, 500138],
            2: [500117, 500119],
            3: [500124, 500129],
            4: [500126, 500122],
        }
        for week, mids in expected.items():
            with self.subTest(week=week):
                conn = Fake()
                row = state.row

                def no_weekly_config(table, key):
                    if table in ("SpeedDevelop", "Activity"):
                        raise AssertionError(f"unexpected weekly table lookup: {table}")
                    return row(table, key)

                with mock.patch("zmxy.tasks.quick_develop.state.row", side_effect=no_weekly_config), \
                     mock.patch("zmxy.tasks.quick_develop.state.activity_open", side_effect=AssertionError("unexpected activity lookup")):
                    result = self.run_week(conn, {"dungeon": {}}, week=week, stale=True)
                self.assertIn("备用配置", result.detail)
                self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], mids)

    def test_unset_difficulty_defaults_to_nightmare(self):
        a, b = Fake(), Fake()
        self.run_week(a, {"dungeon": {}}, {})
        self.run_week(b, {"dungeon": {}})
        self.assertTrue(a.bodies("createSinglePlayerCopy"))
        self.assertEqual([x["mid"] for x in a.bodies("createSinglePlayerCopy")], [x["mid"] for x in b.bodies("createSinglePlayerCopy")])

    def test_invalid_difficulty_or_unknown_growth_week(self):
        conn = Fake()
        self.assertEqual(run("quick_develop", conn, {"dungeon": {}}, {"mode": "地狱"}).status, "blocked")
        self.assertEqual(self.run_week(conn, {"dungeon": {}}, week=None, stale=True, now=ts(2027, 1, 1)).status, "blocked")
        self.assertEqual(self.run_week(conn, {"dungeon": {}}, now=ts(2027, 1, 1)).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_timeout_is_uncertain(self):
        def boom(route, body):
            if route == dungeon.R_FINISH:
                raise TimeoutError("t")
        with self.assertRaises(UncertainOutcome):
            self.run_week(Fake(boom), {"dungeon": {}})


class DailyQuestTests(unittest.TestCase):
    """日常任务：接取（前置步骤）→ 打勾选关卡 → 领奖 → 补万能强化石。用一个会记状态的假服务器。"""

    Q = {30040: 3001, 30050: 3002, 30060: 3003}  # 关卡 → 日常任务（TaskType label 2 的 dungeonId）

    class Server:
        def __init__(self, stones=0, per_attempt=0, vip=2, used=0, status=None, unlocked=True):
            from zmxy import data
            self.stones, self.per, self.vip, self.used = stones, per_attempt, vip, used
            self.status = dict(status or {})
            keys = data.table("Dungeon") if unlocked else {}
            self.dungeon = {k: {"id": int(k), "passNum": 1, "dailyCount": 0} for k in keys}
            self.last = None

        def info(self):
            return {"vip": {"level": self.vip}, "dungeon": self.dungeon,
                    "everyDaySetting": {"eliteSweep": self.stones, "dailyQuestCnt": self.used},
                    "quest": [{"id": q, "status": st} for q, st in self.status.items()]}

        def handler(self, route, body):
            if route == p.ROUTE_ENTER:
                return {"info": self.info()}
            if route.endswith("acceptQuest"):
                self.status[body["qId"]] = 1
                self.used += 1
            elif route.endswith("commitQuest"):
                self.status[body["qId"]] = 3
            elif route == dungeon.R_CREATE:
                self.last = body["mid"]
                return {"rnd": 7}
            elif route == dungeon.R_FINISH:
                self.stones += self.per
                qid = DailyQuestTests.Q.get(self.last)
                if self.status.get(qid) == 1:
                    self.status[qid] = 2
                return {"everyDaySetting": {"eliteSweep": self.stones}}

    def session(self, conn, info):
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = conn, info
        s.bind(load_tasks()["daily_quest"])
        return s

    def run_live(self, conn, info, options):
        """带着能重新进入角色的连接跑 run（打完勾选的关要 refresh 后领取）。"""
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = conn, info
        s._link = login.Link(conn, s.config, [{"index": 0}], {"time": ts(2026, 10, 1)})
        spec = load_tasks()["daily_quest"]
        s.bind(spec)
        return spec.run(TaskContext(s, options))

    def accept(self, server, levels, handler=None):
        conn = Fake(handler or server.handler)
        s = self.session(conn, server.info())
        return conn, s, load_tasks()["daily_quest"].prelude(TaskContext(s, {"levels": levels}))

    def test_prelude_accepts_chosen_quests_within_daily_allowance(self):
        server = self.Server(vip=3, used=3)  # VIP3 起每天 5 个，已接 3 个 → 只能再接 2 个
        conn, s, r = self.accept(server, [30040, 30050, 30060])
        self.assertIsNone(r)
        self.assertEqual([b["qId"] for b in conn.bodies("acceptQuest")], [3001, 3002])
        self.assertIn("已接取日常任务 2 个", s.carry["daily_quest"])
        self.assertIn("超出今日可接数量", s.carry["daily_quest"])

    def test_prelude_skips_accepted_and_ignores_levels_without_quest(self):
        server = self.Server(status={3001: 1, 3002: 3})
        conn, s, r = self.accept(server, [30040, 30050, 30010])  # 30010 没有日常任务
        self.assertIsNone(r)
        self.assertEqual(conn.sent, [])
        self.assertIn("2 个今天已接过", s.carry["daily_quest"])

    def test_prelude_blocked_without_server_state_and_does_nothing_without_levels(self):
        info = self.Server().info()
        info.pop("quest")
        conn = Fake()
        s = self.session(conn, info)
        spec = load_tasks()["daily_quest"]
        self.assertEqual(spec.prelude(TaskContext(s, {"levels": [30040]})).status, "blocked")
        self.assertIsNone(spec.prelude(TaskContext(s, {})))
        self.assertEqual(conn.sent, [])

    def test_prelude_requires_vip_limit_before_accepting_but_not_when_all_are_already_accepted(self):
        server = self.Server()
        info = server.info()
        info.pop("vip")
        conn = Fake()
        s = self.session(conn, info)
        result = load_tasks()["daily_quest"].prelude(TaskContext(s, {"levels": [30040]}))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(conn.bodies("acceptQuest"), [])

        info["quest"] = [{"id": qid, "status": 1} for qid in self.Q.values()]
        info.pop("everyDaySetting")
        s.info = info
        self.assertIsNone(load_tasks()["daily_quest"].prelude(TaskContext(s, {"levels": [30040]})))
        self.assertEqual(conn.bodies("acceptQuest"), [])

    def test_prelude_server_refusal_is_reported_not_fatal(self):
        server = self.Server()
        refuse = lambda route, body: "任务数量已达上限" if route.endswith("acceptQuest") else server.handler(route, body)
        _, s, r = self.accept(server, [30040], refuse)
        self.assertIsNone(r)
        self.assertIn("接取被拒绝", s.carry["daily_quest"])

    def test_prelude_timeout_is_uncertain(self):
        def boom(route, body):
            raise TimeoutError("t")
        with self.assertRaises(UncertainOutcome):
            self.accept(self.Server(), [30040], boom)

    def test_unlock_follows_prereqs(self):
        from zmxy.tasks import daily_quest as dq
        # 30010 需要主线第 1 关通关；30020 需要 30010 与主线第 2 关
        info = lambda passed: {"dungeon": {str(m): {"id": m, "passNum": 1} for m in passed}}
        self.assertEqual(dq.unlocked(info((1,))), [30010])
        self.assertEqual(dq.unlocked(info((1, 30010))), [30010])
        self.assertEqual(dq.unlocked(info((1, 2, 30010)))[:2], [30010, 30020])
        self.assertEqual(dq.unlocked(info(())), [])
        self.assertIsNone(dq.unlocked({}))

    def test_plays_each_chosen_level_once_then_claims(self):
        server = self.Server(stones=4, status={3001: 1, 3002: 1})
        conn = Fake(server.handler)
        r = self.run_live(conn, server.info(), {"levels": [30040, 30050]})
        self.assertIsNone(load_tasks()["daily_quest"].finale)  # 打完勾选的关当场领，不留到所有任务做完以后
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [30050, 30040])  # 已有 4 个：不补
        self.assertEqual([b["qId"] for b in conn.bodies("commitQuest")], [3002, 3001])  # 按打的顺序领
        self.assertLess(conn.routes().index("finishSingleCopy"), conn.routes().index("commitQuest"))
        self.assertEqual((r.status, r.pending), ("submitted", False), r.detail)  # 这一行直接是最终结果
        self.assertIn("已达今日强化石目标", r.detail)
        self.assertIn("已领取日常任务 2 个", r.detail)
        self.assertEqual(server.status, {3001: 3, 3002: 3})

    def test_claims_before_filling_stones(self):
        server = self.Server(per_attempt=0, status={3001: 1})
        conn = Fake(server.handler)
        r = self.run_live(conn, server.info(), {"levels": [30040]})
        routes = conn.routes()
        self.assertEqual(routes.count("commitQuest"), 1)
        self.assertLess(routes.index("commitQuest"), len(routes) - 1 - routes[::-1].index("finishSingleCopy"))  # 领完才去补强化石
        self.assertIn("已领取日常任务 1 个", r.detail)

    def test_completed_quests_and_full_stones_send_no_writes(self):
        server = self.Server(stones=4, used=2, status={3001: 3, 3002: 3})
        conn, s, prelude = self.accept(server, [30040, 30050])
        self.assertIsNone(prelude)
        s._link = login.Link(conn, s.config, [{"index": 0}], {"time": ts(2026, 10, 1)})
        r = load_tasks()["daily_quest"].run(TaskContext(s, {"levels": [30040, 30050]}))
        self.assertEqual(r.status, "skipped", r.detail)
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])
        self.assertIn("2 个今天已接过", r.detail)
        self.assertIn("2 个日常任务已完成", r.detail)
        self.assertIn("4/4", r.detail)
        self.assertNotIn("已结算", r.detail)

    def test_ready_quests_only_claim_when_stones_full(self):
        server = self.Server(stones=4, status={3001: 2, 3002: 3})
        conn = Fake(server.handler)
        r = self.run_live(conn, server.info(), {"levels": [30040, 30050]})
        self.assertEqual(conn.bodies("createSinglePlayerCopy"), [])
        self.assertEqual(conn.bodies("commitQuest"), [{"qId": 3001}])
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("已达今日强化石目标", r.detail)
        self.assertNotIn("没有可用的精英免费次数", r.detail)

    def test_mixed_quest_states_only_play_unfinished_quests(self):
        server = self.Server(stones=4, status={3001: 3, 3002: 2, 3003: 1})
        conn = Fake(server.handler)
        r = self.run_live(conn, server.info(), {"levels": list(self.Q)})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [30060])
        self.assertEqual([b["qId"] for b in conn.bodies("commitQuest")], [3003, 3002])
        self.assertEqual(r.status, "submitted", r.detail)

    def test_refresh_prevents_replaying_quests_completed_by_earlier_tasks(self):
        server = self.Server(stones=0, status={3001: 1})
        stale = server.info()
        server.stones, server.status[3001] = 4, 3
        conn = Fake(server.handler)
        r = self.run_live(conn, stale, {"levels": [30040]})
        self.assertEqual(r.status, "skipped", r.detail)
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])

    def test_repeating_finished_run_does_not_replay_or_reclaim(self):
        server = self.Server(stones=4, status={3001: 1, 3002: 1})
        conn = Fake(server.handler)
        options = {"levels": [30040, 30050]}
        first = self.run_live(conn, server.info(), options)
        self.assertEqual(first.status, "submitted", first.detail)
        conn.sent.clear()
        second = self.run_live(conn, server.info(), options)
        self.assertEqual(second.status, "skipped", second.detail)
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])

    def test_completed_quests_only_fill_missing_stones(self):
        server = self.Server(stones=3, per_attempt=1, status={3001: 3, 3002: 3})
        conn = Fake(server.handler)
        r = self.run_live(conn, server.info(), {"levels": [30040, 30050]})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [30050])
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertIn("4/4", r.detail)

    def test_unaccepted_quests_are_not_played_when_stones_full(self):
        server = self.Server(stones=4)
        refuse = lambda route, body: "任务数量已达上限" if route.endswith("acceptQuest") else server.handler(route, body)
        conn, s, prelude = self.accept(server, [30040], refuse)
        self.assertIsNone(prelude)
        conn.sent.clear()
        s._link = login.Link(conn, s.config, [{"index": 0}], {"time": ts(2026, 10, 1)})
        r = load_tasks()["daily_quest"].run(TaskContext(s, {"levels": [30040]}))
        self.assertEqual(r.status, "blocked", r.detail)  # 接取被拒要显式可见，不能算“已达目标”跳过
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])
        self.assertIn("接取被拒绝", r.detail)

    def test_missing_refreshed_quest_list_blocks_without_writes(self):
        server = self.Server(stones=4, status={3001: 1})
        missing = server.info()
        missing.pop("quest")
        conn = Fake(lambda route, body: {"info": missing} if route == p.ROUTE_ENTER else server.handler(route, body))
        r = self.run_live(conn, server.info(), {"levels": [30040]})
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])

    def test_ready_quest_without_stone_count_only_claims(self):
        server = self.Server(stones=4, status={3001: 2})

        def handler(route, body):
            reply = server.handler(route, body)
            if route == p.ROUTE_ENTER:
                reply["info"]["everyDaySetting"].pop("eliteSweep")
            return reply

        conn = Fake(handler)
        r = self.run_live(conn, server.info(), {"levels": [30040]})
        self.assertEqual(conn.bodies("createSinglePlayerCopy"), [])
        self.assertEqual(conn.bodies("commitQuest"), [{"qId": 3001}])
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("未补强化石", r.detail)

    def test_completed_quests_without_stone_count_block_without_writes(self):
        server = self.Server(stones=4, status={3001: 3})
        missing = server.info()
        missing["everyDaySetting"].pop("eliteSweep")
        conn = Fake(lambda route, body: {"info": missing} if route == p.ROUTE_ENTER else server.handler(route, body))
        r = self.run_live(conn, server.info(), {"levels": [30040]})
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])

    def test_stopped_ready_quests_send_no_writes(self):
        from threading import Event
        stop = Event()
        stop.set()
        server = self.Server(stones=4, status={3001: 2})
        conn = Fake(server.handler)
        r = run("daily_quest", conn, server.info(), {"levels": [30040]}, stop=stop)
        self.assertEqual(r.status, "skipped", r.detail)
        self.assertIn("已被用户停止", r.detail)
        self.assertEqual([route for route, _ in conn.sent if route != p.ROUTE_ENTER], [])

    def test_claim_refused_is_blocked(self):
        server = self.Server(stones=4, status={3001: 1})
        refuse = lambda route, body: "背包空间不足" if route.endswith("commitQuest") else server.handler(route, body)
        r = self.run_live(Fake(refuse), server.info(), {"levels": [30040]})
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertIn("领取被拒绝", r.detail)

    def test_short_of_stones_replays_chosen_then_fills_from_first_level(self):
        server = self.Server(per_attempt=0, status={3001: 1, 3002: 1})  # 一直拿不到强化石
        conn = Fake(server.handler)
        r = run("daily_quest", conn, server.info(), {"levels": [30040, 30050]})
        self.assertFalse(r.pending)
        mids = [b["mid"] for b in conn.bodies("createSinglePlayerCopy")]
        # 勾选的各一遍（最新的先打）→ 勾选的照原顺序再一遍 → 从第一关往后补（勾选的两关已满 2 次，跳过）
        self.assertEqual(mids[:12], [30050, 30040, 30050, 30040, 30010, 30010, 30020, 30020, 30030, 30030, 30060, 30060])
        self.assertTrue(all(mids.count(m) <= 2 for m in set(mids)))

    def test_second_pass_not_needed_when_target_reached(self):
        server = self.Server(stones=2, per_attempt=1, status={3001: 1, 3002: 1})
        conn = Fake(server.handler)
        r = run("daily_quest", conn, server.info(), {"levels": [30040, 30050]})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [30050, 30040])  # 2→4
        self.assertIn("4/4", r.detail)

    def test_no_levels_chosen_only_fills_stones_from_first_level(self):
        server = self.Server(stones=3, per_attempt=1)
        conn = Fake(server.handler)
        r = run("daily_quest", conn, server.info(), {})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [30010])
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertEqual(r.status, "submitted", r.detail)

    def test_target_met_or_unknown(self):
        server = self.Server(stones=4)
        conn = Fake(server.handler)
        self.assertEqual(run("daily_quest", conn, server.info()).status, "skipped")
        no_count = server.info()
        no_count.pop("everyDaySetting")
        conn = Fake(lambda route, body: {"info": no_count} if route == p.ROUTE_ENTER else None)
        self.assertEqual(run("daily_quest", conn, no_count).status, "blocked")
        conn2 = Fake(lambda route, body: {"info": {}} if route == p.ROUTE_ENTER else None)
        self.assertEqual(run("daily_quest", conn2, {}).status, "blocked")
        none_open = self.Server(unlocked=False)
        conn3 = Fake(none_open.handler)
        self.assertEqual(run("daily_quest", conn3, none_open.info()).status, "skipped")  # 一关都没通：没解锁精英
        for c in (conn, conn2, conn3):
            self.assertEqual([r for r, _ in c.sent if r != p.ROUTE_ENTER], [])

    def test_level_free_count_used_up_moves_on(self):
        server = self.Server(per_attempt=0)
        server.dungeon["30010"]["dailyCount"] = 2
        conn = Fake(server.handler)
        run("daily_quest", conn, server.info(), {})
        mids = [b["mid"] for b in conn.bodies("createSinglePlayerCopy")]
        self.assertNotIn(30010, mids)
        self.assertEqual(mids[0], 30020)

    def test_stones_only_flag_reaches_pickup(self):
        server = self.Server(stones=3)

        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": [{"monsterId": 1, "type": 3, "num": 10}]}  # 灵魂，不是强化道具
            return server.handler(route, body)
        conn = Fake(handler)
        run("daily_quest", conn, server.info(), {"stones_only": True})
        self.assertNotIn("syncDropInfo", conn.routes())

    def test_commit_timeout_is_uncertain(self):
        server = self.Server(stones=4, status={3001: 1})

        def handler(route, body):
            if route.endswith("commitQuest"):
                raise TimeoutError("t")
            return server.handler(route, body)
        with self.assertRaises(UncertainOutcome):
            self.run_live(Fake(handler), server.info(), {"levels": [30040]})


class PreludeOrderTests(unittest.TestCase):
    class FakeSession:
        def __init__(self, *a, **k):
            self.carry = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def bind(self, spec):
            pass

    def run_registry(self, registry, tasks):
        acc = core.Account("a", p.ClientConfig(uid=1, username="u"), tasks)
        with mock.patch("zmxy.session.Session", self.FakeSession), mock.patch("zmxy.runner.load_tasks", return_value=registry):
            return core.run_account(acc)

    def test_prelude_runs_before_first_tasks_but_run_keeps_its_place(self):
        calls = []

        def spec(id, **kw):
            return core.TaskSpec(id, id, lambda ctx: calls.append(f"{id}.run") or core.TaskResult(id, "submitted"), **kw)
        registry = {"first": spec("first", first=True), "plain": spec("plain"), "last": spec("last", last=True),
                    "quest": spec("quest", prelude=lambda ctx: calls.append("quest.prelude"))}
        self.run_registry(registry, ("plain", "quest", "first", "last"))
        self.assertEqual(calls, ["quest.prelude", "first.run", "plain.run", "quest.run", "last.run"])

    def test_late_prelude_runs_after_other_preludes(self):
        calls = []

        def spec(id, **kw):
            return core.TaskSpec(id, id, lambda ctx: core.TaskResult(id, "submitted"), **kw)
        registry = {"arena": spec("arena", prelude=lambda ctx: calls.append("arena") , prelude_late=True),
                    "quest": spec("quest", prelude=lambda ctx: calls.append("quest")),
                    "limited": spec("limited", prelude=lambda ctx: calls.append("limited"))}
        self.run_registry(registry, ("arena", "quest", "limited"))
        self.assertEqual(calls, ["quest", "limited", "arena"])

    def test_finale_runs_after_normal_tasks_before_last_and_merges_into_one_row(self):
        calls = []
        emitted = []

        def spec(id, **kw):
            return core.TaskSpec(id, id, lambda ctx: calls.append(f"{id}.run") or core.TaskResult(id, "skipped", "打完"), **kw)
        registry = {"quest": spec("quest", finale=lambda ctx: calls.append("quest.finale") or core.TaskResult("quest", "submitted", "已领取")),
                    "plain": spec("plain"), "last": spec("last", last=True)}
        acc = core.Account("a", p.ClientConfig(uid=1, username="u"), ("quest", "plain", "last"))
        with mock.patch("zmxy.session.Session", self.FakeSession), mock.patch("zmxy.runner.load_tasks", return_value=registry):
            out = core.run_account(acc, emit=lambda n, r: emitted.append(r))
        self.assertEqual(calls, ["quest.run", "plain.run", "quest.finale", "last.run"])
        quest = [r for r in out if r.task == "quest"]
        self.assertEqual([(r.status, r.detail) for r in quest], [("submitted", "打完；已领取")])

    def test_prelude_result_aborts_that_task_only(self):
        ran = []
        quest = core.TaskSpec("quest", "quest", lambda ctx: ran.append("quest") or core.TaskResult("quest", "submitted"),
                              prelude=lambda ctx: core.TaskResult("quest", "blocked", "no"))
        other = core.TaskSpec("other", "other", lambda ctx: ran.append("other") or core.TaskResult("other", "submitted"))
        out = self.run_registry({"quest": quest, "other": other}, ("quest", "other"))
        self.assertEqual(ran, ["other"])
        self.assertEqual([(r.task, r.status) for r in out], [("quest", "blocked"), ("other", "submitted")])


@mock.patch("zmxy.tasks.treasure_cave.claim_fairy", lambda ctx: core.TaskResult("treasure_cave", core.SKIPPED, "仙魄尚未满一小时可领"))
class TreasureCaveTests(unittest.TestCase):
    """挑战本身；打完顺带领仙魄见 TreasureCaveFairyTests 和 tests/test_fairy_claim.py。"""
    RECS = {"recommend": [{"entityId": "a"}, {"entityId": "b"}, {"entityId": "c"}]}

    def conn(self):
        return Fake(lambda route, body: self.RECS if route.endswith("getRecommend") else None)

    def test_challenges_middle_card_until_cap(self):
        conn = self.conn()
        r = run("treasure_cave", conn, {"everyDaySetting": {"landformChallengeCnt": 3}})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("beginBattle"), [{"entityId": "b"}] * 2)  # 上限 5，已用 3
        self.assertEqual(conn.bodies("endBattle"), [{"entityId": "b", "win": 1}] * 2)

    def test_used_up_or_unknown_sends_nothing(self):
        conn = self.conn()
        self.assertEqual(run("treasure_cave", conn, {"everyDaySetting": {"landformChallengeCnt": 5}}).status, "skipped")
        self.assertEqual(run("treasure_cave", conn, {}).status, "blocked")
        self.assertEqual(run("treasure_cave", conn, {"everyDaySetting": {"landformChallengeCnt": "x"}}).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_missing_recommendation_blocks_without_battle(self):
        conn = Fake(lambda route, body: {"recommend": [{"entityId": "a"}]} if route.endswith("getRecommend") else None)
        r = run("treasure_cave", conn, {"everyDaySetting": {}})
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("beginBattle", conn.routes())

    def test_begin_rejected_is_not_retried(self):
        def handler(route, body):
            if route.endswith("getRecommend"):
                return self.RECS
            if route.endswith("beginBattle"):
                return "次数不足"
        conn = Fake(handler)
        r = run("treasure_cave", conn, {"everyDaySetting": {}})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes().count("beginBattle"), 1)
        self.assertNotIn("endBattle", conn.routes())

    def test_end_timeout_is_uncertain(self):
        def handler(route, body):
            if route.endswith("getRecommend"):
                return self.RECS
            if route.endswith("endBattle"):
                raise TimeoutError("t")
        with self.assertRaises(UncertainOutcome):
            run("treasure_cave", Fake(handler), {"everyDaySetting": {}})


class TreasureCaveFairyTests(unittest.TestCase):
    """藏宝洞打完顺带领仙魄：次数用完也领；领取被阻止时整体报阻止、挑战结果照写；挑战结果未知就不领。"""
    RECS = TreasureCaveTests.RECS

    def claim(self, status, detail="仙魄"):
        calls = []

        def fake(ctx):
            calls.append(ctx)
            return core.TaskResult("treasure_cave", status, detail)
        return calls, mock.patch("zmxy.tasks.treasure_cave.claim_fairy", fake)

    def test_claims_after_challenges(self):
        calls, patch = self.claim(core.SUBMITTED, "已领取 1 个藏宝洞的仙魄")
        conn = Fake(lambda route, body: self.RECS if route.endswith("getRecommend") else None)
        with patch:
            r = run("treasure_cave", conn, {"everyDaySetting": {"landformChallengeCnt": 4}})
        self.assertEqual((r.status, len(calls)), ("submitted", 1))
        self.assertIn("藏宝洞挑战 1 次", r.detail)
        self.assertIn("已领取 1 个藏宝洞的仙魄", r.detail)

    def test_used_up_still_claims(self):
        for claimed, expected in ((core.SUBMITTED, "submitted"), (core.SKIPPED, "skipped"), (core.BLOCKED, "blocked")):
            calls, patch = self.claim(claimed)
            with self.subTest(claimed=claimed), patch:
                r = run("treasure_cave", Fake(), {"everyDaySetting": {"landformChallengeCnt": 5}})
                self.assertEqual((r.status, len(calls)), (expected, 1))
                self.assertIn("免费次数已用完", r.detail)

    def test_unknown_count_blocks_but_still_claims(self):
        calls, patch = self.claim(core.SUBMITTED)
        with patch:
            r = run("treasure_cave", Fake(), {})
        self.assertEqual((r.status, len(calls)), ("blocked", 1))

    def test_uncertain_challenge_does_not_claim(self):
        def handler(route, body):
            if route.endswith("getRecommend"):
                return self.RECS
            if route.endswith("endBattle"):
                raise TimeoutError("t")
        calls, patch = self.claim(core.SUBMITTED)
        with patch, self.assertRaises(UncertainOutcome):
            run("treasure_cave", Fake(handler), {"everyDaySetting": {}})
        self.assertEqual(calls, [])

    def test_routes_moved_from_shop(self):
        from zmxy.tasks import fairy_flag_shop as ff, treasure_cave as tc
        specs = load_tasks()
        self.assertIn(tc.R_REWARD, specs[tc.ID].writes)
        self.assertIn(tc.R_PUBLISHED, specs[tc.ID].reads)
        self.assertFalse({tc.R_REWARD, tc.R_PUBLISHED} & (specs[ff.ID].reads | specs[ff.ID].writes))


def arena_info(**extra):
    base = {"recommend": [{"rid": i, "name": f"n{i}", "entityId": f"e{i}"} for i in range(1, 9)],
            "jjcChallengeNum": "5;5;5;5;5;5;5", "sweepEnergy": 20,
            "arenaSetting": {"week": {}}, "petArenaSetting": {"week": {}}}
    return {**base, **extra}


SW = {"mode": "sweep"}


@mock.patch("time.sleep", lambda s: None)
class ArenaTests(unittest.TestCase):
    A = "scene.arenaHandler."
    P = "scene.petArenaHandler."

    def conn(self, info, sweep=None, details=None, prefix="scene.arenaHandler."):
        def handler(route, body):
            name = route[len(prefix):]
            if name in ("getArenaInfo",):
                return info() if callable(info) else info
            if name == "sweep" and sweep is not None:
                return sweep(body)
            if name in ("getArenaTargetInfo", "getArenaUserData"):
                return details or {"userData": {"name": "x", "entityId": "ex"}}
        return Fake(handler)

    def test_free_count_by_weekday_and_used(self):
        from zmxy.tasks import arena as ar
        sunday, monday = ts(2026, 10, 4), ts(2026, 10, 5)  # 2026-10-04 是周日
        resp = {"jjcChallengeNum": "10;5;5;5;5;10;10", "arenaSetting": {"week": {"0": 3, "1": 5}}}
        self.assertEqual(ar.free_left(ar.ARENA, resp, {}, sunday), 7)
        self.assertEqual(ar.free_left(ar.ARENA, resp, {}, monday), 0)
        self.assertIsNone(ar.free_left(ar.ARENA, {}, {}, sunday))  # 缺少周次数状态，不能假定今天用过 0 次
        self.assertEqual(ar.free_left(ar.ARENA, {}, {"arenaSetting": {"week": {}}}, sunday), 10)  # 正常空记录 = 已用 0 次
        self.assertIsNone(ar.free_left(ar.ARENA, {"arenaSetting": {"week": "bad"}}, {}, sunday))
        self.assertIsNone(ar.free_left(ar.ARENA, {"jjcChallengeNum": "x"}, {}, sunday))
        self.assertIsNone(ar.free_left(ar.ARENA, {}, {}, None))
        self.assertIsNone(ar.free_left(ar.ARENA, {"jjcChallengeNum": "-1;5;5;5;5;5;5"}, {}, sunday))

    def test_nonfinite_or_negative_power_is_unknown(self):
        from zmxy.tasks import arena as ar
        for value in (float("nan"), float("inf"), -1):
            self.assertIsNone(ar._power(value))

    def test_valid_empty_week_state_uses_client_default_for_both_arenas(self):
        from zmxy.tasks import arena as ar
        monday = ts(2026, 10, 5)
        for activity in (ar.ARENA, ar.PET_ARENA):
            with self.subTest(activity=activity.id):
                self.assertEqual(ar.free_left(activity, {"jjcChallengeNum": "5;5;5;5;5;5;5", "everyWeekSetting": {}}, {}, monday), 5)
                self.assertEqual(ar.free_left(activity, {"jjcChallengeNum": "5;5;5;5;5;5;5"}, {"everyWeekSetting": {}}, monday), 5)

    def test_omitted_response_record_preserves_used_count_from_entry(self):
        from zmxy.tasks import arena as ar
        monday = ts(2026, 10, 5)
        for activity in (ar.ARENA, ar.PET_ARENA):
            info = {"everyWeekSetting": {activity.setting: {"week": {"1": 4}}}}
            resp = {"jjcChallengeNum": "5;5;5;5;5;5;5", "everyWeekSetting": {}}
            self.assertEqual(ar.free_left(activity, resp, info, monday), 1)

    def test_sparse_week_array_uses_zero_only_for_absent_day(self):
        from zmxy.tasks import arena as ar
        monday = ts(2026, 10, 5)
        for week, expected in (([], 5), ([3], 5), ([3, 2], 3), ([3, "bad"], None)):
            resp = {"jjcChallengeNum": "5;5;5;5;5;5;5", "everyWeekSetting": {"arenaSetting": {"week": week}}}
            self.assertEqual(ar.free_left(ar.ARENA, resp, {}, monday), expected)

    def test_malformed_week_container_is_not_replaced_by_empty_default(self):
        from zmxy.tasks import arena as ar
        for value in (None, [], "bad"):
            resp = {"everyWeekSetting": value}
            self.assertIsNone(ar.free_left(ar.ARENA, resp, {"everyWeekSetting": {}}, ts(2026, 10, 5)))

    def test_fresh_week_record_overrides_entry_without_false_fallback(self):
        from zmxy.tasks import arena as ar
        monday = ts(2026, 10, 5)
        info = {"everyWeekSetting": {"arenaSetting": {"week": {"1": 0}}}}
        for setting in ({}, None, [], {"week": "bad"}):
            with self.subTest(setting=setting):
                self.assertIsNone(ar.free_left(ar.ARENA, {"everyWeekSetting": {"arenaSetting": setting}}, info, monday))
        self.assertEqual(ar.free_left(ar.ARENA, {"everyWeekSetting": {"arenaSetting": {"week": {"1": 5}}}}, info, monday), 0)

    def test_normal_empty_week_response_can_complete_free_challenges(self):
        for task_id, prefix in (("arena", self.A), ("pet_arena", self.P)):
            with self.subTest(task_id=task_id):
                response = arena_info(jjcChallengeNum="1;1;1;1;1;1;1", everyWeekSetting={})
                response.pop("arenaSetting")
                response.pop("petArenaSetting")
                conn = self.conn(response, sweep=lambda _: {}, prefix=prefix)
                result = run(task_id, conn, {}, SW)
                self.assertEqual(result.status, "submitted", result.detail)
                self.assertEqual(len(conn.bodies("sweep")), 1)

    def test_unknown_week_blocks_with_specific_reason_and_no_battle(self):
        response = arena_info()
        response.pop("arenaSetting")
        response.pop("petArenaSetting")
        conn = self.conn(response)
        result = run("arena", conn, {}, SW)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("每周状态", result.detail)
        self.assertEqual(conn.routes(), ["getArenaInfo"])

    def test_sweeps_from_slot_8_down(self):
        conn = self.conn(arena_info(arenaSetting={"week": {}}), sweep=lambda b: {"sweepEnergy": 20})
        with mock.patch("zmxy.state.week_index", lambda now: 1):
            r = run("arena", conn, {}, SW)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("sweep"), [{"targetId": 8}] * 5)
        self.assertNotIn("beginArenaBattle", conn.routes())

    def test_unsweepable_target_moves_to_next_slot(self):
        def sweep(body):
            return "该对手不能扫荡" if body["targetId"] == 8 else {"sweepEnergy": 20}
        conn = self.conn(arena_info(jjcChallengeNum="1;1;1;1;1;1;1"), sweep=sweep)
        r = run("arena", conn, {}, SW)
        self.assertEqual(conn.bodies("sweep"), [{"targetId": 8}, {"targetId": 7}])
        self.assertEqual(r.status, "submitted")

    def test_unknown_sweep_error_stops_without_retry(self):
        conn = self.conn(arena_info(), sweep=lambda b: "服务器繁忙")
        r = run("arena", conn, {}, SW)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.bodies("sweep")), 1)

    def test_no_energy_falls_back_to_manual_slot_8(self):
        conn = self.conn(arena_info(sweepEnergy=0, jjcChallengeNum="2;2;2;2;2;2;2"))
        r = run("arena", conn, {}, SW)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("sweep"), [])
        self.assertEqual(conn.bodies("getArenaTargetInfo"), [{"targetId": 8}] * 2)
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 8, "win": 1}] * 2)

    def test_arena_without_slot_8_does_not_fight(self):
        info = arena_info(sweepEnergy=0)
        info["recommend"] = info["recommend"][:3]
        conn = self.conn(info)
        self.assertEqual(run("arena", conn, {}, SW).status, "blocked")
        self.assertNotIn("beginArenaBattle", conn.routes())

    def test_manual_mode_fights_strongest(self):
        info = arena_info(jjcChallengeNum="2;2;2;2;2;2;2")
        for row in info["recommend"]:
            row["p1"] = {"fightPower": 100 * row["rid"] if row["rid"] != 5 else 9999}
        conn = self.conn(info)
        r = run("arena", conn, {}, {"mode": "manual"})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 5, "win": 1}] * 2)

    def test_manual_without_power_data_fights_slot_1(self):
        info = arena_info(jjcChallengeNum="1;1;1;1;1;1;1")
        info["recommend"] = info["recommend"][:3]
        conn = self.conn(info)
        self.assertEqual(run("arena", conn, {}, {"mode": "manual"}).status, "submitted")
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 1, "win": 1}])

    def test_bottom_mode_fights_weakest(self):
        info = arena_info(jjcChallengeNum="2;2;2;2;2;2;2")
        for row in info["recommend"]:
            row["p1"] = {"fightPower": 100 * row["rid"] if row["rid"] != 3 else 1}
        conn = self.conn(info)
        r = run("arena", conn, {}, {"mode": "bottom"})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 3, "win": 1}] * 2)
        self.assertEqual(conn.bodies("sweep"), [])

    def test_bottom_without_power_data_fights_last_slot(self):
        info = arena_info(jjcChallengeNum="1;1;1;1;1;1;1")
        info["recommend"] = info["recommend"][:3]
        conn = self.conn(info)
        self.assertEqual(run("arena", conn, {}, {"mode": "bottom"}).status, "submitted")
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 3, "win": 1}])

    def test_bottom_and_top_pick_by_pet_arena_power(self):
        from zmxy.tasks import arena
        recs = [{"totalScore": n, "battleScore": 0} for n in (50, 10, 90, 10)]  # 斗宠战力 = totalScore - battleScore
        self.assertEqual(arena.strongest(arena.PET_ARENA, recs), 3)
        self.assertEqual(arena.weakest(arena.PET_ARENA, recs), 4)  # 同战力取靠后的

    def test_modes_are_top_bottom_sweep(self):
        spec = load_tasks()["arena"]
        self.assertEqual(spec.modes, (("manual", "榜首"), ("bottom", "榜尾"), ("sweep", "扫荡")))
        self.assertEqual(load_tasks()["pet_arena"].modes, spec.modes)
        self.assertEqual(spec.default_mode, "sweep")

    def test_pet_arena_registered_fights_slot_1_without_details(self):
        info = arena_info(jjcChallengeNum="1;1;1;1;1;1;1")
        info["recommend"] = info["recommend"][:3]
        info["self"] = {"rank": 5}
        conn = self.conn(info, prefix=self.P)
        r = run("pet_arena", conn, {}, SW)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("sweep"), [])
        self.assertEqual(conn.bodies("getArenaUserData"), [])  # 推荐里已有 name/entityId
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 1, "win": 1}])

    def test_pet_arena_empty_list_after_refresh_stops_with_a_reason(self):
        reads = []

        def info():
            got = arena_info(jjcChallengeNum="2;2;2;2;2;2;2")
            got["recommend"] = got["recommend"][:3] if not reads else []  # 打完一场再读：对手列表空了
            got["self"] = {"rank": 5}  # 排名靠前不能扫荡，只能手打
            reads.append(1)
            return got

        conn = self.conn(info, prefix=self.P)
        r = run("pet_arena", conn, {}, SW)
        self.assertNotEqual(r.status, "error", r.detail)
        self.assertIn("没有返回对手列表", r.detail)
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 1, "win": 1}])

    def test_missing_opponent_fields_do_not_begin(self):
        conn = self.conn(arena_info(sweepEnergy=0), details={"userData": {}})
        info = arena_info(sweepEnergy=0)
        info["recommend"] = [{"rid": i} for i in range(1, 9)]
        conn = self.conn(info, details={"userData": {}})
        self.assertEqual(run("arena", conn, {}, SW).status, "blocked")
        self.assertNotIn("beginArenaBattle", conn.routes())

    def test_quota_unknown_or_used_up(self):
        conn = self.conn(arena_info(jjcChallengeNum="x"))
        self.assertEqual(run("arena", conn, {}, SW).status, "blocked")
        used = arena_info(arenaSetting={"week": {str(d): 99 for d in range(7)}})
        self.assertEqual(run("arena", self.conn(used), {}, SW).status, "skipped")
        self.assertEqual(self.conn(used).sent, [])

    def test_manual_mode_fights_slot_1_and_never_sweeps(self):
        conn = self.conn(arena_info(jjcChallengeNum="2;2;2;2;2;2;2"))
        r = run("arena", conn, {}, {"mode": "manual"})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("sweep"), [])
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 1, "win": 1}] * 2)

    def test_no_mode_defaults_to_sweep(self):
        conn = self.conn(arena_info())
        run("arena", conn, {}, {})
        self.assertEqual(conn.bodies("beginArenaBattle"), [])

    def test_arena_and_pet_alternate_when_both_selected(self):
        def handler(route, body):
            name = route.rsplit(".", 1)[1]
            if name == "getArenaInfo":
                return arena_info(jjcChallengeNum="2;2;2;2;2;2;2", sweepEnergy=0)
            if name in ("getArenaTargetInfo", "getArenaUserData"):
                return {"userData": {"name": "x", "entityId": "e"}}
        conn = Fake(handler)
        session = Session(p.ClientConfig(uid=1, username="u"))
        both = ("arena", "pet_arena")
        opts = {"arena": {"mode": "manual"}, "pet_arena": {"mode": "manual"}}
        r1 = run("arena", conn, {}, opts["arena"], tasks=both, all_options=opts, session=session)
        sent_after_arena = len(conn.sent)
        r2 = run("pet_arena", conn, {}, opts["pet_arena"], tasks=both, all_options=opts, session=session)
        self.assertEqual(len(conn.sent), sent_after_arena)  # 斗宠任务不再发包，只回报结果
        self.assertEqual((r1.task, r1.status, r2.task, r2.status), ("arena", "submitted", "pet_arena", "submitted"), (r1.detail, r2.detail))
        begins = [r.split(".")[1] for r, _ in conn.sent if r.endswith("beginArenaBattle")]
        self.assertEqual(begins, ["arenaHandler", "petArenaHandler", "arenaHandler", "petArenaHandler"])

    def test_sweeping_jobs_do_not_alternate(self):
        def handler(route, body):
            name = route.rsplit(".", 1)[1]
            if name == "getArenaInfo":
                return arena_info(jjcChallengeNum="2;2;2;2;2;2;2")
            if name == "sweep":
                return {"sweepEnergy": 20}
        conn = Fake(handler)
        session = Session(p.ClientConfig(uid=1, username="u"))
        both = ("arena", "pet_arena")
        opts = {"arena": SW, "pet_arena": SW}
        run("arena", conn, {}, SW, tasks=both, all_options=opts, session=session)
        sweeps = [r.split(".")[1] for r, _ in conn.sent if r.endswith(".sweep")]
        self.assertEqual(sweeps, ["arenaHandler"] * 2 + ["petArenaHandler"] * 2)

    def test_pair_degrades_when_one_side_has_no_count(self):
        def handler(route, body):
            name = route.rsplit(".", 1)[1]
            if name == "getArenaInfo":
                used = route.startswith(self.P)
                return arena_info(jjcChallengeNum="2;2;2;2;2;2;2", sweepEnergy=0,
                                  **({"petArenaSetting": {"week": {str(d): 9 for d in range(7)}}} if used else {}))
            if name == "getArenaTargetInfo":
                return {"userData": {"name": "x", "entityId": "e"}}
        conn = Fake(handler)
        opts = {"arena": {"mode": "manual"}, "pet_arena": {"mode": "manual"}}
        session = Session(p.ClientConfig(uid=1, username="u"))
        run("arena", conn, {}, opts["arena"], tasks=("arena", "pet_arena"), all_options=opts, session=session)
        r2 = run("pet_arena", conn, {}, opts["pet_arena"], tasks=("arena", "pet_arena"), all_options=opts, session=session)
        self.assertEqual(r2.status, "skipped")
        self.assertEqual(len(conn.bodies("endArenaBattle")), 2)

    def test_end_timeout_is_uncertain(self):
        def handler(route, body):
            if route.endswith("getArenaInfo"):
                return arena_info(sweepEnergy=0)
            if route.endswith("endArenaBattle"):
                raise TimeoutError("t")
            if route.endswith("getArenaTargetInfo"):
                return {"userData": {"name": "x", "entityId": "e"}}
        with self.assertRaises(UncertainOutcome):
            run("arena", Fake(handler), {}, SW)

    def test_stop_halts_before_next_sweep(self):
        import threading
        stop = threading.Event()
        stop.set()
        conn = self.conn(arena_info(), sweep=lambda b: {"sweepEnergy": 20})
        r = run("arena", conn, {}, SW, stop=stop)
        self.assertEqual(conn.bodies("sweep"), [])

    def test_sweep_button_rule_follows_client(self):
        from zmxy.tasks import arena as ar
        me = {"index": 0, "p1": {"fightPower": 1000}, "p2": {"fightPower": 5000}}
        self.assertIs(ar.sweepable(ar.ARENA, me, {"p1": {"fightPower": 990}}), True)  # Challenge[1].sweepPower = 0.99
        self.assertIs(ar.sweepable(ar.ARENA, me, {"p1": {"fightPower": 991}}), False)
        self.assertIs(ar.sweepable(ar.ARENA, {**me, "index": 1}, {"index": 1, "p1": {"fightPower": 1}, "p2": {"fightPower": 4950}}), True)  # 双方都看出战的那个角色
        self.assertIsNone(ar.sweepable(ar.ARENA, me, {"rid": 1}))  # 读不到战力：不知道，让服务器判断
        self.assertIsNone(ar.sweepable(ar.ARENA, None, {"p1": {"fightPower": 1}}))
        pets = {"pets": [{"fightPower": 400}, {"fightPower": 300}, {"fightPower": 300}]}
        self.assertIs(ar.sweepable(ar.PET_ARENA, pets, {"totalScore": 900, "battleScore": 100}), True)  # Challenge[2].sweepPower = 0.8
        self.assertIs(ar.sweepable(ar.PET_ARENA, pets, {"totalScore": 901, "battleScore": 100}), False)
        self.assertIsNone(ar.sweepable(ar.PET_ARENA, {"pets": []}, {"totalScore": 1, "battleScore": 0}))

    def test_targets_out_of_sweep_range_are_never_requested(self):
        info = arena_info(jjcChallengeNum="2;2;2;2;2;2;2", self={"p1": {"fightPower": 1000}})
        for row in info["recommend"]:
            row["p1"] = {"fightPower": 500 if row["rid"] == 6 else 2000}
        conn = self.conn(info, sweep=lambda b: {"sweepEnergy": 20})
        r = run("arena", conn, {}, SW)
        self.assertEqual(conn.bodies("sweep"), [{"targetId": 6}] * 2)  # 8、7 号位战力超出范围：不拿去试
        self.assertEqual(r.status, "submitted", r.detail)

    def test_no_target_in_sweep_range_goes_manual_without_asking(self):
        info = arena_info(jjcChallengeNum="1;1;1;1;1;1;1", self={"p1": {"fightPower": 1000}})
        for row in info["recommend"]:
            row["p1"] = {"fightPower": 2000}
        conn = self.conn(info)
        r = run("arena", conn, {}, SW)
        self.assertEqual(conn.bodies("sweep"), [])
        self.assertEqual(conn.bodies("endArenaBattle"), [{"targetId": 8, "win": 1}])
        self.assertIn("手打 1：对手战力都超出扫荡范围", r.detail)

    def test_energy_running_out_switches_to_manual_cooldown(self):
        from zmxy.tasks import arena as ar
        conn = self.conn(arena_info(sweepEnergy=4, jjcChallengeNum="3;3;3;3;3;3;3"), sweep=lambda b: {})
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn = conn
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": ts(2026, 10, 1)})
        session.bind(load_tasks()["arena"])
        shown = []
        job = ar.Job(TaskContext(session, SW, report=shown.append), ar.ARENA, "sweep")
        job.begin()
        self.assertTrue(job.step())  # 扫一次，能量用完
        self.assertEqual((job.swept, job.can_sweep, job.ready_at() - job.last), (1, True, ar.SWEEP_GAP))
        self.assertTrue(job.step())  # 这一步只是改成手打，不发包
        self.assertEqual((len(conn.sent), job.can_sweep, job.ready_at() - job.last), (2, False, ar.CHALLENGE_GAP))
        self.assertTrue(job.step())
        self.assertFalse(job.step())
        self.assertEqual((job.swept, job.fought), (1, 2))
        self.assertIn("扫荡 1，手打 2：扫荡能量不足", job.result.detail)
        self.assertEqual([(r.status, r.detail) for r in shown[:-1]],  # 每做完一次就更新自己那一行，不落后一次
                         [("progress", f"竞技场 {n}/3") for n in (0, 1, 2)])
        self.assertIs(shown[-1], job.result)


class PetTrialSim:
    """服务器模拟：第 1 周天 6 关，每关 begin 记住编号，end 记为通关；reset 清空通关并 +1 重置计数。"""

    def __init__(self, done=(), resets=0, complete_on_end=True):
        self.done, self.resets, self.current, self.complete = set(done), resets, None, complete_on_end

    def info(self, resets=None):
        return {"petDungeonSetting": {"layerId": 1, "dungeonHistory": {str(i): {"dailyCnt": 1} for i in self.done}},
                "everyDaySetting": {"petDungeonResetCnt": self.resets}}

    def handler(self, route, body):
        name = route.rsplit(".", 1)[1]
        if name == "beginChallengeDungeon":
            self.current = body["petDungeonId"]
        elif name == "endChallengeDugeon" and self.complete:
            self.done.add(self.current)
        elif name == "resetPetChallengeDungeon":
            self.done, self.resets = set(), self.resets + 1


def run_trial(sim, options=None):
    conn = Fake(sim.handler)
    with mock.patch.object(login, "enter", lambda link: sim.info()):
        r = run("pet_trial", conn, sim.info(), options or {})
    return r, conn


class PetTrialTests(unittest.TestCase):
    def test_runs_remaining_stages_in_order(self):
        sim = PetTrialSim(done={1, 2})
        r, conn = run_trial(sim)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([b["petDungeonId"] for b in conn.bodies("beginChallengeDungeon")], [3, 4, 5, 6])
        # 创建副本用的是表里的战斗 mid（900103…），不是 petDungeonId
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [900103, 900104, 900105, 900106])
        self.assertEqual(conn.bodies("endChallengeDugeon"), [{"win": 1}] * 4)
        self.assertEqual(sim.done, {1, 2, 3, 4, 5, 6})
        self.assertNotIn("resetPetChallengeDungeon", conn.routes())

    def test_all_done_uses_the_free_reset_once(self):
        sim = PetTrialSim(done={1, 2, 3, 4, 5, 6})
        r, conn = run_trial(sim)
        self.assertEqual(conn.bodies("resetPetChallengeDungeon"), [{"petDungeonId": 1}])
        self.assertEqual(len(conn.bodies("beginChallengeDungeon")), 6)
        self.assertEqual(r.status, "submitted")

    def test_free_reset_used_up_never_resets(self):
        sim = PetTrialSim(done={1, 2, 3, 4, 5, 6}, resets=1)
        r, conn = run_trial(sim)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_sparse_daily_reset_counter_uses_client_zero_default(self):
        sim = PetTrialSim(done={1, 2, 3, 4, 5, 6})
        original_info = sim.info

        def sparse_info():
            info = original_info()
            if sim.resets == 0:
                info['everyDaySetting'].pop('petDungeonResetCnt')
            return info

        sim.info = sparse_info
        r, conn = run_trial(sim)
        self.assertEqual(r.status, 'submitted', r.detail)
        self.assertEqual(len(conn.bodies('resetPetChallengeDungeon')), 1)
        self.assertEqual(len(conn.bodies('beginChallengeDungeon')), 6)

    def test_refresh_consumed_free_reset_never_sends_reset(self):
        sim = PetTrialSim(done={1, 2, 3, 4, 5, 6})
        info = sim.info()
        sim.resets = 1
        conn = Fake(sim.handler)
        with mock.patch.object(login, 'enter', lambda link: sim.info()):
            r = run('pet_trial', conn, info, {})
        self.assertEqual(r.status, 'skipped', r.detail)
        self.assertEqual(conn.sent, [])

    def test_corrupt_reset_counter_never_sends_reset(self):
        for value in (None, '', 'bad', -1, True, 0.5):
            with self.subTest(value=value):
                sim = PetTrialSim(done={1, 2, 3, 4, 5, 6})
                info = sim.info()
                info['everyDaySetting']['petDungeonResetCnt'] = value
                conn = Fake(sim.handler)
                r = run('pet_trial', conn, info, {})
                self.assertEqual(r.status, 'blocked', r.detail)
                self.assertEqual(conn.sent, [])

    def test_reset_refresh_invalid_or_failed_never_sends_reset(self):
        sim = PetTrialSim(done={1, 2, 3, 4, 5, 6})
        snapshots = [{},
                     {**sim.info(), 'everyDaySetting': {'petDungeonResetCnt': None}},
                     PetTrialSim(done={1, 2}).info()]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                conn = Fake(sim.handler)
                with mock.patch.object(login, 'enter', return_value=snapshot):
                    r = run('pet_trial', conn, sim.info(), {})
                self.assertEqual(r.status, 'blocked', r.detail)
                self.assertEqual(conn.sent, [])
        conn = Fake(sim.handler)
        with mock.patch.object(login, 'enter', side_effect=p.AllianceError('offline refresh failed')):
            r = run('pet_trial', conn, sim.info(), {})
        self.assertEqual(r.status, 'blocked', r.detail)
        self.assertEqual(conn.sent, [])

    def test_reset_count_unknown_blocks_without_reset(self):
        sim = PetTrialSim(done={1, 2, 3, 4, 5, 6})
        info = sim.info()
        info.pop("everyDaySetting")
        conn = Fake(sim.handler)
        r = run("pet_trial", conn, info, {})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_stage_not_confirmed_stops_before_next(self):
        sim = PetTrialSim(complete_on_end=False)
        r, conn = run_trial(sim)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.bodies("beginChallengeDungeon")), 1)

    def test_missing_progress_sends_nothing(self):
        conn = Fake()
        self.assertEqual(run("pet_trial", conn, {}, {}).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_end_timeout_is_uncertain(self):
        sim = PetTrialSim()

        def handler(route, body):
            if route.endswith("endChallengeDugeon"):
                raise TimeoutError("t")
            return sim.handler(route, body)
        with mock.patch.object(login, "enter", lambda link: sim.info()), self.assertRaises(UncertainOutcome):
            run("pet_trial", Fake(handler), sim.info(), {})


class SpecialGroupTests(unittest.TestCase):
    def test_catalogs_match_old_page(self):
        from zmxy.tasks import special as sp
        sizes = {gid: len(sp.catalog(types, boss)) for gid, _, types, boss in sp.GROUPS}
        self.assertEqual(sizes, {"nine_tower": 25, "ten_yama": 15, "forest": 12, "tree_king": 3})

    def test_tower_routes_follow_client_scene_order(self):
        self.assertEqual(dungeon.tower_route(100011), (100011, 100012, 100014))          # 坎宫：2、3 号房是并行分支
        self.assertEqual(dungeon.tower_route(100021), (100021, 100022, 100023, 100024, 100025, 100029))
        self.assertEqual(dungeon.tower_route(100091), (100091, 100092, 100093, 100099))
        self.assertIsNone(dungeon.tower_route(100101))

    def test_shared_quota_across_variants(self):
        info = {"dungeon": {"100011": {"dailyCount": 1}, "100101": {"dailyCount": 1}, "100201": {"dailyCount": 1}}}
        a, b, c = (quota.of(info, m) for m in (100011, 100101, 100301))
        self.assertEqual((a.left, b.left, c.left), (0, 0, 0))   # 25 个关共用 3 次，已用 3
        self.assertEqual(a.key, b.key)                           # 同一份次数，记账键必须相同
        self.assertEqual(quota.of({"dungeon": {}}, 700301).left, 2)
        self.assertEqual(quota.of({"dungeon": {}}, 300107).left, 4)
        self.assertEqual(quota.of({"dungeon": {}}, 400101).left, 3)

    def test_helper_groups_with_same_members_have_same_cap(self):
        seen = {}
        for r in data_rows("DungeonHelper"):
            cap = (data_row("Dungeon", int(r["id"])) or {}).get("dailyCountMax")
            self.assertEqual(seen.setdefault(str(r["shareDailyDungeonId"]), cap), cap)

    def test_nine_tower_walks_all_rooms_and_settles_once(self):
        conn = Fake()
        r = run("nine_tower", conn, {"dungeon": {}}, {"levels": [100021], "times": 1})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")],
                         [100021, 100022, 100023, 100024, 100025, 100029])
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)

    def test_tree_king_enters_boss_room(self):
        conn = Fake()
        run("tree_king", conn, {"dungeon": {}}, {"levels": [700201], "times": 1})
        self.assertEqual([b["mid"] for b in conn.bodies("createSinglePlayerCopy")], [700201, 700202])

    def test_stops_when_shared_count_used_up(self):
        conn = Fake()
        info = {"dungeon": {"300101": {"dailyCount": 4}}}
        r = run("forest", conn, info, {"levels": [300101, 300102], "times": 3, "mode": "manual"})
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])
        r = run("forest", conn, {"dungeon": {"300101": {"dailyCount": 3}}}, {"levels": [300101, 300102], "times": 3, "mode": "manual"})
        self.assertEqual(len(conn.bodies("createSinglePlayerCopy")), 1)   # 共用 4 次，已用 3，只剩 1 次

    def test_unknown_count_or_bad_level_or_no_choice(self):
        conn = Fake()
        self.assertEqual(run("ten_yama", conn, {}, {"levels": [400101], "times": 1}).status, "blocked")
        self.assertEqual(run("ten_yama", conn, {"dungeon": {}}, {"levels": [51], "times": 1}).status, "blocked")
        self.assertEqual(run("ten_yama", conn, {"dungeon": {}}, {}).status, "skipped")
        self.assertEqual(run("ten_yama", conn, {"dungeon": {}}, {"levels": [400101], "times": ""}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_finish_timeout_is_uncertain(self):
        def boom(route, body):
            if route == dungeon.R_FINISH:
                raise TimeoutError("t")
        with self.assertRaises(UncertainOutcome):
            run("ten_yama", Fake(boom), {"dungeon": {}}, {"levels": [400101], "times": 1})


def data_rows(name):
    from zmxy import state
    return state.table_rows(name)


def data_row(name, key):
    from zmxy import state
    return state.row(name, key)


class FulushouTests(unittest.TestCase):
    def info(self, vip=0, used=0):
        return {"vip": {"level": vip}, "dungeon": {"200101": {"dailyCount": used}}}

    def test_settles_with_score_100_and_no_pickup(self):
        conn = Fake()
        r = run("fulushou", conn, self.info(), {"levels": [200101], "times": 3})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "finishSingleCopy"])  # 免费 1 次
        self.assertEqual(conn.bodies("finishSingleCopy")[0]["sceneBuffScore"], 100)

    def test_vip3_gets_extra_attempt(self):
        conn = Fake()
        run("fulushou", conn, self.info(vip=3), {"levels": [200101], "times": 3})
        self.assertEqual(len(conn.bodies("createSinglePlayerCopy")), 2)
        conn = Fake()
        r = run("fulushou", conn, self.info(vip=3, used=2), {"levels": [200101], "times": 3})
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_unknown_vip_or_bad_level_blocks(self):
        conn = Fake()
        self.assertEqual(run("fulushou", conn, {"dungeon": {}}, {"levels": [200101], "times": 1}).status, "blocked")
        self.assertEqual(run("fulushou", conn, self.info(), {"levels": [51], "times": 1}).status, "blocked")
        self.assertEqual(run("fulushou", conn, self.info(), {}).status, "skipped")
        self.assertEqual(conn.sent, [])


class ForestPickupTests(unittest.TestCase):
    DROPS = [{"monsterId": m, "index": 0, "type": 3, "num": 1} for m in (700101, 700102, 700103)]

    def conn(self, selection):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": self.DROPS}
            if route == dungeon.R_SELECT_MONSTER:
                return selection
        return Fake(handler)

    def test_only_spawned_monster_drops_are_picked(self):
        conn = self.conn({"selectMonster": {"300101": {"700102": [100]}}})
        r = run("forest", conn, {"dungeon": {}}, {"levels": [300101], "times": 1, "mode": "manual"})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(len(conn.bodies("syncDropInfo")), 1)  # 3 条掉落里只有 700102 出场
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)

    def test_missing_selection_stops_before_pickup_and_settle(self):
        conn = self.conn({})
        r = run("forest", conn, {"dungeon": {}}, {"levels": [300101], "times": 1, "mode": "manual"})
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("syncDropInfo", conn.routes())
        self.assertNotIn("finishSingleCopy", conn.routes())

    def test_ambiguous_selection_stops(self):
        conn = self.conn({"selectMonster": {"300101": {"700101": [100], "700102": [100]}}})
        self.assertEqual(run("forest", conn, {"dungeon": {}}, {"levels": [300101], "times": 1, "mode": "manual"}).status, "blocked")
        self.assertNotIn("syncDropInfo", conn.routes())


@mock.patch("time.sleep", lambda s: None)
class ForestSweepTests(unittest.TestCase):
    OPT = {"levels": [300101], "times": 2}

    def info(self, **kw):
        base = {"power": 20000, "sweepEnergy": 40, "dungeon": {"300101": {"id": 300101, "passNum": 2, "dailyCount": 0}}}
        return {**base, **kw}

    def sweep_conn(self, energy=40):
        state = {"n": 2, "energy": energy}

        def handler(route, body):
            if route.endswith("sweep"):
                state["n"] += 1
                state["energy"] -= 10
                return {"sweepEnergy": state["energy"], "dungeon": {"300101": {"id": 300101, "passNum": state["n"]}}}
        return Fake(handler)

    def test_sweeps_with_updated_pass_num(self):
        conn = self.sweep_conn()
        r = run("forest", conn, self.info(), self.OPT)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("sweep"), [{"mid": 300101, "type": 0, "passNum": 2}, {"mid": 300101, "type": 0, "passNum": 3}])
        self.assertNotIn("getPower", conn.routes())  # 登录快照战力已够，不再查询

    def test_pass_num_updates_when_response_dungeon_is_the_single_record(self):
        """真实响应里 dungeon 就是这一关的记录（不是 {关卡id: 记录}）；认不出来 passNum 就不变，第二次请求和第一次一模一样 → 服务器回“重复请求”。"""
        n = {"v": 2}

        def handler(route, body):
            if route.endswith("sweep"):
                n["v"] += 1
                return {"dungeon": {"id": 300101, "dailyCount": 1, "passNum": n["v"]}}
        conn = Fake(handler)
        run("forest", conn, self.info(), self.OPT)
        self.assertEqual([b["passNum"] for b in conn.bodies("sweep")], [2, 3])

    def test_unverified_conditions_send_nothing(self):
        for info in (self.info(dungeon={"300101": {"id": 300101, "passNum": 0, "dailyCount": 0}}),   # 没通关
                     self.info(power=100),                                                           # 战力不够（getPower 也没给）
                     {k: v for k, v in self.info().items() if k != "sweepEnergy"},                   # 读不到能量
                     self.info(dungeon={})):                                                         # 没有该关记录
            conn = self.sweep_conn()
            r = run("forest", conn, info, self.OPT)
            self.assertIn(r.status, ("blocked", "skipped"))
            self.assertEqual(conn.bodies("sweep"), [])

    def test_power_read_from_server_when_snapshot_short(self):
        conn = Fake(lambda route, body: {"p1": 30000, "p2": 100} if route.endswith("getPower") else None)
        r = run("forest", conn, self.info(power=100), {"levels": [300101], "times": 1})
        self.assertEqual(len(conn.bodies("sweep")), 1, r.detail)

    def test_energy_runs_out_and_shared_count(self):
        conn = self.sweep_conn(energy=10)
        run("forest", conn, self.info(sweepEnergy=10), self.OPT)
        self.assertEqual(len(conn.bodies("sweep")), 1)
        conn = self.sweep_conn()
        used = self.info(dungeon={"300101": {"id": 300101, "passNum": 2, "dailyCount": 4}})  # 神兽森林共用 4 次，已用完
        self.assertEqual(run("forest", conn, used, self.OPT).status, "skipped")
        self.assertEqual(conn.bodies("sweep"), [])

    def test_rejection_stops_and_timeout_is_uncertain(self):
        conn = Fake(lambda route, body: "不能速通" if route.endswith("sweep") else None)
        r = run("forest", conn, self.info(), self.OPT)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.bodies("sweep")), 1)

        def boom(route, body):
            if route.endswith("sweep"):
                raise TimeoutError("t")
        with self.assertRaises(UncertainOutcome):
            run("forest", Fake(boom), self.info(), self.OPT)


class WanxiangTests(unittest.TestCase):
    OPT = {"levels": [300113], "times": 3}

    def conn(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": [{"monsterId": 1, "type": 3, "num": 1}, None]}
            if route == dungeon.R_TREASURE_DROP:
                return {"drop": [{"id": 1}, {"id": 2}]}
        return Fake(handler)

    def test_ground_drops_and_chest_then_settle_without_card(self):
        conn = self.conn()
        r = run("wanxiang_tree", conn, {"dungeonTreasure": {"1400600": {"remainCnt": 1}}}, self.OPT)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "getDungeonTreasureReward", "getDungeonTreasureDrop",
                                         "getDungeonTreasureReward", "getDungeonTreasureReward", "finishSingleCopy"])
        self.assertEqual(conn.bodies("createSinglePlayerCopy")[0]["costGem"], 0)
        self.assertEqual([b["i"] for b in conn.bodies("getDungeonTreasureReward")], [0, 0, 1])
        self.assertNotIn("getCard", conn.routes())

    def test_shared_count_used_up_or_unknown(self):
        conn = self.conn()
        self.assertEqual(run("wanxiang_tree", conn, {"dungeonTreasure": {"1400600": {"remainCnt": 0}}}, self.OPT).status, "skipped")
        self.assertEqual(run("wanxiang_tree", conn, {}, self.OPT).status, "blocked")
        self.assertEqual(run("wanxiang_tree", conn, {"dungeonTreasure": {}}, {"levels": [300113], "times": 1}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_chest_failure_does_not_settle(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7}
            if route == dungeon.R_TREASURE_DROP:
                return "宝箱异常"
        conn = Fake(handler)
        r = run("wanxiang_tree", conn, {"dungeonTreasure": {"1400600": {"remainCnt": 1}}}, self.OPT)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("finishSingleCopy", conn.routes())


# 测试快照（09-28）里还没有周限妖幡卷：按 2026-11 新表的 9 行补上（价格同原日限），其余字段只放代码会看的
WEEKLY_SCROLLS = {i: {"id": i, "name": f"{name}(周限)", "consume_type": "coin", "consume_subType": "fairyMoney",
                      "consume_count": str(price), "dayPurchaseMaxNum": -1, "weekPurchaseMaxNum": 35, "purchaseMaxNum": -1,
                      "underTime": "-1", "shelfTime": "-1"}
                  for i, name, price in zip(range(8022, 8031),
                                            ("瑶池妖幡卷一", "瑶池妖幡卷二", "瑶池妖幡卷三", "瑶池妖幡卷四", "神雷妖幡卷一",
                                             "神雷妖幡卷二", "神雷妖幡卷三", "神雷妖幡卷四", "北极妖幡卷一"),
                                            (40, 60, 90, 135, 200, 300, 450, 675, 1000))}
_shop_row = state.row


@mock.patch("time.sleep", lambda s: None)
@mock.patch.object(Session, "refresh", lambda s: s.info)  # 购买前重新进角：读回来和登录时一样
@mock.patch.object(state, "row", lambda table, key: WEEKLY_SCROLLS.get(state.num(key)) if table == "Shop" and state.num(key) in WEEKLY_SCROLLS else _shop_row(table, key))
class FairyFlagShopTests(unittest.TestCase):
    def info(self, money=1000000, **buy):
        return {"fairyMoney": money, "everyDaySetting": {},
                "everyWeekSetting": {"shopBuyCount": {str(k): {"buyCount": v} for k, v in buy.items()}}}

    def test_catalog_is_the_nine_weekly_scrolls(self):
        from zmxy.tasks import fairy_flag_shop as ff
        self.assertEqual([i for i, _ in ff.catalog()], list(range(8022, 8031)))
        self.assertEqual(ff.catalog()[0][1], "瑶池妖幡卷一")
        self.assertEqual(load_tasks()[ff.ID].group, "周常")

    def test_buys_remaining_up_to_weekly_limit(self):
        conn = Fake()
        r = run("fairy_flag_shop", conn, self.info(**{"8022": 30}), {"shop_ids": [8023, 8022]})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("buy"), [{"shopId": 8022, "count": 5, "password": ""}, {"shopId": 8023, "count": 35, "password": ""}])

    def test_old_daily_selection_asks_to_reselect(self):
        conn = Fake()
        r = run("fairy_flag_shop", conn, self.info(), {"shop_ids": [0, 8001]})
        self.assertEqual(r.status, "blocked")
        self.assertIn("重新选择", r.detail)
        self.assertEqual(conn.sent, [])

    def test_blocks_when_unaffordable_unknown_or_unreviewed(self):
        conn = Fake()
        self.assertEqual(run("fairy_flag_shop", conn, self.info(money=10), {"shop_ids": [8022]}).status, "blocked")
        self.assertEqual(run("fairy_flag_shop", conn, {"fairyMoney": 999, "everyDaySetting": {}}, {"shop_ids": [8022]}).status, "blocked")  # 无 everyWeekSetting
        self.assertEqual(run("fairy_flag_shop", conn, {"everyWeekSetting": {}}, {"shop_ids": [8022]}).status, "blocked")  # 无余额
        self.assertEqual(run("fairy_flag_shop", conn, self.info(), {"shop_ids": [101]}).status, "blocked")
        self.assertEqual(run("fairy_flag_shop", conn, self.info(), {}).status, "skipped")
        self.assertEqual(run("fairy_flag_shop", conn, self.info(**{"8022": 35}), {"shop_ids": [8022]}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_delisted_weekly_item_is_not_bought(self):
        from zmxy.tasks import fairy_flag_shop as ff
        self.assertIsNotNone(ff.good(8022))
        row = dict(WEEKLY_SCROLLS[8022], underTime="2026/12/1-23:59:59")
        with mock.patch.object(ff.state, "row", lambda table, key: row):
            self.assertIsNone(ff.good(8022))

    def test_cooldown_retries_other_errors_do_not(self):
        calls = []

        def cd(route, body):
            calls.append(1)
            return "购买CD中，请稍后再试" if len(calls) < 3 else None
        self.assertEqual(run("fairy_flag_shop", Fake(cd), self.info(), {"shop_ids": [8022]}).status, "submitted")
        self.assertEqual(len(calls), 3)
        conn = Fake(lambda r, b: "商品不存在")
        self.assertEqual(run("fairy_flag_shop", conn, self.info(), {"shop_ids": [8022]}).status, "blocked")
        self.assertEqual(len(conn.sent), 1)

    def test_timeout_is_uncertain(self):
        def boom(route, body):
            raise TimeoutError("t")
        with self.assertRaises(UncertainOutcome):
            run("fairy_flag_shop", Fake(boom), self.info(), {"shop_ids": [8022]})


class AncientTests(unittest.TestCase):
    OPT = {"levels": [1400101], "times": 2}
    INFO = {"dungeonTreasure": {"1400100": {"remainCnt": 1}}}

    def conn(self, drop=None):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": [{"monsterId": 1, "type": 3}]}  # 地面掉落不管
            if route == dungeon.R_TREASURE_DROP:
                return {"drop": [{"id": 1}]} if drop is None else drop
        return Fake(handler)

    def test_flow_and_shared_count(self):
        conn = self.conn()
        r = run("ancient_wilderness", conn, self.INFO, self.OPT)
        self.assertEqual(r.status, "submitted", r.detail)  # 次数 1，只打一次
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "getDungeonTreasureDrop",
                                         "getDungeonTreasureReward", "finishSingleCopy"])
        self.assertEqual(conn.bodies("createSinglePlayerCopy")[0]["mid"], 1400101)  # 多人版
        self.assertNotIn("costGem", conn.bodies("createSinglePlayerCopy")[0])
        self.assertNotIn("getCard", conn.routes())

    def test_unknown_count_blocks_and_used_up_skips(self):
        conn = self.conn()
        self.assertEqual(run("ancient_wilderness", conn, {}, self.OPT).status, "blocked")
        self.assertEqual(run("ancient_wilderness", conn, {"dungeonTreasure": {"1400100": {"remainCnt": 0}}}, self.OPT).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_bad_level_and_missing_chest_array(self):
        conn = self.conn()
        self.assertEqual(run("ancient_wilderness", conn, self.INFO, {"levels": [490], "times": 1}).status, "blocked")
        self.assertEqual(run("ancient_wilderness", conn, self.INFO, {"levels": [1410101], "times": 1}).status, "blocked")  # 不提供单人版
        self.assertEqual(conn.sent, [])
        conn = self.conn(drop={})  # 宝箱响应没有 drop 数组：不结算
        self.assertEqual(run("ancient_wilderness", conn, self.INFO, self.OPT).status, "blocked")
        self.assertNotIn("finishSingleCopy", conn.routes())


@mock.patch("time.sleep", lambda s: None)
class ArenaInterleaveTests(unittest.TestCase):
    """竞技场/斗宠手打的冷却穿插在其他任务的请求之间（完整 run_account 流程）。"""

    class LoginSession(Session):
        conn = None

        def __enter__(self):
            self._conn, self.info = self.conn, {}
            self._link = login.Link(self.conn, self.config, [{"index": 0}], {"time": ts(2026, 10, 1)})
            return self

        def __exit__(self, *exc):
            self._conn = self._link = None

    def flow(self, tasks, opts, other_requests=4, jjc="3;3;3;3;3;3;3", gaps=True, after=None, stop=None, account_profile=None):
        def handler(route, body):
            name = route.rsplit(".", 1)[1]
            if name == "getArenaInfo":
                return arena_info(jjcChallengeNum=jjc, sweepEnergy=0)
            if name in ("getArenaTargetInfo", "getArenaUserData"):
                return {"userData": {"name": "x", "entityId": "e"}}
        conn = Fake(handler)
        order = []

        def other(ctx):
            for _ in range(other_requests):
                ctx.request("x.y", {})
                order.append(len([r for r, _ in conn.sent if r.endswith("endArenaBattle")]))
            if after:
                after()
            return core.TaskResult("other", "submitted", "ok")
        registry = {**load_tasks(), "other": core.TaskSpec("other", "other", other, reads=frozenset({"x.y"}))}

        class S(self.LoginSession):
            pass
        S.conn = conn
        acc = core.Account("a", p.ClientConfig(uid=1, username="u"), tasks, options=opts, account_profile=account_profile or {})
        patches = [mock.patch("zmxy.session.Session", S), mock.patch("zmxy.runner.load_tasks", return_value=registry)]
        if gaps:  # 去掉冷却，只看穿插顺序
            patches += [mock.patch("zmxy.tasks.arena.CHALLENGE_GAP", 0.0)]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        emitted = []
        out = core.run_account(acc, stop=stop, emit=lambda n, r: emitted.append(r))
        self.emitted = emitted
        return conn, out, order

    def test_fights_happen_between_other_tasks_requests(self):
        conn, out, order = self.flow(("other", "arena"), {"arena": {"mode": "manual"}})
        self.assertEqual(len(conn.bodies("endArenaBattle")), 3)
        self.assertTrue(any(n > 0 for n in order[:-1]), order)  # 其他任务还没发完请求时就已经打过了
        rows = [r for r in out if r.task == "arena"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].status, "submitted")
        self.assertIn("已挑战 3/3", rows[0].detail)

    def test_placeholder_row_is_shown_as_progress_and_replaced_by_final_result(self):
        conn, out, order = self.flow(("other", "arena"), {"arena": {"mode": "manual"}})
        rows = [r for r in self.emitted if r.task == "arena"]
        statuses = [r.status for r in rows]
        self.assertEqual(set(statuses), {"progress", "submitted"}, statuses)  # 占位不再显示成“跳过”
        first = statuses.index("submitted")
        self.assertEqual(set(statuses[first:]), {"submitted"}, statuses)  # 显示成已完成后不再退回“进行中”
        self.assertIn("已挑战 3/3", rows[first].detail)
        last_other = max(i for i, r in enumerate(self.emitted) if r.task == "other")
        self.assertLess(self.emitted.index(rows[first]), last_other)  # 打满当场显示已完成，不等其他任务做完
        self.assertEqual((rows[-1].status, "手打穿插" in rows[-1].detail), ("submitted", False))  # 最终结果整个替换占位文字
        self.assertEqual(sum(r.task == "arena" and r.status == "submitted" for r in self.emitted), 1)
        self.assertFalse(any(r.pending for r in out))

    def test_progress_row_is_updated_after_each_fight(self):
        self.flow(("other", "arena"), {"arena": {"mode": "manual"}})
        shown = [r.detail for r in self.emitted if r.task == "arena" and r.status == "progress"]
        steps = [d for i, d in enumerate(shown) if i == 0 or d != shown[i - 1]]  # 本体的占位行会把当前进度再写一遍
        self.assertEqual(steps, ["竞技场 0/3", "竞技场 1/3", "竞技场 2/3"])  # 打完一次就更新，不落后一次

    def test_finished_arena_keeps_its_result_when_stopped_afterwards(self):
        import threading
        stop = threading.Event()
        for tasks in (("arena", "other"), ("other", "arena")):  # 停止时竞技场的本体已经给过占位行 / 还没轮到
            stop.clear()
            conn, out, order = self.flow(tasks, {"arena": {"mode": "manual"}}, after=stop.set, stop=stop)
            self.assertEqual(order[-1], 3)  # 停止前已经穿插着打满
            rows = [(r.status, r.detail) for r in out if r.task == "arena"]
            self.assertEqual([st for st, _ in rows], ["submitted"], (tasks, rows))
            self.assertIn("已挑战 3/3", rows[0][1])
            last = [r for r in self.emitted if r.task == "arena"][-1]
            self.assertEqual(last.status, "submitted", tasks)  # 界面上也不会退回“跳过 / 未执行完”
            self.assertEqual(sum(r.task == "arena" and r.status == "submitted" for r in self.emitted), 1)

    def test_unfinished_arena_is_settled_as_not_done_when_stopped(self):
        import threading
        stop = threading.Event()
        conn, out, order = self.flow(("arena", "other"), {"arena": {"mode": "manual"}}, other_requests=1, gaps=False, after=stop.set, stop=stop)
        rows = [(r.status, r.detail) for r in out if r.task == "arena"]
        self.assertEqual([st for st, _ in rows], ["skipped"], rows)
        self.assertIn("未执行完", rows[0][1])

    def test_cooldown_is_respected_and_finale_drains_the_rest(self):
        conn, out, order = self.flow(("other", "arena"), {"arena": {"mode": "manual"}}, other_requests=6, gaps=False)
        self.assertEqual(max(order), 1)  # 冷却 10 秒没到：别的任务发请求期间最多打一次
        self.assertEqual(len(conn.bodies("endArenaBattle")), 3)  # 剩下的在收尾里打完
        self.assertEqual([r.status for r in out if r.task == "arena"], ["submitted"])
        self.assertEqual(sum(r.task == "arena" and r.status == "submitted" for r in self.emitted), 1)

    def test_arena_and_pet_interleave_independently(self):
        opts = {"arena": {"mode": "manual"}, "pet_arena": {"mode": "manual"}}
        conn, out, order = self.flow(("arena", "other", "pet_arena"), opts, jjc="2;2;2;2;2;2;2")
        begins = [r.split(".")[1] for r, _ in conn.sent if r.endswith("beginArenaBattle")]
        self.assertEqual(sorted(begins), ["arenaHandler"] * 2 + ["petArenaHandler"] * 2)
        self.assertEqual({r.task: r.status for r in out}, {"arena": "submitted", "pet_arena": "submitted", "other": "submitted"})
        for task_id in ("arena", "pet_arena"):
            self.assertEqual(sum(r.task == task_id and r.status == "submitted" for r in self.emitted), 1)

    def test_both_arena_finals_are_emitted_once_with_saved_account_profile(self):
        opts = {"arena": {"mode": "manual"}, "pet_arena": {"mode": "manual"}}
        conn, out, _ = self.flow(("arena", "other", "pet_arena"), opts,
                                 jjc="5;5;5;5;5;5;5", account_profile={"saved_marker": True})
        for task_id, handler in (("arena", "arenaHandler"), ("pet_arena", "petArenaHandler")):
            self.assertEqual(sum(route == f"scene.{handler}.endArenaBattle" for route, _ in conn.sent), 5)
            finals = [r for r in self.emitted if r.task == task_id and r.status == "submitted"]
            self.assertEqual(len(finals), 1, finals)

    def test_used_up_arena_is_reported_by_prelude_and_never_pumped(self):
        conn, out, order = self.flow(("other", "arena"), {"arena": {"mode": "manual"}}, jjc="0;0;0;0;0;0;0")
        self.assertEqual(conn.bodies("beginArenaBattle"), [])
        self.assertEqual([(r.task, r.status) for r in out if r.task == "arena"], [("arena", "skipped")])
