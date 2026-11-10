"""挑战GM免费边界与诸星封印按券挑战、断点及未知结果回归。"""

import copy
import threading
import unittest
from unittest import mock

from zmxy import data, dungeon, protocol as p, state
from zmxy.core import UncertainOutcome, load_tasks
from zmxy.tasks import star_seal as seal, limited, pet_catch
from tests.support import FakeConn, run, ts

CREATE, FINISH, PICKUP = dungeon.R_CREATE, dungeon.R_FINISH, dungeon.R_PICKUP


def info_with_passes(count=4):
    # 满足最高390星门槛；只计客户端getStar>0的主线与精英记录。
    records = {k: {"id": int(k), "star": 3, "dailyCount": 0}
               for k, row in data.table("Dungeon").items() if (state.num(row.get("getStar")) or 0) > 0}
    return {"p1": {"level": 168}, "dungeon": records,
            "bag": {"item": [{"id": seal.PASS_ID, "count": count}]}}


class StarSealTests(unittest.TestCase):
    def go(self, mid=10011, count=4, times=1, info=None, override=None, options=None, stop=None):
        current = copy.deepcopy(info if info is not None else info_with_passes(count))
        self.current, self.entries = current, 0

        def enter(body):
            return {"info": copy.deepcopy(current), "time": ts(2026, 11, 3)}

        def create(body):
            self.entries += 1
            cfg = seal.config(body["mid"])
            current["bag"]["item"][0]["count"] -= cfg[1]
            return {"rnd": 7, "drops": [{"type": 4, "subType": 101712002, "index": 0}]}

        responses = {p.ROUTE_ENTER: enter, CREATE: create, FINISH: {}, PICKUP: {}}
        responses.update(override or {})
        self.conn = FakeConn(responses)
        return run(seal.ID, self.conn, current, {"levels": [mid], "times": times, **(options or {})},
                   now=ts(2026, 11, 3), stop=stop)

    def test_four_ticket_stages_clear_for_materials_without_gourds(self):
        for mid, cost in ((10011, 1), (10012, 1), (10013, 2), (10014, 2)):
            with self.subTest(mid=mid):
                result = self.go(mid, count=cost, options={"no_clear": True, "filters": {str(mid): {"release": True}}})
                self.assertEqual(result.status, "submitted", result.detail)
                self.assertEqual(self.conn.routes(), [p.ROUTE_ENTER, CREATE, PICKUP, FINISH])
                self.assertEqual(self.conn.bodies(FINISH)[0]["win"], 1)
                self.assertEqual(self.current["bag"]["item"][0]["count"], 0)
                self.assertEqual(result.progress[str(mid)], 1)

    def test_each_room_refreshes_and_stops_before_balance_is_too_low(self):
        result = self.go(10013, count=3, times=3)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("已完成 1/3", result.detail)
        self.assertIn("挑战券不足", result.detail)
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertEqual(self.conn.routes().count(p.ROUTE_ENTER), 2)
        self.assertEqual(self.current["bag"]["item"][0]["count"], 1)

    def test_missing_malformed_and_zero_passes_block_before_creation(self):
        for bag in (None, {}, {"item": [{"id": seal.PASS_ID, "count": "bad"}]}, {"item": []}):
            current = info_with_passes()
            current["bag"] = bag
            with self.subTest(bag=bag):
                self.assertEqual(self.go(info=current).status, "blocked")
                self.assertNotIn(CREATE, self.conn.routes())

    def test_uses_refreshed_state_instead_of_old_balance(self):
        fresh = info_with_passes(0)
        result = self.go(count=99, override={p.ROUTE_ENTER: {"info": fresh, "time": ts(2026, 11, 3)}})
        self.assertEqual(result.status, "blocked")
        self.assertNotIn(CREATE, self.conn.routes())

    def test_unknown_time_blocks(self):
        result = self.go(override={p.ROUTE_ENTER: {"info": info_with_passes()}})
        self.assertEqual(result.status, "blocked")
        self.assertIn("服务器时间", result.detail)
        self.assertNotIn(CREATE, self.conn.routes())

    def test_refresh_failure_does_not_enter(self):
        result = self.go(override={p.ROUTE_ENTER: {"code": 500, "error": "拒绝"}})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.conn.routes(), [p.ROUTE_ENTER])

    def test_low_or_unknown_star_record_blocks(self):
        for records in ({}, None, [None], {"1": {"id": 1, "star": "bad"}}):
            current = info_with_passes()
            current["dungeon"] = records
            result = self.go(info=current)
            self.assertEqual(result.status, "blocked", result.detail)
            self.assertNotIn(CREATE, self.conn.routes())

    def test_changed_cost_currency_fails_before_refresh_or_entry(self):
        original = data.row

        def row(table, key):
            result = original(table, key)
            return {**result, "consume_type": "coin", "consume_subType": "ticket"} if table == "Dungeon" and key == 10011 else result

        with mock.patch.object(data, "row", side_effect=row):
            result = self.go()
        self.assertEqual((result.status, self.conn.sent), ("blocked", []))

    def test_unknown_settlement_does_not_start_another_room(self):
        with self.assertRaises(UncertainOutcome):
            self.go(times=2, override={FINISH: TimeoutError("lost")})
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertEqual(self.conn.routes().count(FINISH), 1)

    def test_rejected_settlement_does_not_start_another_room(self):
        result = self.go(times=2, override={FINISH: {"code": 500, "error": "拒绝"}})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.progress, {})
        self.assertEqual(self.conn.routes().count(CREATE), 1)

    def test_unknown_create_does_not_retry_or_capture(self):
        with self.assertRaises(UncertainOutcome):
            self.go(times=2, override={CREATE: TimeoutError("lost")})
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertNotIn(FINISH, self.conn.routes())

    def test_failed_pickup_keeps_room_and_never_reenters(self):
        result = self.go(times=2, override={PICKUP: {"code": 500, "error": "拒绝"}})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertNotIn(FINISH, self.conn.routes())

    def test_bag_full_counts_settled_room_but_blocks_next_room(self):
        result = self.go(times=2, override={PICKUP: {"code": 500, "error": "背包空间不足"}})
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertEqual(result.progress["10011"], 1)
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertEqual(self.conn.routes().count(FINISH), 1)

    def test_resume_counts_completed_challenges(self):
        result = self.go(times=3, options={"resume_done": {"10011": 2}})
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertEqual(result.progress["10011"], 3)

    def test_pause_preserves_progress_and_spends_nothing(self):
        stop = threading.Event()
        stop.set()
        result = self.go(times=3, options={"resume_done": {"10011": 1}}, stop=stop)
        self.assertEqual(result.status, "paused")
        self.assertEqual(result.progress["10011"], 1)
        self.assertEqual(self.conn.sent, [])

    def test_zero_times_and_already_completed_do_not_refresh(self):
        self.assertEqual(self.go(times=0).status, "skipped")
        self.assertEqual(self.conn.sent, [])
        self.assertEqual(self.go(options={"resume_done": {"10011": 1}}).status, "submitted")
        self.assertEqual(self.conn.sent, [])

    def test_mixed_capture_and_seal_selection_blocks(self):
        result = self.go(options={"levels": [10001, 10011]})
        self.assertEqual((result.status, self.conn.sent), ("blocked", []))


class ChallengeGmTests(unittest.TestCase):
    def setUp(self):
        # 测试快照GM活动2026已过期；仅替换该活动的开放判断，关卡/次数仍用快照。
        original = state.activity_open
        aid = data.row("Dungeon", 500112)["activityId"]
        patcher = mock.patch.object(state, "activity_open", side_effect=lambda key, now: (
            now is not None and ts(2026, 10, 29) <= now <= ts(2026, 11, 22) if key == aid else original(key, now)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def go(self, used_a=0, used_b=0, mode=None, fresh=None, now=ts(2026, 11, 3), override=None):
        current = {"p1": {"level": 168}, "dungeon": {"500112": {"dailyCount": used_a}, "500113": {"dailyCount": used_b}}}
        self.conn = FakeConn({p.ROUTE_ENTER: {"info": current if fresh is None else fresh, **({"time": now} if now is not None else {})},
                              CREATE: {"rnd": 7}, FINISH: {}, **(override or {})})
        return run(limited.CHALLENGE_GM_ID, self.conn, current, {"mode": mode} if mode else {}, now=now)

    def test_registered_separately_from_gm_spar(self):
        spec = load_tasks()[limited.CHALLENGE_GM_ID]
        self.assertEqual((spec.title, spec.group, spec.default_mode), ("挑战GM", "活动", "噩梦"))
        self.assertEqual(load_tasks()[limited.GM_ID].title, "GM切磋")

    def test_normal_and_nightmare_share_three_free_attempts(self):
        for mode, mid in ((None, 500113), ("普通", 500112)):
            result = self.go(used_a=1, mode=mode)
            self.assertEqual(result.status, "submitted", result.detail)
            self.assertEqual([b["mid"] for b in self.conn.bodies(CREATE)], [mid, mid])
            self.assertEqual(self.conn.routes()[0], p.ROUTE_ENTER)

    def test_free_quota_exhausted_does_not_pay(self):
        result = self.go(used_a=1, used_b=2)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.conn.routes(), [p.ROUTE_ENTER])

    def test_all_three_free_attempts_and_refresh_failure_boundary(self):
        result = self.go()
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.conn.routes().count(CREATE), 3)
        result = self.go(override={p.ROUTE_ENTER: {"code": 500, "error": "拒绝"}})
        self.assertEqual(result.status, "blocked")
        self.assertNotIn(CREATE, self.conn.routes())

    def test_refresh_overrides_old_quota_and_unknown_quota_blocks(self):
        for fresh, expected in (({"p1": {"level": 168}, "dungeon": {"500112": {"dailyCount": 3}}}, "skipped"), ({}, "blocked")):
            self.assertEqual(self.go(fresh=fresh).status, expected)
            self.assertNotIn(CREATE, self.conn.routes())

    def test_bad_mode_closed_activity_or_unknown_time_does_not_create(self):
        for args in ({"mode": "bad"}, {"now": ts(2026, 11, 25)}, {"now": None}):
            result = self.go(**args)
            self.assertIn(result.status, ("blocked", "skipped"))
            self.assertNotIn(CREATE, self.conn.routes())

    def test_unknown_settlement_never_retries(self):
        with self.assertRaises(UncertainOutcome):
            self.go(override={FINISH: TimeoutError("lost")})
        self.assertEqual(self.conn.routes().count(CREATE), 1)
        self.assertEqual(self.conn.routes().count(FINISH), 1)
