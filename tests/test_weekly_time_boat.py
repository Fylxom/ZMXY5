"""时光之舟（周常页）。全部离线。"""

import unittest
from unittest import mock

from zmxy import guard
from zmxy.core import UncertainOutcome, load_tasks
from tests.support import Fake, run

NINE = {"levels": [3], "times": 3}  # 九宫玄塔：表里每次 1 点、每周 9 次，关卡 100011… 按顺序


def info(energy=10, used=0, daily=None, key="timeEnergy"):  # used=None：服务器没给 timeEnergy 对象
    return {"energy": energy, "everyWeekSetting": {key: {"3": used}} if used is not None else {},
            "dungeon": daily if daily is not None else {"100021": {"id": 100021, "dailyCount": 1}}}


class BoardTests(unittest.TestCase):
    def test_profile_and_board_show_energy_remaining_and_fight_counts(self):
        from zmxy.tasks import time_boat
        snap = {"energy": 8, "everyWeekSetting": {"timeEnergy": {"3": 2}},
                "dungeon": {"100011": {"id": 100011, "dailyCount": 1}}, "dungeonTreasure": {"1400200": {"remainCnt": 1}}}
        prof = time_boat.profile(snap)["time_boat"]
        self.assertEqual((prof["energy"], prof["max"]), (8, 12))
        self.assertEqual(prof["rows"]["3"]["remain"], 7)   # 每周 9 次，已兑换 2
        self.assertEqual(prof["rows"]["3"]["fight"], 2)    # 九宫共享 3 次，已用 1
        self.assertEqual((prof["rows"]["4"]["fight"], prof["rows"]["4"]["cap"]), (1, 2))
        data = time_boat.board({"profile": {"time_boat": prof}})
        self.assertEqual((data["energy"], data["max"]), (8, 12))
        self.assertEqual(data["rows"][0], (3, "九宫玄塔", 1, "7", "2/3"))
        empty = time_boat.board({})  # 没读取过：显示 —，不当成 0
        self.assertEqual(empty["rows"][0][3:], ("—", "—"))

    def test_estimate_adds_regen_per_midnight_up_to_max(self):
        from datetime import datetime, timedelta, timezone
        from zmxy.tasks import time_boat
        at = lambda *d: datetime(2026, *d, tzinfo=timezone(timedelta(hours=8))).timestamp()
        acc = {"profile": {"time_boat": {"energy": 3, "max": 12, "at": at(10, 6, 23)}}}
        with mock.patch("zmxy.data.row", return_value={"timeEnergyRePoint": 2}):
            self.assertEqual(time_boat.estimate(acc, at(10, 6, 23, 59)), 3)   # 同一天：不加
            self.assertEqual(time_boat.estimate(acc, at(10, 7, 0, 1)), 5)     # 过了一个 0 点
            self.assertEqual(time_boat.estimate(acc, at(10, 9, 12)), 9)
            self.assertEqual(time_boat.estimate(acc, at(10, 30, 12)), 12)     # 不超过上限
            self.assertEqual(time_boat.estimate(acc, at(10, 5, 12)), 3)       # 时钟往回拨：不减
        self.assertIsNone(time_boat.estimate({}, at(10, 7)))


class UrgentTests(unittest.TestCase):
    """名字变红：九宫 = 幻兽法宝周第二周，神兽森林 = 灵宠宝石周第二周，噩梦 = 周六周日；本周读过且剩余 0 才不标。"""

    def at(self, *d):
        from datetime import datetime, timedelta, timezone
        return datetime(2026, *d, tzinfo=timezone(timedelta(hours=8))).timestamp()

    def urgent(self, week, now, snap=None, first=None):  # week：现在是哪个养成周的第二周；first：是哪个养成周但还在第一周
        from zmxy.tasks import time_boat
        with mock.patch("zmxy.state.growth_second_week", return_value=week), mock.patch("zmxy.state.growth_now", return_value=first or week):
            return time_boat.urgent({"profile": {"time_boat": snap}} if snap else {}, now)

    def test_growth_week_picks_its_offer_and_weekend_adds_nightmare(self):
        tue, sat, sun = self.at(10, 6, 12), self.at(10, 10, 12), self.at(10, 11, 23)
        self.assertEqual(self.urgent(None, tue), set())
        self.assertEqual(self.urgent(2, tue), {3})   # 幻兽法宝 → 九宫玄塔
        self.assertEqual(self.urgent(3, tue), {2})   # 灵宠宝石 → 神兽森林
        self.assertEqual(self.urgent(1, tue), set())
        self.assertEqual(self.urgent(None, sat), {4})
        self.assertEqual(self.urgent(3, sun), {2, 4})

    def test_first_week_reminds_on_weekend_only_and_second_week_asks_again(self):
        fri, sat, mon = self.at(10, 2, 12), self.at(10, 3, 12), self.at(10, 5, 9)
        self.assertEqual(self.urgent(None, fri, first=3), set())
        self.assertEqual(self.urgent(None, sat, first=3), {2, 4})
        self.assertEqual(self.urgent(None, sat, first=2), {3, 4})
        used = {"at": sat, "rows": {"2": {"remain": 0}, "4": {"remain": 0}}}
        self.assertEqual(self.urgent(None, sat, used, first=3), set())
        self.assertEqual(self.urgent(3, mon, used), {2})  # 第一周用完了：周一重置，第二周重新提醒

    def test_growth_now_prefers_live_and_stops_at_table_end(self):
        from zmxy import state
        from tests.test_auto_config import ReserveTests
        rows = ReserveTests.ROWS
        self.assertEqual(state.growth_now(self.at(10, 3, 12), rows), 3)
        self.assertIsNone(state.growth_now(self.at(10, 9, 1), rows))
        self.assertEqual(state.growth_now(self.at(10, 9, 12), rows, live=4), 4)

    def test_used_up_this_week_is_not_marked(self):
        sat = self.at(10, 10, 12)
        rows = {"2": {"remain": 0}, "4": {"remain": 1}}
        self.assertEqual(self.urgent(3, sat, {"at": self.at(10, 5, 1), "rows": rows}), {4})
        rows["4"]["remain"] = 0
        self.assertEqual(self.urgent(3, sat, {"at": self.at(10, 5, 1), "rows": rows}), set())
        self.assertEqual(self.urgent(3, sat, {"at": self.at(10, 4, 23), "rows": rows}), {2, 4})  # 上周读的：周一已重置
        self.assertEqual(self.urgent(3, sat, {"at": self.at(10, 5, 1), "rows": {"2": {"remain": None}}}), {2, 4})


class FightRoundsTests(unittest.TestCase):
    """兑换并挑战：换一批打一批。九宫上限 3、每周 9 次；挑战交给九宫任务（这里换成假的）按它自己的设置打。"""

    def setUp(self):
        self.used, self.week, self.energy, self.fights, self.fail_at, self.eat = 3, 0, 12, [], None, 3

    def info(self):
        return {"energy": self.energy, "everyWeekSetting": {"timeEnergy": {"3": self.week}},
                "dungeon": {"100011": {"id": 100011, "dailyCount": self.used}} if self.used else {}}

    def handler(self, route, body):
        if route.endswith("copyBuyCountByEnergy"):
            self.used, self.week, self.energy = self.used - 1, self.week + 1, self.energy - 1
            return {"dungeon": {"id": body["dungeonId"], "dailyCount": self.used}}

    def fight(self, ctx):
        from zmxy.core import TaskResult
        self.fights.append((dict(ctx.options), 3 - self.used))
        if self.fail_at == len(self.fights):
            return TaskResult("nine_tower", "blocked", "停止于 一层：背包已满")
        self.used = min(3, self.used + self.eat)
        return TaskResult("nine_tower", "submitted", "已结算 一层×3")

    def go(self, times=9, nine={"levels": [100011], "times": 3}, **extra):
        from types import SimpleNamespace
        from zmxy import login, protocol as p
        from zmxy.core import Session, TaskContext
        from zmxy.tasks import time_boat
        s, conn = Session(p.ClientConfig(uid=1, username="u")), Fake(self.handler)
        s._conn, s.info = conn, self.info()
        s._link = login.Link(conn, s.config, [{"index": 0}], {})
        s.bind(load_tasks()["time_boat"])
        ctx = TaskContext(s, {"levels": [3], "times": times, "fight": True, **extra}, all_options={"nine_tower": nine})
        spec = SimpleNamespace(id="nine_tower", title="九宫玄塔", run=self.fight)
        with mock.patch.dict(time_boat.REGISTRY, {"nine_tower": spec}), mock.patch.object(login, "enter", lambda link: self.info()):
            return time_boat._run(ctx, time_boat.Wrote()), conn, ctx

    def test_nine_times_with_cap_three_is_three_rounds(self):
        r, conn, ctx = self.go()
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 9)
        self.assertEqual(self.fights, [({"levels": [100011], "times": 3}, 3)] * 3)  # 每轮换 3 次打 3 次，设置原样沿用
        self.assertIn("兑换 9 次（分 3 轮）", r.detail)
        self.assertEqual((self.week, self.used, ctx.progress), (9, 3, {"n": 9}))

    def test_unused_counts_are_fought_first_and_energy_limits_the_rest(self):
        self.used, self.energy = 0, 4
        r, conn, _ = self.go()
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([left for _, left in self.fights], [3, 3, 1])  # 先打掉原有的 3 次；能量只够换 4 次
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 4)
        self.assertIn("时光能量不足", r.detail)

    def test_bag_full_keeps_exchanged_count_and_resume_does_not_exchange_again(self):
        self.fail_at = 2
        r, conn, ctx = self.go()
        self.assertEqual(r.status, "blocked")
        self.assertIn("背包已满", r.detail)
        self.assertEqual((len(conn.bodies("copyBuyCountByEnergy")), ctx.progress), (6, {"n": 6}))
        self.fail_at = None
        r, conn, ctx = self.go(resume_done={"n": 6})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 3)  # 先打掉换来没打的 3 次，再换剩下的 3 次
        self.assertEqual((len(self.fights), self.week), (4, 9))

    def test_stops_when_the_fight_uses_nothing_or_nothing_is_chosen(self):
        self.eat = 0
        r, conn, _ = self.go()
        self.assertEqual(r.status, "blocked")
        self.assertEqual((len(conn.bodies("copyBuyCountByEnergy")), len(self.fights)), (3, 1))
        r, conn, _ = self.go(nine={"levels": []})
        self.assertEqual((r.status, conn.sent), ("blocked", []))


class TimeBoatTests(unittest.TestCase):
    def copy_server(self):
        def handler(route, body):
            if route.endswith("copyBuyCountByEnergy"):
                return {"dungeon": {"id": body["dungeonId"], "dailyCount": 5}}
        return Fake(handler)

    def test_exchanges_for_first_used_dungeon_and_stays_within_energy(self):
        conn = self.copy_server()
        r = run("time_boat", conn, info(energy=2), NINE)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("copyBuyCountByEnergy"), [{"energyId": 3, "dungeonId": 100021}] * 2)  # 能量只够 2 次
        self.assertIn("消耗时光能量 2 点", r.detail)

    def test_limited_by_weekly_remaining_and_zero_means_skip(self):
        conn = self.copy_server()
        r = run("time_boat", conn, info(used=8), NINE)
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 1)  # 每周 9 次，已用 8
        self.assertEqual(run("time_boat", conn, info(used=9), NINE).status, "skipped")
        self.assertEqual(run("time_boat", conn, info(energy=0), NINE).status, "skipped")  # 不用时光水晶补足

    def test_crystals_top_up_energy_only_when_confirmed(self):
        bag = {"item": [{"id": 101027001, "count": 5}]}
        base = {**info(energy=1), "bag": bag}
        # 没确认（没有 crystal 选项）：能量只够 1 次，不碰水晶
        conn = self.copy_server()
        r = run("time_boat", conn, dict(base), {"levels": [3], "times": 2})
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 1)
        self.assertNotIn("useItem", conn.routes())
        # 确认过：第 2 次前用 1 个水晶，刷新后能量够了再兑换
        conn = self.copy_server()
        with mock.patch("zmxy.core.Session.refresh", lambda s: {**info(energy=1), "bag": bag}):
            r = run("time_boat", conn, dict(base), {"levels": [3], "times": 2, "crystal": True})
        self.assertEqual(r.status, "submitted", r.detail)
        sent = conn.bodies("useItem")
        self.assertEqual([{k: b[k] for k in ("index", "count", "id")} for b in sent], [{"index": 0, "count": 1, "id": "101027001"}])  # password 由会话按需补
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 2)
        self.assertIn("1 个时光水晶", r.detail)

    def test_crystals_not_enough_limits_planned_times(self):
        conn = self.copy_server()
        r = run("time_boat", conn, {**info(energy=0), "bag": {"item": [{"id": 101027001, "count": 1}]}},
                {"levels": [3], "times": 3, "crystal": True})
        self.assertEqual(conn.routes().count("useItem"), 1)  # 只有 1 个水晶、每次 1 点 → 最多兑换 1 次
        self.assertEqual(r.status, "blocked")  # 补足后服务器能量仍没到（假连接没刷新），停止且不继续

    def test_unreadable_bag_blocks_when_crystals_requested(self):
        r = run("time_boat", self.copy_server(), info(energy=0), {"levels": [3], "times": 1, "crystal": True})
        self.assertEqual(r.status, "blocked")

    def test_unknown_state_blocks_without_request(self):
        conn = self.copy_server()
        bad = [info(used=None), {**info(), "energy": None}, {"energy": 5, "dungeon": {}}]
        for i in bad:
            self.assertEqual(run("time_boat", conn, i, NINE).status, "blocked")
        self.assertEqual(run("time_boat", conn, info(daily={}), NINE).status, "blocked")  # 没有 dailyCount>0 的关卡
        self.assertEqual(conn.sent, [])

    def test_unselected_blank_times_and_bad_target(self):
        conn = self.copy_server()
        self.assertEqual(run("time_boat", conn, info(), {}).status, "skipped")
        self.assertEqual(run("time_boat", conn, info(), {"levels": [3], "times": ""}).status, "skipped")
        self.assertEqual(run("time_boat", conn, info(), {"levels": [6], "times": 1}).status, "blocked")  # 藏宝洞不在可选项里
        self.assertEqual(run("time_boat", conn, info(), {"levels": [3, 2], "times": 1}).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_missing_daily_count_in_response_stops(self):
        conn = Fake(lambda route, body: {})
        r = run("time_boat", conn, info(), NINE)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.bodies("copyBuyCountByEnergy")), 1)

    def test_nightmare_needs_counter_plus_one(self):
        good = {"energy": 9, "everyWeekSetting": {"timeEnergy": {"4": 0}}}
        conn = Fake(lambda route, body: {"everyWeekSetting": {"timeEnergy": {"4": 1}}})
        r = run("time_boat", conn, good, {"levels": [4], "times": 1})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("buyDungeonTreasureCnt"), [{"energyId": 4}])
        conn = Fake(lambda route, body: {})
        with mock.patch("zmxy.core.Session.refresh", lambda s: good):  # 刷新后仍是 0：不算成功，不重发
            r = run("time_boat", conn, good, {"levels": [4], "times": 3})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.bodies("buyDungeonTreasureCnt")), 1)

    def test_timeout_uncertain_and_no_ticket_routes(self):
        def boom(route, body):
            raise TimeoutError("t")
        conn = Fake(boom)
        with self.assertRaises(UncertainOutcome):
            run("time_boat", conn, info(), NINE)
        self.assertEqual(len(conn.sent), 1)
        spec = load_tasks()["time_boat"]
        for route in spec.writes:  # 兑换路由已审核（只耗时光能量），其余付费路由仍被拒
            self.assertIsNone(guard.check(route, {"energyId": 3}, spec.spends))
        self.assertIsNotNone(guard.check("scene.activityHandler.buyDungeonTreasureCnt", {"energyId": 4}))  # 没登记就被拒
        self.assertIsNotNone(guard.check(next(iter(spec.writes)), {"energyId": 3, "costGem": 1}, spec.spends))


if __name__ == "__main__":
    unittest.main()
