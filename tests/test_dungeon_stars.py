"""副本卡片与悬浮评分一致性回归。"""

import unittest
from unittest import mock

from zmxy import data, dungeon
from tests.support import Fake, elite_info, run_dungeon


class DungeonStarTests(unittest.TestCase):
    def test_repeated_wins_keep_card_and_tooltip_stars_in_sync(self):
        # 模拟已有三星记录被后续结算覆盖，再按客户端 hasStar 解码悬浮提示。
        for task, info in (("dungeon_main", {}),
                           ("dungeon_elite", elite_info(used=0)),
                           ("dungeon_nightmare", {"dungeonTreasure": {"1400200": {"remainCnt": 2}}})):
            with self.subTest(task=task), mock.patch("zmxy.quota.locked", return_value=None):
                record = {"star": 3, "starInfo": 7}

                def handler(route, body):
                    if route == dungeon.R_FINISH:
                        record.update(star=body["star"], starInfo=body["starInfo"])

                conn = Fake(handler)
                result = run_dungeon(task, conn, info, {"times": 2})
                self.assertEqual(result.status, "submitted", result.detail)
                self.assertEqual(len(conn.bodies("finishSingleCopy")), 2)
                self.assertIs(type(record["starInfo"]), int)
                self.assertEqual(record["star"], 3)
                self.assertEqual([bool(record["starInfo"] & bit) for bit in (1, 2, 4)], [True] * 3)

    def test_combo_grade_uses_combo_bit_instead_of_health_bit(self):
        original_row = data.row

        def row(table, key):
            if table == "DungeonGrade" and key == 1062:  # 主线最后的传送门 Boss 房
                return {"star_complete": 1, "star_time": 180,
                        "star_hpPercent": -1, "star_hitCombo": 30}
            return original_row(table, key)

        with mock.patch("zmxy.quota.locked", return_value=None), mock.patch.object(data, "row", side_effect=row):
            conn = Fake()
            result = run_dungeon("dungeon_main", conn, {})
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(conn.bodies("finishSingleCopy")[0]["starInfo"], 11)

    def test_no_clear_keeps_failure_and_zero_rating(self):
        with mock.patch("zmxy.quota.locked", return_value=None):
            conn = Fake()
            result = run_dungeon("dungeon_main", conn, {}, {"no_clear": True})
        self.assertEqual(result.status, "submitted", result.detail)
        body = conn.bodies("finishSingleCopy")[0]
        self.assertEqual((body["win"], body["star"], body["starInfo"]), (0, 0, 0))

    def test_bag_full_settlement_also_has_tooltip_rating(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": [{"type": 3, "index": 0}]}
            if route == dungeon.R_PICKUP:
                return {"code": 500, "error": "背包空间不足"}

        with mock.patch("zmxy.quota.locked", return_value=None):
            conn = Fake(handler)
            run_dungeon("dungeon_main", conn, {})
        self.assertEqual(len(conn.bodies("finishSingleCopy")), 1)
        self.assertEqual(conn.bodies("finishSingleCopy")[0]["starInfo"], 7)

    def test_direct_failure_callers_submit_integer_zero(self):
        body = dungeon._finish_body(1, 2, win=False)
        self.assertIs(type(body["starInfo"]), int)
        self.assertEqual(body["starInfo"], 0)
