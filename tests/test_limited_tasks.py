"""活动页任务（云梯夺宝、中秋博饼、决战牛魔）。全部离线。"""

import unittest
from datetime import datetime, timedelta, timezone

from zmxy import core, login, protocol as p, state
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from tests.support import FakeConn, day, run_daily as run


_UNLOCK_PATCH = None


def setUpModule():
    """这个文件的用例不关心解锁状态（解锁判断另有 tests/test_unlock.py）。"""
    global _UNLOCK_PATCH
    from unittest import mock
    _UNLOCK_PATCH = mock.patch("zmxy.quota.locked", return_value=None)
    _UNLOCK_PATCH.start()


def tearDownModule():
    _UNLOCK_PATCH.stop()

CLOUD, BOBING = "scene.activityHandler.cloudLadder", "scene.activityHandler.midBoCake"
CLOUD_REWARD = "scene.activityHandler.getCloudLadderReward"
ALL_TAKEN = {"0": 1, "1": 1, "2": 1, "3": 1}  # 云梯四档阶段奖励（6/40/80/120 格）都领过
CREATE, FINISH = "scene.copyHandler.createSinglePlayerCopy", "scene.copyHandler.finishSingleCopy"


class CloudLadderTests(unittest.TestCase):
    def test_free_attempt_is_sent_once_with_empty_body(self):
        conn = FakeConn({CLOUD: {"operate": [{"step": 1}]}})
        r = run("cloud_ladder", conn, day(cloudLadderCount=0))
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.sent, [(CLOUD, {})])

    def test_missing_counter_in_existing_object_means_unused(self):
        conn = FakeConn({CLOUD: {"operate": []}})
        self.assertEqual(run("cloud_ladder", conn, day()).status, "submitted")

    def test_at_the_end_is_skipped_and_marked_ended(self):
        conn = FakeConn()
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 120, "reward": ALL_TAKEN}})
        self.assertEqual((r.status, conn.sent, r.profile), ("skipped", [], {"cloud_end": True}))

    def test_not_at_the_end_clears_the_mark(self):
        conn = FakeConn({CLOUD: {"operate": [{"step": 1}], "cloudLadder": {"pos": 50, "reward": ALL_TAKEN}}})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 40}})
        self.assertEqual((r.status, r.profile), ("submitted", {"cloud_end": False}))

    def test_reaching_the_end_during_the_run_marks_ended(self):
        conn = FakeConn({CLOUD: {"operate": [{"step": 1}], "cloudLadder": {"pos": 121, "reward": ALL_TAKEN}}})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {}})
        self.assertEqual((r.status, r.profile), ("submitted", {"cloud_end": True}))
        self.assertIn("终点", r.detail)

    def claim_server(self, pos, taken):
        state = {"reward": {str(i): 1 for i in taken}}

        def claim(body):
            state["reward"][str(body["i"])] = 1
            return {"cloudLadder": {"pos": pos, "reward": dict(state["reward"])}, "give": [{"itemId": 101045121, "count": 1}]}
        return claim

    def test_stage_rewards_reached_after_the_climb_are_claimed(self):
        """走到 45 格：6、40 两档达到了；6 格那档领过，只领 40 格（第 1 档）。"""
        conn = FakeConn({CLOUD: {"operate": [{"step": 1}], "cloudLadder": {"pos": 45, "reward": {"0": 1}}}, CLOUD_REWARD: self.claim_server(45, [0])})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 38, "reward": {"0": 1}}})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies(CLOUD_REWARD), [{"i": 1}])
        self.assertIn("领取阶段奖励 40 格", r.detail)

    def test_unclaimed_stages_are_claimed_even_without_free_count_or_at_the_end(self):
        conn = FakeConn({CLOUD_REWARD: self.claim_server(120, [0, 1])})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=1), "cloudLadder": {"pos": 120, "reward": {"0": 1, "1": 1}}})
        self.assertEqual((r.status, conn.sent), ("submitted", [(CLOUD_REWARD, {"i": 2}), (CLOUD_REWARD, {"i": 3})]))
        conn = FakeConn({CLOUD_REWARD: self.claim_server(10, [])})  # 今天次数用完、没到终点、没有 reward 字段 = 一档都没领过
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=1), "cloudLadder": {"pos": 10}})
        self.assertEqual((r.status, conn.sent), ("submitted", [(CLOUD_REWARD, {"i": 0})]))

    def test_stage_claim_rejected_or_unconfirmed_stops_without_resend(self):
        info = {**day(cloudLadderCount=1), "cloudLadder": {"pos": 120}}
        for answer in ("背包已满", {}, {"cloudLadder": {"pos": 120}}):
            conn = FakeConn({CLOUD_REWARD: answer})
            r = run("cloud_ladder", conn, info)
            self.assertEqual((r.status, conn.sent), ("blocked", [(CLOUD_REWARD, {"i": 0})]))

    def test_no_cloud_object_means_unknown_no_mark(self):
        conn = FakeConn({CLOUD: {"operate": []}})
        self.assertIsNone(run("cloud_ladder", conn, day(cloudLadderCount=0)).profile)

    def test_landing_on_play_again_throws_once_more_for_free(self):
        answers = iter([{"operate": [{"event": 1}], "cloudLadder": {"pos": 18, "event": 1, "reward": {"0": 1}},
                         "everyDaySetting": {"cloudLadderCount": 1}},
                        {"operate": [{"event": 0}], "cloudLadder": {"pos": 22, "event": 0, "reward": {"0": 1}},
                         "everyDaySetting": {"cloudLadderCount": 1}}])
        conn = FakeConn({CLOUD: lambda body: next(answers)})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 12, "reward": {"0": 1}}})
        self.assertEqual((r.status, conn.sent), ("submitted", [(CLOUD, {}), (CLOUD, {})]), r.detail)
        self.assertIn("再来一次", r.detail)

    def test_saved_play_again_is_used_even_when_daily_count_is_used_up(self):
        conn = FakeConn({CLOUD: {"operate": [{"event": 0}], "cloudLadder": {"pos": 25, "event": 0, "reward": {"0": 1}}}})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=1), "cloudLadder": {"pos": 18, "event": 1, "reward": {"0": 1}}})
        self.assertEqual((r.status, conn.sent), ("submitted", [(CLOUD, {})]), r.detail)

    def test_saved_play_again_is_spent_first_then_the_daily_throw(self):
        # 账号1存档2 实况：停在 56 格（再来一次），扔到 59 格后服务器没扣每日次数，应该接着扔每日那一次
        answers = iter([{"operate": [{"event": 0}], "cloudLadder": {"pos": 59, "event": 0, "reward": ALL_TAKEN},
                         "everyDaySetting": {"cloudLadderCount": 0}},
                        {"operate": [{"event": 0}], "cloudLadder": {"pos": 63, "event": 0, "reward": ALL_TAKEN},
                         "everyDaySetting": {"cloudLadderCount": 1}}])
        conn = FakeConn({CLOUD: lambda body: next(answers)})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 56, "event": 1, "reward": ALL_TAKEN}})
        self.assertEqual((r.status, conn.sent), ("submitted", [(CLOUD, {}), (CLOUD, {})]), r.detail)
        self.assertIn("每日免费挑战 ×1", r.detail)
        self.assertIn("再来一次”1 次", r.detail)

    def test_each_throw_is_written_into_the_result(self):
        conn = FakeConn({CLOUD: {"operate": [{"start": 59, "point": 6, "endPos": 65, "event": 0, "give": []}],
                                 "cloudLadder": {"pos": 65, "event": 0, "reward": ALL_TAKEN}, "everyDaySetting": {"cloudLadderCount": 1}}})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 59, "reward": ALL_TAKEN}})
        self.assertIn("59→65（6 点）（每日次数，服务器记今日已用 1）", r.detail)

    def test_server_not_counting_the_daily_throw_does_not_keep_throwing(self):
        conn = FakeConn({CLOUD: {"operate": [], "cloudLadder": {"pos": 30, "event": 0, "reward": {"0": 1}},
                                 "everyDaySetting": {"cloudLadderCount": 0}}})
        r = run("cloud_ladder", conn, {**day(cloudLadderCount=0), "cloudLadder": {"pos": 27, "reward": {"0": 1}}})
        self.assertEqual(conn.sent, [(CLOUD, {})])
        self.assertIn("没记上", r.detail)

    def test_play_again_not_used_when_daily_count_unreadable(self):
        conn = FakeConn()
        r = run("cloud_ladder", conn, {**day(cloudLadderCount="x"), "cloudLadder": {"pos": 18, "event": 1, "reward": {"0": 1}}})
        self.assertEqual((r.status, conn.sent), ("blocked", []))

    def test_used_up_is_skipped_without_request(self):
        conn = FakeConn()
        r = run("cloud_ladder", conn, day(cloudLadderCount=1))
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_unreadable_counter_blocks(self):
        for info in ({}, day(cloudLadderCount="x"), day(cloudLadderCount=-1)):
            conn = FakeConn()
            self.assertEqual(run("cloud_ladder", conn, info).status, "blocked")
            self.assertEqual(conn.sent, [])

    def test_response_without_operate_is_blocked_not_retried(self):
        conn = FakeConn({CLOUD: {}})
        self.assertEqual(run("cloud_ladder", conn, day()).status, "blocked")
        self.assertEqual(len(conn.sent), 1)

    def test_transport_failure_is_uncertain_and_not_resent(self):
        conn = FakeConn(exc=TimeoutError("t"))
        with self.assertRaises(UncertainOutcome):
            run("cloud_ladder", conn, day())
        self.assertEqual(len(conn.sent), 1)

    def test_same_session_does_not_use_the_attempt_twice(self):
        conn = FakeConn({CLOUD: {"operate": []}})
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = conn, day()
        s._link = login.Link(conn, s.config, [], {"time": 1})
        s.bind(load_tasks()["cloud_ladder"])
        spec = load_tasks()["cloud_ladder"]
        self.assertEqual(spec.run(TaskContext(s, {})).status, "submitted")
        self.assertEqual(spec.run(TaskContext(s, {})).status, "skipped")
        self.assertEqual(len(conn.sent), 1)


DICE = 101022003  # MidAutumn[1].midAutumnPropId：每次 6 个


class BobingTests(unittest.TestCase):
    def go(self, conn, info, fresh=None):
        """fresh：免费的博完后重新读到的角色信息（含日常 / 限时任务刚发的骰子）；不给 = 和登录时一样。"""
        from unittest import mock
        with mock.patch.object(login, "enter", lambda link: fresh if fresh is not None else info):
            return run("mid_autumn_bobing", conn, info)

    def bag(self, n):
        return {"bag": {"item": [{"id": DICE, "count": n}]}}

    def test_sends_type_zero_and_reports_result(self):
        conn = FakeConn({BOBING: {"bcId": 704, "points": [1, 2, 3, 4, 5, 6]}})
        r = self.go(conn, {**day(boCakeCnt=0), **self.bag(5)})  # 5 个骰子不够一次（要 6 个）
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.sent, [(BOBING, {"type": 0})])
        self.assertIn("博饼", r.detail)

    def test_used_up_is_skipped(self):
        conn = FakeConn()
        self.assertEqual(self.go(conn, {**day(boCakeCnt=1), **self.bag(0)}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_dice_are_used_after_the_free_roll_and_counted_from_a_fresh_bag(self):
        conn = FakeConn({BOBING: {"bcId": 704, "points": [1, 2, 3, 4, 5, 6]}})
        r = self.go(conn, {**day(boCakeCnt=0), **self.bag(0)}, fresh={**day(boCakeCnt=1), **self.bag(13)})  # 登录时没有，任务奖励后有 13 个
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.sent, [(BOBING, {"type": 0}), (BOBING, {"type": 1}), (BOBING, {"type": 1})])  # 13 // 6 = 2 次，不发 type 2
        self.assertIn("×2", r.detail)

    def test_free_used_up_still_uses_dice_and_rejection_stops(self):
        conn = FakeConn({BOBING: {"bcId": 704, "points": [1, 2, 3, 4, 5, 6]}})
        r = self.go(conn, {**day(boCakeCnt=1), **self.bag(6)})
        self.assertEqual((r.status, conn.sent), ("submitted", [(BOBING, {"type": 1})]))
        conn = FakeConn({BOBING: "道具不足"})
        r = self.go(conn, {**day(boCakeCnt=1), **self.bag(60)})
        self.assertEqual((r.status, len(conn.sent)), ("blocked", 1))

    def test_dice_rolls_stop_at_the_daily_cap(self):
        conn = FakeConn({BOBING: {"bcId": 704, "points": [1, 2, 3, 4, 5, 6]}})
        r = self.go(conn, {**day(boCakeCnt=499), **self.bag(60)})  # MidAutumn[1].dayMaxNum = 500：只剩 1 次
        self.assertEqual((r.status, conn.sent), ("submitted", [(BOBING, {"type": 1})]))

    def test_unreadable_bag_uses_no_dice(self):
        conn = FakeConn({BOBING: {"bcId": 704, "points": [1, 2, 3, 4, 5, 6]}})
        r = self.go(conn, day(boCakeCnt=0))
        self.assertEqual((r.status, conn.sent), ("submitted", [(BOBING, {"type": 0})]))
        self.assertIn("读不到背包", r.detail)

    def test_runs_after_daily_and_limited_quests(self):
        specs = load_tasks()
        order = sorted(("mid_autumn_bobing", "limited_quest", "daily_quest", "cloud_ladder"),
                       key=lambda t: (not specs[t].first, specs[t].last, core.display_rank(t)))
        self.assertEqual(order[-2:], ["limited_quest", "mid_autumn_bobing"])

    def test_missing_everyday_blocks(self):
        conn = FakeConn()
        self.assertEqual(run("mid_autumn_bobing", conn, {}).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_response_without_points_is_blocked(self):
        conn = FakeConn({BOBING: {"bcId": 704}})
        self.assertEqual(self.go(conn, day()).status, "blocked")


NOW = int(datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=8))).timestamp())  # 牛魔、魔龙活动都开放


RED = "scene.activityHandler.getRedPacketReward"


class GoldenAutumnTests(unittest.TestCase):
    def info(self, **daily):
        # 当前任务还会检查丰稔箱；无已解锁奖励也需要完整的有效状态。
        return {**day(**daily), "redPacket": {"id": 2051, "consumeTicket": 0, "consumeCnt": 0},
                "rechargeActivity": {}}

    def test_free_chest_sent_once_as_type_zero(self):
        conn = FakeConn({RED: {}})
        r = run("golden_autumn", conn, self.info(redPacketFreeGiftDayNum=0), now=NOW)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.sent, [(RED, {"type": 0, "id": ""})])

    def test_missing_counter_in_existing_object_means_unused(self):
        conn = FakeConn({RED: {}})
        self.assertEqual(run("golden_autumn", conn, self.info(), now=NOW).status, "submitted")

    def test_used_up_skipped(self):
        conn = FakeConn()
        r = run("golden_autumn", conn, self.info(redPacketFreeGiftDayNum=1), now=NOW)
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_missing_red_packet_blocks_bonus_claims_after_free_chest(self):
        conn = FakeConn({RED: {}})
        result = run("golden_autumn", conn, day(redPacketFreeGiftDayNum=0), now=NOW)
        self.assertEqual(result.status, "blocked")
        self.assertIn("redPacket", result.detail)
        self.assertEqual(conn.sent, [(RED, {"type": 0, "id": ""})])

    def test_stop_after_unconfirmed_ticket_chest_reports_instead_of_crashing(self):
        import threading
        stop = threading.Event()
        conn = FakeConn({RED: lambda body: stop.set() or {}})  # 点券箱响应里没有 redPacket，用户同时点了停止
        info = {**self.info(redPacketFreeGiftDayNum=1), "redPacket": {"id": 2051, "consumeTicket": 10 ** 7, "consumeCnt": 0}}
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = conn, info
        s._link = login.Link(conn, s.config, [], {"time": NOW})
        s.bind(load_tasks()["golden_autumn"])
        r = load_tasks()["golden_autumn"].run(TaskContext(s, {}, stop=stop))
        self.assertNotEqual(r.status, "error", r.detail)
        self.assertIn("未确认 consumeCnt", r.detail)
        self.assertEqual([b["type"] for _, b in conn.sent], [2])

    def test_unreadable_counter_blocks(self):
        conn = FakeConn()
        self.assertEqual(run("golden_autumn", conn, {}, now=NOW).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_closed_activity_or_unknown_time(self):
        later = int(datetime(2026, 10, 20, tzinfo=timezone(timedelta(hours=8))).timestamp())
        conn = FakeConn()
        self.assertEqual(run("golden_autumn", conn, day(), now=later).status, "skipped")
        self.assertEqual(run("golden_autumn", conn, day(), now=None).status, "blocked")
        self.assertEqual(conn.sent, [])


class PairTests(unittest.TestCase):
    """决战牛魔（500104 普通 / 500110 噩梦）、挑战魔龙（500107 / 500108）与 GM切磋（500120 / 500127，见 GmSparTests）：难度行上切换，共用每日 2 次。"""

    CASES = (("bull_demon", 500104, 500110), ("magic_dragon", 500107, 500108))

    def info(self, a, b, used_a=0, used_b=0):
        return {"dungeon": {str(a): {"dailyCount": used_a}, str(b): {"dailyCount": used_b}}}

    def conn(self):
        return FakeConn({CREATE: {"rnd": 7}, FINISH: {}})

    def go(self, tid, conn, info, mode=None, now=NOW):
        return run(tid, conn, info, {"mode": mode} if mode else {}, now=now)

    def test_registered_on_limited_page_in_order(self):
        limited = [i for i, spec in load_tasks().items() if spec.group == "活动"]
        self.assertEqual(limited, ["double_drop", "online_welfare", "festival", "hallowmas", "double11_dog", "cloud_ladder", "mid_autumn_bobing", "golden_autumn", "bull_demon", "magic_dragon", "gm_spar", "challenge_gm", "xing_tian", "cyclic_trial", "game_high_jump", "chapter_role", "login_gift", "limited_quest", "dragon_treasure"])
        for tid in ("bull_demon", "magic_dragon", "gm_spar", "challenge_gm", "xing_tian"):
            spec = load_tasks()[tid]
            self.assertEqual((spec.modes, spec.default_mode, spec.choices), ((("普通", "普通"), ("噩梦", "噩梦")), "噩梦", None))

    def test_default_is_nightmare_and_uses_all_remaining_free_attempts(self):
        for tid, normal, nightmare in self.CASES:
            conn = self.conn()
            r = self.go(tid, conn, self.info(normal, nightmare))
            self.assertEqual(r.status, "submitted", r.detail)
            self.assertEqual([b["mid"] for b in conn.bodies(CREATE)], [nightmare, nightmare])

    def test_normal_mode_picks_first_level(self):
        for tid, normal, nightmare in self.CASES:
            conn = self.conn()
            self.go(tid, conn, self.info(normal, nightmare), "普通")
            self.assertEqual({b["mid"] for b in conn.bodies(CREATE)}, {normal})

    def test_count_is_shared_between_difficulties(self):
        for tid, normal, nightmare in self.CASES:
            conn = self.conn()
            r = self.go(tid, conn, self.info(normal, nightmare, used_a=1), "噩梦")
            self.assertEqual(r.status, "submitted")
            self.assertEqual(conn.routes().count(CREATE), 1)
            conn = self.conn()
            r = self.go(tid, conn, self.info(normal, nightmare, 1, 1))
            self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_unreadable_count_or_bad_mode_blocks(self):
        for tid, normal, nightmare in self.CASES:
            for info, mode in (({}, None), (self.info(normal, nightmare), "地狱")):
                conn = self.conn()
                r = self.go(tid, conn, info, mode)
                self.assertEqual((r.status, conn.sent), ("blocked", []))

    def test_closed_activity_or_unknown_time_does_not_create(self):
        later = int(datetime(2026, 10, 20, tzinfo=timezone(timedelta(hours=8))).timestamp())
        for tid, normal, nightmare in self.CASES:
            conn = self.conn()
            self.assertEqual(self.go(tid, conn, self.info(normal, nightmare), now=later).status, "skipped")
            self.assertEqual(self.go(tid, conn, self.info(normal, nightmare), now=None).status, "blocked")
            self.assertEqual(conn.sent, [])


class GmSparTests(PairTests):
    """GM切磋（500120 普通 / 500127 噩梦）：测试用的数据表快照（9/28）里还是上一期（活动 2009，8/30 结束），
    所以活动档期按新表里这一期（活动 2054：9/30 ~ 11/1）替换。"""

    CASES = (("gm_spar", 500120, 500127),)
    LATER = int(datetime(2026, 11, 2, 12, tzinfo=timezone(timedelta(hours=8))).timestamp())

    def setUp(self):
        from unittest import mock
        real = state.activity_open
        patcher = mock.patch.object(state, "activity_open", lambda aid, now: (
            self.LATER > now >= int(datetime(2026, 9, 30, tzinfo=timezone(timedelta(hours=8))).timestamp()) if aid == 2009 else real(aid, now)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_on_limited_page_in_order(self):
        pass  # 页面顺序已在 PairTests 里查

    def test_closed_activity_or_unknown_time_does_not_create(self):
        for tid, normal, nightmare in self.CASES:
            conn = self.conn()
            self.assertEqual(self.go(tid, conn, self.info(normal, nightmare), now=self.LATER).status, "skipped")
            self.assertEqual(self.go(tid, conn, self.info(normal, nightmare), now=None).status, "blocked")
            self.assertEqual(conn.sent, [])


if __name__ == "__main__":
    unittest.main()


class WindowTests(unittest.TestCase):
    """活动页“识别”：每个限时任务的档期判断（北京时间中午）。"""

    def at(self, y, m, d):
        from datetime import datetime, timedelta, timezone
        return int(datetime(y, m, d, 12, tzinfo=timezone(timedelta(hours=8))).timestamp())

    def test_windows_follow_activity_dates(self):
        from zmxy.core import load_tasks
        specs = load_tasks()
        # 本用例只验证旧快照中的这五个档期；不顺带调用其他新活动的窗口函数。
        checked = ("cloud_ladder", "mid_autumn_bobing", "bull_demon", "magic_dragon", "golden_autumn")
        open_now = {t for t in checked if specs[t].window(self.at(2026, 10, 12))}
        self.assertNotIn("cloud_ladder", open_now)       # 2027 档期至 10/11
        self.assertNotIn("mid_autumn_bobing", open_now)
        self.assertNotIn("bull_demon", open_now)
        self.assertIn("magic_dragon", open_now)           # 至 10/18
        self.assertIn("golden_autumn", open_now)          # 至 10/18
        self.assertTrue(specs["cloud_ladder"].window(self.at(2026, 10, 3)))
        self.assertFalse(specs["golden_autumn"].window(self.at(2026, 10, 19)))
