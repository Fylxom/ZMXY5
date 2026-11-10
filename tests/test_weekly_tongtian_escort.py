"""通天河报名雇佣 / 护送经书（周常页）。全部离线。"""

import unittest
from unittest import mock

from zmxy.core import UncertainOutcome
from zmxy.tasks import tongtian_escort as te
from tests.support import Fake, run

NOW = 1_000_000_000_000
TRIP = 15 * 60_000


def info(trips=0, refreshed=0, good=1, escort=None, applied=0):
    river = {"goodType": 0, "goodId": good}
    if escort:
        river["escortData"] = escort
    return {"uid": 1, "tongtianRiver": river,
            "everyDaySetting": {"tongtianRiverEscortNum": trips, "tongtianRiverEscortRefreshNum": refreshed, "tongtianHireApply": applied}}


class Server:
    """每次刷新档次 +1（最高 4）；护送 15 分钟；getReward 清掉 escortData。"""

    def __init__(self, good=1, ongoing=False, now=NOW):
        self.good, self.ongoing, self.now = good, ongoing, now

    def __call__(self, route, body):
        name = route.rsplit(".", 1)[1]
        if name == "getEscortList":
            return {"list": [], "serverTime": self.now}
        if name == "freshEscortGood":
            self.good = min(4, self.good + 1)
            return {"tongtianRiver": {"goodType": 0, "goodId": self.good}}
        if name == "escort":
            self.ongoing = True
            return {"tongtianRiver": {"goodType": 0, "goodId": self.good, "escortData": {"endTime": self.now + TRIP}}}
        if name == "getReward":
            self.ongoing = False
            self.good = 1
            return {"tongtianRiver": {"goodType": 0, "goodId": 1}, "give": [{"itemId": 101000021, "count": 30}]}


@mock.patch("time.sleep", lambda s: None)
class HireTests(unittest.TestCase):
    def test_applies_once(self):
        conn = Fake()
        self.assertEqual(run("tongtian_hire", conn, info(), {}).status, "submitted")
        self.assertEqual(conn.routes(), ["hireApply"])

    def test_skips_when_applied_and_blocks_without_state(self):
        conn = Fake()
        self.assertEqual(run("tongtian_hire", conn, info(applied=1), {}).status, "skipped")
        self.assertEqual(run("tongtian_hire", conn, {"uid": 1}, {}).status, "blocked")
        self.assertEqual(conn.sent, [])


class EscortTests(unittest.TestCase):
    def go(self, server, data, options=None):
        # 等待不真的睡：ctx.sleep 走 stop.wait，这里直接换成立即返回
        with mock.patch("zmxy.core.TaskContext.sleep", lambda self, s, background=True: True):
            conn = Fake(server)
            return conn, run("tongtian_escort", conn, data, options or {})

    def test_default_plan_is_3_1_1_and_stops_at_best_good(self):
        conn, r = self.go(Server(), info())
        self.assertEqual(r.status, "submitted", r.detail)
        # 第 1 条：1→4 只需刷 3 次；第 2、3 条各刷 1 次（服务器领奖后档次回到 1）
        self.assertEqual(conn.routes().count("freshEscortGood"), 5)
        self.assertEqual(conn.routes().count("escort"), 3)
        self.assertEqual(conn.routes().count("getReward"), 2)  # 第 3 条不等，下次运行时领
        self.assertIn("护送 3 条船，刷新 5 次", r.detail)
        self.assertIn("最后这条船的经书下次运行时领", r.detail)
        self.assertIn("经书", r.detail)

    def test_custom_plan_and_best_good_stops_refresh(self):
        conn, r = self.go(Server(good=3), info(good=3), {"refresh1": 3, "refresh2": 0, "refresh3": 0})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes().count("freshEscortGood"), 1)  # 3→4 后到顶，剩下的次数不刷

    def test_refresh_never_exceeds_daily_allowance(self):
        conn, r = self.go(Server(), info(refreshed=4), {"refresh1": 5, "refresh2": 5, "refresh3": 5})
        self.assertEqual(conn.routes().count("freshEscortGood"), 1)  # 今日只剩 1 次免费刷新

    def test_resumes_from_trips_already_done_and_picks_matching_plan_slot(self):
        conn, r = self.go(Server(), info(trips=2, refreshed=3), {"refresh1": 3, "refresh2": 1, "refresh3": 0})
        self.assertEqual(conn.routes().count("escort"), 1)
        self.assertEqual(conn.routes().count("freshEscortGood"), 0)  # 第 3 条船的计划是 0 次

    def test_claims_ongoing_ship_before_starting_new_one(self):
        ongoing = {"endTime": NOW + 60_000}
        conn, r = self.go(Server(ongoing=True), info(trips=1, escort=ongoing), {"refresh2": 0, "refresh3": 0})
        self.assertEqual(conn.routes()[:2], ["getEscortList", "getReward"])
        self.assertEqual(conn.routes().count("escort"), 2)

    def test_last_ship_before_window_closes_is_not_waited_for(self):
        """离时段结束还有 7 分钟、还剩 2 条船：发一条就结束，不等它领经书（下次运行时领）。"""
        with mock.patch.object(te.timeplan, "open_until", return_value=NOW / 1000 + 7 * 60):
            conn, r = self.go(Server(), info(trips=1))
        self.assertEqual(conn.routes().count("escort"), 1)
        self.assertEqual(conn.routes().count("getReward"), 0)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("还有 1 条船没发", r.detail)
        self.assertIn("下次运行时领", r.detail)

    def test_next_ship_still_fits_keeps_going(self):
        """离结束还有 20 分钟：第一条回来时还在时段里，接着发第二条；第二条回来时已结束，就不等它。"""
        server = Server()
        until = NOW / 1000 + 20 * 60

        def claim_later(route, body):
            out = server(route, body)
            if route.endswith("getReward"):
                server.now += TRIP  # 领奖时已经过了 15 分钟
            return out

        with mock.patch.object(te.timeplan, "open_until", side_effect=lambda tid, now: until):
            conn, r = self.go(claim_later, info())
        self.assertEqual(conn.routes().count("escort"), 2)
        self.assertEqual(conn.routes().count("getReward"), 1)
        self.assertIn("还有 1 条船没发", r.detail)

    def test_last_of_three_is_not_waited_for(self):
        conn, r = self.go(Server(), info(trips=2))
        self.assertEqual((conn.routes().count("escort"), conn.routes().count("getReward")), (1, 0))
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertNotIn("没发", r.detail)

    def test_leftover_last_ship_is_claimed_next_day(self):
        """隔天：先领昨天留下的第 3 条，再发今天的。"""
        conn, r = self.go(Server(ongoing=True), info(trips=0, escort={"endTime": NOW - 60_000}))
        self.assertEqual(conn.routes()[:2], ["getEscortList", "getReward"])
        self.assertEqual(conn.routes().count("escort"), 3)

    def test_closed_window_sends_nothing_but_claims_leftover(self):
        with mock.patch.object(te.timeplan, "closed", return_value="现在不在开放时间"):
            conn, r = self.go(Server(), info())
            self.assertEqual((r.status, conn.routes()), ("skipped", []))
            conn, r = self.go(Server(ongoing=True), info(escort={"endTime": NOW + 60_000}))
        self.assertEqual(conn.routes().count("getReward"), 1)  # 上次留下的船照样领
        self.assertEqual(conn.routes().count("escort"), 0)

    def test_all_trips_used_is_skipped(self):
        conn, r = self.go(Server(), info(trips=3))
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_unconfirmed_escort_response_stops_without_retry(self):
        def server(route, body):
            if route.endswith("escort"):
                return {"tongtianRiver": {"goodId": 1}}  # 没有 escortData
            return Server()(route, body)

        conn, r = self.go(server, info(), {"refresh1": 0})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes().count("escort"), 1)

    def test_server_rejection_of_escort_is_blocked(self):
        conn, r = self.go(lambda route, body: "护送次数已用尽" if route.endswith("escort") else Server()(route, body), info(), {"refresh1": 0})
        self.assertEqual(r.status, "blocked")
        self.assertIn("护送次数已用尽", r.detail)

    def test_missing_state_is_blocked(self):
        conn, r = self.go(Server(), {"uid": 1, "everyDaySetting": {}})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_uncertain_write_reports_progress(self):
        def server(route, body):
            if route.endswith("getReward"):
                raise UncertainOutcome("超时")
            return Server()(route, body)

        with self.assertRaises(UncertainOutcome) as cm:
            self.go(server, info(), {"refresh1": 0})
        self.assertIn("已出发 1 条船", str(cm.exception))

    def test_plan_defaults_and_bad_values(self):
        self.assertEqual(te.refresh_plan({}), [3, 1, 1])
        self.assertEqual(te.refresh_plan({"refresh1": 2, "refresh2": "x", "refresh3": -1}), [2, 1, 1])

class InterleaveTests(unittest.TestCase):
    """发船后不空等：船到点的领奖、发下一条穿插在其他任务的请求里。"""

    def setUp(self):
        from zmxy import login, protocol as p
        from zmxy.core import Session, TaskContext, load_tasks
        self.server = Server()
        self.conn = Fake(self.server)
        s = self.session = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = self.conn, info()
        from tests.support import ts
        s._link = login.Link(self.conn, s.config, [{"index": 0}], {"time": ts(2026, 10, 1)})  # 开放时段里（测试数据表国庆前后每天 12–15 点）
        self.spec = load_tasks()["tongtian_escort"]
        s.bind(self.spec)
        self.ctx = TaskContext(s, {"refresh1": 0, "refresh2": 0, "refresh3": 0})

    def test_prelude_sails_one_ship_then_other_requests_claim_and_resail(self):
        self.assertIsNone(self.spec.prelude(self.ctx))
        self.assertEqual(self.conn.routes().count("escort"), 1)
        self.assertEqual(self.conn.routes().count("getReward"), 0)
        self.session.pump()  # 还没到点：不动
        self.assertEqual(self.conn.routes().count("getReward"), 0)
        voyage = self.session._voyage
        voyage.end_at = 0.0  # 到点
        self.session.pump()
        self.assertEqual(self.conn.routes().count("getReward"), 1)
        self.assertEqual(self.conn.routes().count("escort"), 2)
        self.assertEqual(self.session._spec, self.spec)

    def test_finale_finishes_remaining_ships_and_reports(self):
        with mock.patch("zmxy.core.TaskContext.sleep", lambda self, s, background=True: True):
            self.assertIsNone(self.spec.prelude(self.ctx))
            body = self.spec.run(self.ctx)
            self.assertTrue(body.pending)
            final = self.spec.finale(self.ctx)
        self.assertEqual(final.status, "submitted", final.detail)
        self.assertIn("护送 3 条船", final.detail)
        self.assertEqual(self.conn.routes().count("getReward"), 2)
        self.assertIsNone(self.session.background)

    def sail_all(self, ctx):
        """船一到点就由别的任务的请求领掉、发下一条，直到三条都发出（第三条不等）。"""
        for _ in range(3):
            self.session._voyage.end_at = 0.0
            self.session.pump()

    def test_last_ship_claimed_in_passing_shows_final_result_at_once(self):
        shown = []
        ctx = self.ctx.__class__(self.session, self.ctx.options, report=shown.append)
        self.assertIsNone(self.spec.prelude(ctx))
        self.assertTrue(self.spec.run(ctx).pending)
        self.sail_all(ctx)
        self.assertEqual([(r.status, "护送 3 条船" in r.detail) for r in shown], [("submitted", True)])  # 不等收尾
        self.assertIsNone(self.session.background)
        final = self.spec.finale(ctx)  # 收尾只是把这份结果交出去，换掉占位行
        self.assertEqual((final.status, final.detail), (shown[0].status, shown[0].detail))
        self.assertEqual(self.conn.routes().count("getReward"), 2)

    def test_ships_done_before_the_body_runs_give_one_result(self):
        self.assertIsNone(self.spec.prelude(self.ctx))
        self.sail_all(self.ctx)
        body = self.spec.run(self.ctx)
        self.assertEqual((body.status, body.pending), ("submitted", False), body.detail)
        self.assertIsNone(self.spec.finale(self.ctx))  # 已经交过，不重复并入

    def test_first_ship_refused_is_reported_by_the_prelude(self):
        self.conn.handler = lambda route, body: "护送失败" if route.endswith(".escort") else self.server(route, body)
        r = self.spec.prelude(self.ctx)
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertIsNone(self.session.background)

    def test_task_inserted_while_waiting_may_claim_the_ship_itself(self):
        """等船时插进来的任务（ctx.extra）自己的请求触发了领奖发船：收尾接着等新的那条，不重复领。"""
        fired = []

        def inserted():
            if not fired:
                fired.append(1)
                self.session._voyage.end_at = 0.0  # 到点
                self.session.pump()  # 插进来的任务发请求 → 穿插点领奖、发下一条
                self.assertEqual(self.conn.routes().count("getReward"), 1)

        with mock.patch("zmxy.core.TaskContext.sleep", lambda self, s, background=True: True):
            self.assertIsNone(self.spec.prelude(self.ctx))
            ctx = self.ctx.__class__(self.session, self.ctx.options, extra=inserted)
            final = self.spec.finale(ctx)
        self.assertEqual((final.status, fired), ("submitted", [1]), final.detail)
        self.assertIn("护送 3 条船", final.detail)
        self.assertEqual((self.conn.routes().count("escort"), self.conn.routes().count("getReward")), (3, 2))
        self.assertEqual(self.spec.hosts, True)

    def test_other_save_claim_is_left_to_the_finale(self):
        ctx = self.ctx.__class__(self.session, {"claim_record": 5, "refresh1": 0}, )
        self.assertIsNone(self.spec.prelude(ctx))
        self.session._voyage.end_at = 0.0
        self.session.pump()
        self.assertEqual(self.conn.routes().count("getReward"), 0)

    def test_coexists_with_arena_hook(self):
        calls = []
        self.session.hook("arena", lambda: calls.append(1))
        self.assertIsNone(self.spec.prelude(self.ctx))
        self.session._voyage.end_at = 0.0
        self.session.pump()
        self.assertTrue(calls)  # 竞技场那类回调照常被调用
        self.assertEqual(self.conn.routes().count("getReward"), 1)
        self.session.hook("arena", None)
        self.assertIsNotNone(self.session.background)  # 护送的回调还在


if __name__ == "__main__":
    unittest.main()
