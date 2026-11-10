"""登录有礼（15 天签到）。全部离线。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from zmxy import state
from zmxy.core import UncertainOutcome
from zmxy.tasks import login_gift
from tests.support import FakeConn, run_daily as run

CLAIM = "scene.activityHandler.getSummerLoginRewards"
NOW = int(datetime(2026, 10, 5, 12, tzinfo=timezone(timedelta(hours=8))).timestamp())
AID = "2042"


def info(days, zhizun=0, claimed=None):
    return {"summerLogin": {AID: {"days": days, "zhizun": zhizun, "totalReward": claimed or {}}}}


def go(conn, i):
    return run("login_gift", conn, i, now=NOW)


class LoginGiftTests(unittest.TestCase):
    def claimed(self, premium=False):
        return {str(row["id"]): 1 for row in state.table_rows("LandingReward15")
                if row["levelType"] == login_gift.FREE or premium}

    def test_end_requires_last_day_and_all_rewards_for_current_tier(self):
        free = self.claimed()
        self.assertTrue(login_gift.login_gift_end(info(15, claimed=free)))
        self.assertFalse(login_gift.login_gift_end(info(14, claimed=free)))
        self.assertFalse(login_gift.login_gift_end(info(15, zhizun=1, claimed=free)))
        self.assertTrue(login_gift.login_gift_end(info(15, zhizun=1, claimed=self.claimed(premium=True))))
        self.assertIsNone(login_gift.login_gift_end({"summerLogin": {}}))

    def test_completed_save_is_skipped_and_marked_ended(self):
        conn = FakeConn()
        r = go(conn, info(15, claimed=self.claimed()))
        self.assertEqual((r.status, conn.sent, r.profile),
                         ("skipped", [], {login_gift.END: True}))

    def test_final_day_claim_refreshes_save_before_marking_ended(self):
        full = self.claimed()
        last = max(map(int, full))
        before = {key: value for key, value in full.items() if int(key) != last}
        conn = FakeConn({CLAIM: {}})
        with mock.patch("zmxy.login.enter", return_value=info(15, claimed=full)):
            r = go(conn, info(15, claimed=before))
        self.assertEqual((r.status, r.profile), ("submitted", {login_gift.END: True}))
        self.assertEqual(conn.sent, [(CLAIM, {})])

    def test_free_account_claims_free_only(self):
        conn = FakeConn({CLAIM: {}})
        r = go(conn, info(3))
        self.assertEqual((r.status, conn.sent), ("submitted", [(CLAIM, {})]))
        self.assertEqual(r.profile, {login_gift.END: False})
        self.assertIn("免费奖励 ×3", r.detail)

    def test_premium_account_counts_both_levels(self):
        conn = FakeConn({CLAIM: {}})
        r = go(conn, info(3, zhizun=1))
        self.assertIn("高级+免费奖励 ×6", r.detail)

    def test_already_claimed_is_skipped_without_request(self):
        conn = FakeConn()
        r = go(conn, info(2, claimed={"1": 1, "2": 1}))
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_premium_only_pending_for_free_account_is_skipped(self):
        conn = FakeConn()
        self.assertEqual(go(conn, info(1, claimed={"1": 1})).status, "skipped")
        self.assertEqual(conn.sent, [])
        self.assertEqual(go(FakeConn({CLAIM: {}}), info(1, zhizun=1, claimed={"1": 1})).status, "submitted")

    def test_missing_progress_blocks(self):
        for i in ({}, {"summerLogin": {}}, {"summerLogin": {AID: {"days": 3}}}, {"summerLogin": {AID: {"days": "x", "totalReward": {}}}}):
            conn = FakeConn()
            self.assertEqual(go(conn, i).status, "blocked")
            self.assertEqual(conn.sent, [])

    def test_outside_activity_window_is_skipped(self):
        conn = FakeConn()
        r = run("login_gift", conn, info(3), now=int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()))
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_transport_failure_is_uncertain_and_not_resent(self):
        conn = FakeConn(exc=TimeoutError("t"))
        with self.assertRaises(UncertainOutcome):
            go(conn, info(3))
        self.assertEqual(len(conn.sent), 1)


if __name__ == "__main__":
    unittest.main()
