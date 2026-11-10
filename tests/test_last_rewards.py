import unittest

from zmxy import protocol as p
from zmxy.core import load_tasks
from zmxy.tasks import last_rewards as lr


class Session:
    def drain_pushes(self):
        return []


class Ctx:
    stopped = False
    now = None
    session = Session()

    def __init__(self, replies, info=None):
        self.replies, self.sent = replies, []
        self.info = info if info is not None else {"arenaSetting": {"rewardTime": 0}, "petArenaSetting": {"rewardTime": 0}}

    def request(self, route, body):
        self.sent.append((route, body))
        reply = self.replies[route]
        if isinstance(reply, Exception):
            raise reply
        return reply


class LastRewardTests(unittest.TestCase):
    def test_previously_claimed_rewards_are_not_requested_again(self):
        ctx = Ctx({}, {"arenaSetting": {"rewardTime": 10}, "petArenaSetting": {"rewardTime": 20}})
        result = lr.run(ctx)
        self.assertEqual((result.status, ctx.sent), ("skipped", []))
        self.assertIn("之前已领取", result.detail)

    def test_unknown_or_malformed_claim_state_never_sends_claim(self):
        for setting in (None, {"rewardTime": None}, {"rewardTime": -1}, {"rewardTime": "broken"}):
            with self.subTest(setting=setting):
                ctx = Ctx({lr.R_INFO_ARENA: {}}, {"arenaSetting": setting, "petArenaSetting": {"rewardTime": 10}})
                result = lr.run(ctx)
                self.assertEqual(result.status, "blocked")
                self.assertFalse(any(route in (lr.R_ARENA, lr.R_PET) for route, _body in ctx.sent))

    def test_stop_during_claim_state_read_prevents_claim(self):
        ctx = Ctx({lr.R_INFO_ARENA: {"arenaSetting": {"rewardTime": 0}}}, {})
        request = ctx.request

        def stop_after_read(route, body):
            response = request(route, body)
            ctx.stopped = True
            return response

        ctx.request = stop_after_read
        self.assertEqual(lr.run(ctx).status, "skipped")
        self.assertEqual(ctx.sent, [(lr.R_INFO_ARENA, {})])

    def test_registered_weekly_before_nightmare_tree(self):
        from zmxy import core
        spec = load_tasks()[lr.ID]
        self.assertEqual((spec.title, spec.group), ("上届奖励", "周常"))
        self.assertEqual(core.DISPLAY_ORDER.index(lr.ID) + 1, core.DISPLAY_ORDER.index("tree_king_nightmare"))

    def test_claims_both(self):
        ctx = Ctx({lr.R_ARENA: {"give": [{"type": "prevRank", "count": "3"}, {"type": "item", "subType": "101026007", "count": 5}]},
                   lr.R_PET: {"give": [{"type": "prevRank", "count": 8}]}})
        r = lr.run(ctx)
        self.assertEqual([x[0] for x in ctx.sent], [lr.R_ARENA, lr.R_PET])
        self.assertEqual(r.status, "submitted")
        self.assertIn("竞技场", r.detail)
        self.assertIn("上周排名第 3 名", r.detail)
        self.assertIn("上周排名第 8 名", r.detail)
        out = r.profile[lr.OUT_KEY]
        self.assertEqual((out["竞技场"], out["斗宠"]), (lr.GOT, lr.GOT))
        self.assertEqual((out["竞技场_rank"], out["斗宠_rank"]), (3, 8))
        self.assertEqual(lr.describe({}, out), ("竞技场 已领取 · 斗宠 已领取", False))

    def test_nothing(self):
        r = lr.run(Ctx({lr.R_ARENA: {"give": []}, lr.R_PET: {"give": []}}))
        self.assertEqual(r.status, "skipped")
        self.assertIn("上届没有奖励", r.detail)

    def test_previous_rank_ignores_invalid_values(self):
        for response in ({}, {"give": []}, {"give": [{"type": "prevRank", "count": "x"}]},
                         {"give": [{"type": "prevRank", "count": 0}]}):
            self.assertIsNone(lr.previous_rank(response))

    def test_already_claimed_is_not_failure(self):
        ctx = Ctx({lr.R_ARENA: p.AllianceError("scene.arenaHandler.getReward 失败：奖励已领取"), lr.R_PET: p.AllianceError("奖励已领取")})
        r = lr.run(ctx)
        self.assertEqual(r.status, "skipped")
        self.assertIn("之前已领取", r.detail)
        out = r.profile[lr.OUT_KEY]
        self.assertEqual((out["竞技场"], out["斗宠"]), (lr.CLAIMED, lr.CLAIMED))
        self.assertEqual(lr.describe({}, out), ("竞技场 已领取 · 斗宠 已领取", False))

    def test_one_rejected_still_tries_other(self):
        ctx = Ctx({lr.R_ARENA: p.AllianceError("不在领取时间"), lr.R_PET: {"give": []}})
        r = lr.run(ctx)
        self.assertEqual(len(ctx.sent), 2)
        self.assertEqual(r.status, "blocked")
        self.assertEqual({k: v for k, v in r.profile[lr.OUT_KEY].items() if k != "week"}, {"斗宠": lr.NONE})


if __name__ == "__main__":
    unittest.main()


class StatusTests(unittest.TestCase):
    class C:
        now = None

        def __init__(self, info, replies=None):
            self.info, self.replies = info, replies or {}

        def request(self, route, body):
            return self.replies[route]

    def test_status_from_info(self):
        info = {"everyWeekSetting": {"arenaSetting": {"week": {}, "rewardTime": 1700000000}, "petArenaSetting": {"week": {}, "rewardTime": 0}}}
        text, pending, _, _ = lr.auto_status(self.C(info))
        self.assertEqual(text, "竞技场 已领取 · 斗宠 未领取")
        self.assertTrue(pending)

    def test_all_claimed(self):
        info = {"arenaSetting": {"rewardTime": 5}, "petArenaSetting": {"rewardTime": 7}}
        self.assertEqual(lr.auto_status(self.C(info))[1], False)

    def test_falls_back_to_arena_info(self):
        c = self.C({}, {lr.R_INFO_ARENA: {"arenaSetting": {"rewardTime": 0}}, lr.R_INFO_PET: {"petArenaSetting": {"rewardTime": 3}}})
        self.assertEqual(lr.auto_status(c)[:2], ("竞技场 未领取 · 斗宠 已领取", True))

    def test_unreadable(self):
        c = self.C({}, {lr.R_INFO_ARENA: {}, lr.R_INFO_PET: {}})
        self.assertEqual(lr.auto_status(c)[:2], ("竞技场 未读取 · 斗宠 未读取", None))

    def test_corrupt_reward_time_is_not_reported_as_unclaimed(self):
        info = {"arenaSetting": {"rewardTime": "broken"}, "petArenaSetting": {"rewardTime": -1}}
        self.assertEqual(lr.auto_status(self.C(info))[:2], ("竞技场 未读取 · 斗宠 未读取", None))

    def test_outcome_overrides_status(self):
        states = {"竞技场": False, "斗宠": False}
        self.assertEqual(lr.describe(states, {"竞技场": lr.GOT, "斗宠": lr.NONE}), ("竞技场 已领取 · 斗宠 无奖励", False))
        self.assertEqual(lr.describe(states, {"竞技场": lr.NONE}), ("竞技场 无奖励 · 斗宠 未领取", True))
        self.assertEqual(lr.describe({}, {"斗宠": lr.NONE}), ("竞技场 未读取 · 斗宠 无奖励", None))  # 一边不知道不能被另一边掩盖
        self.assertEqual(lr.describe({}, {}), ("竞技场 未读取 · 斗宠 未读取", None))

class WeekKeyTests(unittest.TestCase):
    def test_week_follows_beijing_time_of_the_server(self):
        # 2026-11-08 周日 23:30（北京时间）还是第 45 周；半小时后（周一 00:00）就是第 46 周，不看电脑时区
        sunday_late = 1794151800
        self.assertEqual(lr.week_key(sunday_late), "2026-45")
        self.assertEqual(lr.week_key(sunday_late + 1800), "2026-46")
