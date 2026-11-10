"""万圣水晶球奇数领取策略、真实拾取序号与异常停止的离线测试。"""

import threading
import unittest
from unittest import mock

from zmxy import data, dungeon, protocol as p
from zmxy.core import Account, Session, UncertainOutcome, load_tasks, task_order
from zmxy.tasks.events import hallowmas as h
from tests.support import Fake, run, ts


class HallowmasTests(unittest.TestCase):
    def execute(self, initial=0, *, drops=None, update=True, info=None, closed=0,
                error=None, stop=None, no_witch=0, options=None):
        info = info if info is not None else {
            "everyDaySetting": {"dungeonActivityCnt": initial},
            "activityOther": {"2071": closed}, "p1": {"level": 100},
            "dungeon": {"6": {"passNum": 1}},
        }
        self.history, self.indices = [], []
        seed, frame, claims = {}, 0, 0

        def handler(route, body):
            nonlocal frame, claims
            if route == h.R_CLOSE:
                info["activityOther"]["2071"] = 0
                return {}
            if route == dungeon.R_CREATE:
                frame += 1
                claims = 0
                seed.update(client=body["rnd"], server=7)
                normal = {"type": "coin", "subType": "exp", "count": 3, "monsterId": 1}
                crystals = drops if drops is not None else [
                    {"type": "item", "subType": str(h.CRYSTAL), "count": 1,
                     "monsterId": "901653", "index": 0, "dropIndex": i}
                    for i in (5, 8)]
                return {"rnd": 7, "drops": [normal] + (crystals if frame > no_witch else [])}
            if route == dungeon.R_PICKUP:
                if error:
                    raise error
                noise = body["key"] ^ seed["client"]
                ident = (body["index"] ^ seed["server"] ^ seed["client"]) - noise
                self.indices.append(ident)
                if ident in (5, 8):
                    claims += 1
                    if update:
                        info["everyDaySetting"]["dungeonActivityCnt"] += 1
                return {"give": [{"type": "item", "subType": str(h.CRYSTAL), "count": 1}]}
            if route == dungeon.R_FINISH:
                self.history.append((claims, info["everyDaySetting"].get("dungeonActivityCnt")))
                if stop:
                    stop.set()
                return {}

        self.conn = Fake(handler)
        session = Session(p.ClientConfig(uid=1, username="u"))
        original_row = data.row

        def row(table, key):
            if table == "ActivityCommon":
                return {"hallowmasId": 2071}
            if table == "Common":
                return {"dungeonActivityMax": 10}
            if table == "DungeonActivity":
                return {"monsterID": 901653, "rolelevel": 20, "dropId": "5016,5017"}
            return original_row(table, key)

        with mock.patch.object(h, "catalog", return_value=[(7, "紫翠丹房")]), \
             mock.patch.object(h, "opened", return_value=True), \
             mock.patch.object(data, "row", side_effect=row), \
             mock.patch.object(dungeon, "level", return_value=dungeon.Level(7, dungeon.MAIN, "紫翠丹房", (7,))), \
             mock.patch.object(session, "refresh", side_effect=lambda: session.info):
            return run(h.ID, self.conn, info, options or {}, now=ts(2026, 10, 28),
                       session=session, stop=stop)

    def test_zero_to_eleven_uses_one_then_five_pairs_and_original_drop_indices(self):
        result = self.execute()
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.history, [(1, 1), (2, 3), (2, 5), (2, 7), (2, 9), (2, 11)])
        self.assertEqual(self.indices, [5] + [5, 8] * 5)
        self.assertEqual(len(self.conn.bodies("createSinglePlayerCopy")), 6)

    def test_resuming_every_count_preserves_odd_path(self):
        for count in range(1, 10):
            with self.subTest(count=count):
                result = self.execute(count)
                self.assertEqual(result.status, "submitted", result.detail)
                self.assertEqual(self.history[-1], (2, 11))
                self.assertTrue(all(n % 2 == 1 for _, n in self.history))

    def test_legacy_options_cannot_enable_other_drops(self):
        result = self.execute(9, options={"crystals_only": True})
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.indices, [5, 8])

    def test_legacy_toggle_off_is_ignored_and_only_crystals_are_collected(self):
        result = self.execute(options={"crystals_only": False})
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.indices, [5] + [5, 8] * 5)
        self.assertEqual(self.history, [(1, 1), (2, 3), (2, 5), (2, 7), (2, 9), (2, 11)])

    def test_other_drops_are_always_ignored(self):
        exp = {"type": "coin", "subType": "exp", "monsterId": 1, "count": 3}
        other = {"type": "item", "subType": "101002001", "monsterId": 1, "count": 1}
        picker = h.CrystalPicker(0, 901653)
        picker.prepare([(0, exp), (1, other)])
        self.assertFalse(picker(exp))
        self.assertFalse(picker(other))

    def test_stored_manual_level_and_main_options_are_ignored(self):
        result = self.execute(9, options={"levels": [80], "crystals_only": False,
                                          "surface_only": True, "no_clear": True})
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual([b["mid"] for b in self.conn.bodies("createSinglePlayerCopy")], [7])
        self.assertEqual(self.indices, [5, 8])
        self.assertEqual(self.conn.bodies("finishSingleCopy")[0]["win"], 1)

    def test_refresh_reselects_newest_unlocked_stage_before_next_copy(self):
        first = (dungeon.Level(7, dungeon.MAIN, "紫翠丹房", (7,)), 901653)
        second = (dungeon.Level(8, dungeon.MAIN, "珍宝阁", (8,)), 901653)
        with mock.patch.object(h, "latest_level", side_effect=[first, first, second]), \
             mock.patch.object(h.quota, "locked", return_value=None):
            result = self.execute(7)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual([b["mid"] for b in self.conn.bodies("createSinglePlayerCopy")], [7, 8])

    def test_ten_blocks_and_eleven_skips_without_writes(self):
        for count, status in [(10, "blocked"), (11, "skipped")]:
            result = self.execute(count)
            self.assertEqual(result.status, status)
            self.assertFalse(self.conn.sent)

    def test_missing_or_malformed_daily_state_blocks(self):
        for daily in (None, {"dungeonActivityCnt": -1}, {"dungeonActivityCnt": True},
                      {"dungeonActivityCnt": "bad"}):
            info = {"everyDaySetting": daily, "activityOther": {}, "p1": {"level": 100}}
            result = self.execute(info=info)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(self.conn.sent)

    def test_unconfirmed_pickup_stops_before_next_copy(self):
        with self.assertRaisesRegex(UncertainOutcome, "不重发"):
            self.execute(update=False)
        self.assertEqual(len(self.conn.bodies("createSinglePlayerCopy")), 1)

    def test_wrong_monster_count_or_single_drop_blocks_before_pickup(self):
        good = {"type": "item", "subType": str(h.CRYSTAL), "count": 1, "monsterId": 901653}
        for drops in ([good], [good, good | {"count": 2}], [good, good | {"monsterId": 1}]):
            result = self.execute(drops=drops)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(self.conn.bodies("syncDropInfo"))
            self.assertFalse(self.conn.bodies("finishSingleCopy"))

    def test_unknown_write_outcome_propagates_without_retry(self):
        with self.assertRaises(UncertainOutcome):
            self.execute(error=TimeoutError("test timeout"))
        self.assertEqual(len(self.conn.bodies("syncDropInfo")), 1)
        self.assertFalse(self.conn.bodies("finishSingleCopy"))

    def test_enable_activity_once_then_confirm(self):
        result = self.execute(9, closed=1)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.conn.bodies("setActivityClose"), [{"id": 2071, "state": 0}])

    def test_no_witch_runs_again_without_claiming_other_drops(self):
        result = self.execute(9, no_witch=2)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(self.history, [(0, 9), (0, 9), (2, 11)])

    def test_no_witch_is_bounded(self):
        with mock.patch.object(h, "MAX_RUNS", 3):
            result = self.execute(9, no_witch=100)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(self.history), 3)

    def test_stop_after_current_copy(self):
        stop = threading.Event()
        result = self.execute(stop=stop)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertEqual(self.history, [(1, 1)])

    def test_stop_before_any_copy_is_skipped(self):
        stop = threading.Event()
        stop.set()
        result = self.execute(stop=stop)
        self.assertEqual(result.status, "skipped", result.detail)
        self.assertEqual(self.history, [])

    def test_ui_registration_has_no_choice_dialog(self):
        spec = load_tasks()[h.ID]
        self.assertEqual(spec.group, "活动")
        self.assertIsNone(spec.choices)
        self.assertTrue(spec.first)

    def test_runs_before_other_main_tasks_and_after_double_drop(self):
        account = Account("test", p.ClientConfig(), tasks=("dungeon_main", h.ID, "double_drop"))
        self.assertEqual(task_order(account), ["double_drop", h.ID, "dungeon_main"])

    def test_too_low_level_or_missing_activity_state_never_creates(self):
        for info in (
            {"everyDaySetting": {}, "activityOther": {}, "p1": {"level": 19}},
            {"everyDaySetting": {}, "p1": {"level": 100}},
            {"everyDaySetting": {}, "activityOther": {"2071": "bad"}, "p1": {"level": 100}},
        ):
            result = self.execute(info=info)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(self.conn.sent)


class AutoLevelTests(unittest.TestCase):
    def select(self, info, config=None):
        original_row = data.row
        config = config if config is not None else {
            "monsterID": 901653, "rolelevel": 20, "dropId": "5016,5017"}

        def row(table, key):
            return config if table == "DungeonActivity" else original_row(table, key)

        with mock.patch.object(h, "catalog", return_value=[(7, "紫翠丹房"), (9, "玉英宫"), (8, "珍宝阁")]), \
             mock.patch.object(data, "row", side_effect=row):
            return h.latest_level(info)

    def test_latest_unlocked_stage_uses_fresh_pass_records(self):
        lv, monster = self.select({"p1": {"level": 100},
                                   "dungeon": {"6": {"passNum": 1}, "7": {"passNum": 1}}})
        self.assertEqual((lv.mid, monster), (8, 901653))

    def test_newly_unlocked_stage_can_be_selected_without_having_cleared_it(self):
        lv, _ = self.select({"p1": {"level": 100}, "dungeon": {"8": {"passNum": 1}}})
        self.assertEqual(lv.mid, 9)

    def test_no_eligible_unlocked_main_stops(self):
        with self.assertRaisesRegex(dungeon.Stop, "没有已解锁"):
            self.select({"p1": {"level": 100}, "dungeon": {}})

    def test_missing_records_or_bad_latest_record_never_falls_back(self):
        for records in (None, {"8": {"passNum": "bad"}, "6": {"passNum": 1}}):
            with self.assertRaisesRegex(dungeon.Stop, "无法自动确认"):
                self.select({"p1": {"level": 100}, "dungeon": records})

    def test_low_or_unknown_role_level_stops(self):
        for level in (19, None, "bad"):
            with self.assertRaises(dungeon.Stop):
                self.select({"p1": {"level": level}, "dungeon": {"8": {"passNum": 1}}})

    def test_main_qixing_without_activity_is_excluded_despite_elite_record(self):
        config = {"isDungeonActivity": 1, "probability": 1}
        with mock.patch.object(dungeon, "catalog", return_value=[(79, "辞衡堂"), (80, "七星殿")]), \
             mock.patch.object(data, "table", return_value={"79": config, "30800": config}):
            self.assertEqual(h.catalog(), [(79, "辞衡堂")])


if __name__ == "__main__":
    unittest.main()
