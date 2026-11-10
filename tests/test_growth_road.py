import unittest
from unittest import mock

from zmxy import core, login, protocol as p
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from zmxy.tasks import growth_road as g
from tests.support import FakeConn


def info(quests=(), day=0, week=0, day_claimed=(), week_claimed=()):
    return {
        "quest": [{"id": q, "status": s} for q, s in quests],
        "everyDaySetting": {"activePoint": day, "livenessReward": {str(i): 1 for i in day_claimed}},
        "everyWeekSetting": {"activePoint": week, "livenessReward": {str(i): 1 for i in week_claimed}},
    }


def make(conn, *snapshots):
    """snapshots：依次作为 refresh 返回的角色信息。"""
    s = Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s._link = conn, object()
    s.bind(load_tasks()[g.ID])
    it = iter(snapshots)

    def enter(_link):
        s.info = next(it)
        return s.info

    patcher = mock.patch.object(login, "enter", enter)
    patcher.start()
    return s, TaskContext(s), patcher


class GrowthRoadTests(unittest.TestCase):
    def run_task(self, conn, *snapshots):
        s, ctx, patcher = make(conn, *snapshots)
        try:
            return load_tasks()[g.ID].run(ctx)
        finally:
            patcher.stop()

    def test_runs_last(self):
        self.assertTrue(load_tasks()[g.ID].last)

    def test_nothing_to_claim_is_skipped(self):
        conn = FakeConn()
        r = self.run_task(conn, info([(990001, 1)], day=20))
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_claims_quest_then_rewards_from_refreshed_points(self):
        conn = FakeConn()
        before = info([(990001, 2)], day=20)
        after = info([(990001, 1)], day=30, week=0)  # 领任务后服务器把活跃度加到 30
        r = self.run_task(conn, before, after)
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.sent, [(g.quest.R_COMMIT, {"qId": 990001}), (g.ROUTE_REWARD, {"rewardId": 1})])

    def test_claimed_and_weekly_rewards(self):
        conn = FakeConn()
        r = self.run_task(conn, info(day=60, week=450, day_claimed=[1]))
        self.assertEqual(r.status, "submitted")
        self.assertEqual([b for _, b in conn.sent], [{"rewardId": 2}, {"rewardId": 11}])

    def test_missing_quest_state_blocks(self):
        conn = FakeConn()
        r = self.run_task(conn, {"everyDaySetting": {"activePoint": 99}})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_missing_points_blocks_rewards_instead_of_assuming_zero(self):
        conn = FakeConn()
        r = self.run_task(conn, {"quest": []})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_quest_failure_stops_and_does_not_claim_rewards(self):
        conn = FakeConn({g.quest.R_COMMIT: "不可领取"})
        r = self.run_task(conn, info([(990001, 2), (990002, 2)], day=100))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.sent), 1)

    def test_timeout_is_uncertain_and_not_retried(self):
        conn = FakeConn(exc=TimeoutError("t"))
        with self.assertRaises(UncertainOutcome):
            self.run_task(conn, info([(990001, 2)]))
        self.assertEqual(len(conn.sent), 1)


class OrderTests(unittest.TestCase):
    def test_last_task_moves_to_end(self):
        calls = []

        def fake(tid, last):
            return core.TaskSpec(tid, tid, lambda ctx, t=tid: calls.append(t) or core.TaskResult(t, "submitted"), last=last)

        reg = {"a": fake("a", True), "b": fake("b", False), "c": fake("c", False)}
        session = mock.MagicMock()
        session.__enter__.return_value = session
        with mock.patch("zmxy.runner.load_tasks", return_value=reg), mock.patch("zmxy.session.Session", return_value=session):
            core.run_account(core.Account("x", p.ClientConfig(uid=1, username="u"), tasks=("a", "b", "c")))
        self.assertEqual(calls, ["b", "c", "a"])


if __name__ == "__main__":
    unittest.main()
