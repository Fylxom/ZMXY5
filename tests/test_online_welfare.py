# 在线福利领取顺序、刷新交接和停止边界的离线回归。
import threading
import unittest

from tests.support import FakeConn, ts
from zmxy import core, login, protocol as p, quest
from zmxy.tasks import online_welfare as welfare
from zmxy.tasks.double_drop import ONLINE_CLAIMED, online_quest_id


FRIDAY = ts(2026, 10, 2, 12)


class OnlineWelfareTests(unittest.TestCase):
    def setUp(self):
        self.first = online_quest_id()
        self.chain = welfare.reward_chain()
        self.assertIsNotNone(self.first)
        self.assertGreaterEqual(len(self.chain), 2)

    @staticmethod
    def info(statuses):
        return {"quest": [{"id": qid, "status": status} for qid, status in statuses.items()]}

    def context(self, reader, commit=None, stop=None):
        def write(body):
            if isinstance(commit, Exception):
                raise commit
            return commit(body) if callable(commit) else commit or {}

        conn = FakeConn({p.ROUTE_ENTER: lambda body: {"info": reader(), "time": FRIDAY},
                         quest.R_COMMIT: write})
        session = core.Session(p.ClientConfig(uid=1, username="u"))
        session._conn = conn
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": FRIDAY})
        session.bind(core.load_tasks()[welfare.ID])
        ctx = core.TaskContext(session, stop=stop)
        self.slept = []
        ctx.sleep = lambda seconds, background=True: self.slept.append(seconds) or not (stop and stop.is_set())  # 不真等
        return ctx, conn

    def test_claims_first_then_refreshes_before_unlocking_each_next_reward(self):
        received = set()
        reads = []
        order = [self.first, *self.chain]

        def reader():
            reads.append(len(received))
            statuses = {qid: quest.DONE for qid in received}
            remaining = [qid for qid in order if qid not in received]
            if remaining:
                statuses[remaining[0]] = quest.READY
            return self.info(statuses)

        def commit(body):
            self.assertNotIn(body["qId"], received)
            received.add(body["qId"])
            return {}

        ctx, conn = self.context(reader, commit)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.SUBMITTED)
        self.assertEqual([body["qId"] for body in conn.bodies(quest.R_COMMIT)], order)
        self.assertEqual(reads, list(range(len(order))))

    def test_session_confirmed_first_is_not_claimed_again(self):
        statuses = {self.first: quest.DOING, self.chain[0]: quest.DOING}
        ctx, conn = self.context(lambda: self.info(statuses))
        ctx.session.carry[ONLINE_CLAIMED] = "yes"
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.SKIPPED)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [])

    def test_unready_reward_prevents_skipping_to_a_later_ready_reward(self):
        statuses = {self.first: quest.DONE, self.chain[0]: quest.DOING, self.chain[1]: quest.READY}
        ctx, conn = self.context(lambda: self.info(statuses))
        self.assertEqual(welfare.run(ctx).status, core.SKIPPED)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [])

    def test_partial_claim_with_unready_later_tiers_is_not_completed(self):
        self.assertGreaterEqual(len(self.chain), 4)
        claimable, remaining = self.chain[:-2], self.chain[-2:]
        received = set()

        def reader():
            statuses = {self.first: quest.DONE, **{qid: quest.DONE for qid in received}}
            next_id = next(qid for qid in self.chain if qid not in received)
            statuses[next_id] = quest.READY if next_id in claimable else quest.DOING
            return self.info(statuses)

        def commit(body):
            received.add(body["qId"])
            return {"give": [{"type": "item", "subType": "101018001", "count": 2}]} if len(received) == 1 else {}

        ctx, conn = self.context(reader, commit)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.BLOCKED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": qid} for qid in claimable])
        self.assertIn("仙织革×2", result.detail)
        for qid in self.chain:
            self.assertIn(welfare.reward_name(qid), result.detail)
        self.assertIn("尚未确认可领取", result.detail)
        self.assertNotIn("用户停止", result.detail)
        self.assertFalse(any(qid in received for qid in remaining))

    def test_first_only_claim_with_unready_chain_is_not_completed(self):
        received = set()

        def reader():
            if not received:
                return self.info({self.first: quest.READY})
            return self.info({self.first: quest.DONE, self.chain[0]: quest.DOING})

        def commit(body):
            received.add(body["qId"])
            return {}

        ctx, conn = self.context(reader, commit)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.BLOCKED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": self.first}])
        self.assertIn("已领取 在线福利双倍掉宝率", result.detail)
        self.assertIn(welfare.reward_name(self.chain[-1]), result.detail)

    def test_later_run_claims_remaining_tiers_without_reclaiming_received_tiers(self):
        received = set()
        later_ready = False

        def reader():
            statuses = {self.first: quest.DONE, **{qid: quest.DONE for qid in received}}
            pending = [qid for qid in self.chain if qid not in received]
            if pending:
                statuses[pending[0]] = quest.READY if later_ready or pending[0] == self.chain[0] else quest.DOING
            return self.info(statuses)

        def commit(body):
            self.assertNotIn(body["qId"], received)
            received.add(body["qId"])
            return {}

        ctx, conn = self.context(reader, commit)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.BLOCKED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": self.chain[0]}])

        later_ready = True
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": qid} for qid in self.chain])
        self.assertNotIn("尚未满足", result.detail)

    def test_user_stop_after_claim_retains_confirmed_reward_and_unclaimed_tiers(self):
        stop = threading.Event()

        def commit(body):
            stop.set()
            return {"give": [{"type": "item", "subType": "101018001", "count": 2}]}

        ctx, conn = self.context(
            lambda: self.info({self.first: quest.DONE, self.chain[0]: quest.READY}), commit, stop=stop)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.BLOCKED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": self.chain[0]}])
        self.assertIn("仙织革×2", result.detail)
        self.assertIn("用户停止，尚未领取", result.detail)
        for qid in self.chain[1:]:
            self.assertIn(welfare.reward_name(qid), result.detail)

    def test_missing_quest_state_blocks_without_claim(self):
        ctx, conn = self.context(lambda: {})
        self.assertEqual(welfare.run(ctx).status, core.BLOCKED)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [])

    def test_server_progress_can_unlock_stale_doing_status(self):
        received = set()

        def reader():
            statuses = {self.first: quest.DONE, **{qid: quest.DONE for qid in received}}
            pending = [qid for qid in self.chain if qid not in received]
            if not pending:
                return self.info(statuses)
            qid = pending[0]
            statuses[qid] = quest.DOING
            info = self.info(statuses)
            from zmxy import state
            row = state.row("OnlineReward", qid)
            info["quest"][-1]["progress"] = {str(row["targetId"]): row["targetNum"]}
            return info

        def commit(body):
            self.assertNotIn(body["qId"], received)
            received.add(body["qId"])
            return {}

        ctx, conn = self.context(reader, commit)
        self.assertEqual(welfare.run(ctx).status, core.SUBMITTED)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": qid} for qid in self.chain])

    def test_invalid_or_insufficient_progress_never_sends_claim(self):
        from zmxy import state
        qid = self.chain[0]
        row = state.row("OnlineReward", qid)
        for progress in (None, {}, {str(row["targetId"]): "bad"}):
            with self.subTest(progress=progress):
                info = self.info({self.first: quest.DONE, qid: quest.DOING})
                info["quest"][-1]["progress"] = progress
                ctx, conn = self.context(lambda: info)
                welfare.run(ctx)
                self.assertEqual(conn.bodies(quest.R_COMMIT), [])

    def test_waits_online_until_last_tier_is_ready_then_claims_it(self):
        # 2026-11-13 真机：第 5 档（20 分钟）progress=3/1200，原来不等直接结束，没领到
        from zmxy import state
        last, total = self.chain[-2], self.chain[-1]
        row = state.row("OnlineReward", last)
        received = {self.first, *self.chain[:-2]}

        def reader():
            statuses = {qid: quest.DONE for qid in received}
            if last not in received:
                statuses[last] = quest.DOING
                info = self.info(statuses)
                done = row["targetNum"] if sum(self.slept) >= row["targetNum"] - 3 else 3
                info["quest"][-1]["progress"] = {str(row["targetId"]): done}
                return info
            statuses[total] = quest.DOING
            return self.info(statuses)

        def commit(body):
            received.add(body["qId"])
            return {}

        ctx, conn = self.context(reader, commit)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": last}, {"qId": total}])
        self.assertGreaterEqual(sum(self.slept), row["targetNum"] - 3)
        self.assertLessEqual(sum(self.slept), welfare.WAIT_MAX)

    def test_claims_after_local_countdown_even_if_server_progress_is_stale(self):
        # 2026-11-15 真机：第 5 档等了 9 分钟，服务器记录一直是 1029/1200，三轮等完没领。客户端是本地倒计时到点就提交
        from zmxy import state
        last, total = self.chain[-2], self.chain[-1]
        row = state.row("OnlineReward", last)
        received = {self.first, *self.chain[:-2]}

        def reader():
            statuses = {qid: quest.DONE for qid in received}
            if last not in received:
                statuses[last] = quest.DOING
                info = self.info(statuses)
                info["quest"][-1]["progress"] = {str(row["targetId"]): row["targetNum"] - 171}  # 一直不涨
                return info
            statuses[total] = quest.DOING
            return self.info(statuses)

        def commit(body):
            received.add(body["qId"])
            return {}

        ctx, conn = self.context(reader, commit)
        result = welfare.run(ctx)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": last}, {"qId": total}])
        self.assertGreaterEqual(sum(self.slept), 171)
        self.assertLess(sum(self.slept), 400)  # 只等一轮

    def test_local_countdown_refused_is_reported_not_retried(self):
        from zmxy import state
        last = self.chain[-2]
        row = state.row("OnlineReward", last)
        statuses = {qid: quest.DONE for qid in (self.first, *self.chain[:-2])}
        statuses[last] = quest.DOING
        info = self.info(statuses)
        info["quest"][-1]["progress"] = {str(row["targetId"]): row["targetNum"] - 100}
        ctx, conn = self.context(lambda: info, lambda body: "任务未完成")
        result = welfare.run(ctx)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": last}])
        self.assertIn("领取被拒绝", result.detail)
        self.assertEqual(result.status, core.BLOCKED)

    def test_cumulative_reward_requires_all_previous_tiers_done(self):
        order = [self.first, *self.chain]
        qid = order[-1]
        statuses = dict.fromkeys(order, quest.DONE)
        statuses[qid] = quest.DOING
        self.assertTrue(welfare.claim_ready(self.info(statuses), qid, statuses, order))
        statuses[order[1]] = quest.DOING
        self.assertFalse(welfare.claim_ready(self.info(statuses), qid, statuses, order))

    def test_stop_during_refresh_prevents_first_claim(self):
        stop = threading.Event()

        def reader():
            stop.set()
            return self.info({self.first: quest.READY})

        ctx, conn = self.context(reader, stop=stop)
        self.assertEqual(welfare.run(ctx).status, core.SKIPPED)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [])

    def test_unknown_first_claim_is_not_retried_or_followed_by_other_claims(self):
        ctx, conn = self.context(lambda: self.info({self.first: quest.READY}), TimeoutError("timeout"))
        with self.assertRaises(core.UncertainOutcome):
            welfare.run(ctx)
        self.assertEqual(conn.bodies(quest.R_COMMIT), [{"qId": self.first}])
