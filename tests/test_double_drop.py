"""双倍掉宝首档与节日 buff；在线福利后续档在所有任务之后领取。全部离线。"""

import unittest
from datetime import datetime, timedelta, timezone

from tests.support import FakeConn, run_daily as run
from zmxy import core

COMMIT, HOLIDAY = "scene.questHandler.commitQuest", "scene.activityHandler.getResetActivityReward"
CN = timezone(timedelta(hours=8))


def at(month, day):
    return int(datetime(2026, month, day, 12, tzinfo=CN).timestamp())


FRI, WED, LATE = at(10, 2), at(10, 7), at(10, 20)  # 周五（在线+十一）、周三（只有十一）、活动都结束
ONLINE = 960001


def info(online=None, claimed=None):
    out = {"everyDaySetting": {"resetActivityReward": {}}}
    if online is not None:
        out["quest"] = [{"id": ONLINE, "status": online}]
    if claimed:
        out["everyDaySetting"]["resetActivityReward"][str(claimed)] = 1
    return out


class DoubleDropTests(unittest.TestCase):
    def test_runs_before_normal_tasks_like_fulushou(self):
        registry = core.load_tasks()
        spec = registry["double_drop"]
        self.assertTrue(spec.first)
        self.assertEqual(spec.group, "活动")
        self.assertFalse(spec.default)
        welfare = registry["online_welfare"]
        self.assertTrue(welfare.terminal)
        self.assertFalse(welfare.first)
        self.assertIsNone(welfare.window)
        self.assertEqual(core.display_rank("online_welfare"), core.display_rank("double_drop") + 1)

    def test_friday_claims_online_first_reward_and_holiday_buff(self):
        conn = FakeConn({COMMIT: {}, HOLIDAY: {}})
        r = run("double_drop", conn, info(online=1), now=FRI)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.sent, [(COMMIT, {"qId": ONLINE}), (HOLIDAY, {"activeId": 2046})])

    def test_ready_status_also_commits(self):
        conn = FakeConn()
        run("double_drop", conn, info(online=2), now=FRI)
        self.assertEqual(conn.bodies(COMMIT), [{"qId": ONLINE}])

    def test_wednesday_only_holiday_buff(self):
        conn = FakeConn()
        r = run("double_drop", conn, info(online=1), now=WED)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes(), [HOLIDAY])

    def test_already_claimed_sends_nothing(self):
        conn = FakeConn()
        r = run("double_drop", conn, info(online=3, claimed=2046), now=FRI)
        self.assertEqual((r.status, conn.sent), ("skipped", []))
        self.assertIn("已领过", r.detail)

    def test_unknown_holiday_claim_state_does_not_send_request(self):
        conn = FakeConn()
        r = run("double_drop", conn, {"quest": [{"id": ONLINE, "status": 1}]}, now=WED)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])
        self.assertIn("无法确认已领取次数", r.detail)

    def test_malformed_holiday_claim_state_does_not_send_request(self):
        conn = FakeConn()
        r = run("double_drop", conn, {"everyDaySetting": {"resetActivityReward": "bad"}}, now=WED)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_nothing_open_is_skipped(self):
        conn = FakeConn()
        r = run("double_drop", conn, info(online=1), now=LATE)
        self.assertEqual(r.status, "skipped", r.detail)
        self.assertEqual(conn.sent, [])

    def test_unknown_time_blocks(self):
        conn = FakeConn()
        self.assertEqual(run("double_drop", conn, info(), now=None).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_rejection_is_reported_but_other_buff_still_sent(self):
        conn = FakeConn({COMMIT: "未到领取时间", HOLIDAY: {}})
        r = run("double_drop", conn, info(online=1), now=FRI)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes(), [COMMIT, HOLIDAY])
        self.assertIn("在线福利", r.detail)
        self.assertIn("已领取 十一手动领取buff", r.detail)

    def test_already_claimed_rejection_is_skipped_not_blocked(self):
        conn = FakeConn({HOLIDAY: {"code": 500, "error": "奖励已领取"}})
        r = run("double_drop", conn, info(), now=FRI)
        self.assertEqual(r.status, "skipped", r.detail)
        self.assertIn("今日已领过", r.detail)
        self.assertIn("留到在线福利任务重新确认", r.detail)


if __name__ == "__main__":
    unittest.main()
