"""登高采菊100–120固定分数挑战的离线协议契约测试。"""

import threading
import unittest
from unittest import mock

from zmxy import state
from zmxy.tasks.events import game_high_jump
from zmxy.core import UncertainOutcome, load_tasks
from tests.support import FakeConn, day, run, ts


START = "scene.activityHandler.beginClimbHigh"
END = "scene.activityHandler.endClimbHigh"
CONFIG = {"activityId": 2066, "freeNum": 3}
NOW = ts(2026, 10, 13)


class GameHighJumpTests(unittest.TestCase):
    def run_task(self, conn, info=None, now=NOW, activity_open=True, log=None, account_profile=None):
        info = day(climbFreeNum=0) if info is None else info
        with mock.patch.object(state, "row", side_effect=lambda table, key: CONFIG if table == "ClimbHigh" else None), \
             mock.patch.object(state, "activity_open", return_value=activity_open):
            return run("game_high_jump", conn, info, now=now, log=log, account_profile=account_profile)

    def test_uses_one_random_account_score_for_each_remaining_free_attempt(self):
        conn = FakeConn({END: {"give": [{"itemId": 101022013, "count": 10}]}})
        account_profile = {}

        with mock.patch.object(game_high_jump.random, "randint", return_value=117) as draw:
            result = self.run_task(conn, account_profile=account_profile)

        self.assertEqual(result.status, "submitted")
        self.assertEqual(conn.routes(), [START, END, START, END, START, END])
        self.assertEqual(conn.bodies(START), [{}, {}, {}])
        self.assertEqual([body["score"] for body in conn.bodies(END)], [117, 117, 117])
        self.assertEqual(draw.call_count, 1)
        self.assertEqual(account_profile, {game_high_jump.SCORE_KEY: 117})
        self.assertEqual(result.account_profile, {game_high_jump.SCORE_KEY: 117})

    def test_reuses_saved_account_score_without_randomizing_again(self):
        conn = FakeConn()
        account_profile = {game_high_jump.SCORE_KEY: 104}

        with mock.patch.object(game_high_jump.random, "randint", side_effect=AssertionError("不应再次随机")):
            result = self.run_task(conn, account_profile=account_profile)

        self.assertEqual(result.status, "submitted")
        self.assertEqual([body["score"] for body in conn.bodies(END)], [104, 104, 104])
        self.assertIsNone(result.account_profile)

    def test_logs_counted_progress_like_dungeon_tasks(self):
        conn = FakeConn()
        logs = []

        with mock.patch.object(game_high_jump.random, "randint", return_value=107):
            result = self.run_task(conn, log=logs.append)

        self.assertEqual(result.status, "submitted")
        self.assertEqual(logs, [
            "登高采菊 0/3",
            "登高采菊 1/3",
            "登高采菊 2/3",
            "登高采菊 3/3",
        ])
        self.assertEqual(result.detail, "已结算 登高采菊×3")

    def test_does_not_declare_or_use_paid_retry_route(self):
        spec = load_tasks()["game_high_jump"]

        self.assertEqual(spec.title, "登高采菊")
        self.assertIsNone(spec.choices)
        self.assertEqual(spec.spends, frozenset())
        self.assertEqual(spec.writes, frozenset({START, END}))
        self.assertNotIn("scene.activityHandler.beginClimbHighWithTicket", spec.writes)

    def test_used_free_count_skips_without_request(self):
        conn = FakeConn()

        result = self.run_task(conn, day(climbFreeNum=3))

        self.assertEqual(result.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_missing_daily_state_blocks_without_request(self):
        conn = FakeConn()

        result = self.run_task(conn, {})

        self.assertEqual(result.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_closed_activity_skips_without_request(self):
        conn = FakeConn()
        result = self.run_task(conn, day(climbFreeNum=0), activity_open=False)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_stop_after_partial_run_is_blocked_and_keeps_new_score(self):
        conn = FakeConn()
        stop = threading.Event()
        conn.responses[END] = lambda body: stop.set() or {}
        info = day(climbFreeNum=0)

        with mock.patch.object(state, "row", side_effect=lambda table, key: CONFIG if table == "ClimbHigh" else None),              mock.patch.object(state, "activity_open", return_value=True),              mock.patch.object(game_high_jump.random, "randint", return_value=110):
            result = run("game_high_jump", conn, info, now=NOW, stop=stop)

        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("已结算 登高采菊×1", result.detail)
        self.assertIn("用户停止", result.detail)
        self.assertEqual(result.account_profile, {game_high_jump.SCORE_KEY: 110})

    def test_rejection_after_partial_run_is_blocked_without_resend(self):
        ends = []

        def end(body):
            ends.append(body)
            return "拒绝" if len(ends) == 2 else {}

        conn = FakeConn({END: end})
        with mock.patch.object(game_high_jump.random, "randint", return_value=110):
            result = self.run_task(conn)

        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("已结算 登高采菊×1", result.detail)
        self.assertEqual(len(conn.bodies(END)), 2)

    def test_transport_failure_is_uncertain_and_not_resent(self):
        conn = FakeConn(exc=TimeoutError("t"))

        with self.assertRaises(UncertainOutcome):
            self.run_task(conn)

        self.assertEqual(conn.sent, [(START, {})])


if __name__ == "__main__":
    unittest.main()
