"""“选择”对话框里的“预览”：只读查对方，文字给用户核对。"""

import unittest

from zmxy import protocol as p
from zmxy.tasks import arena, target_preview as tp, treasure_cave


def fake(routes):
    """routes：路由 → 响应 dict 或 str（服务器拒绝）。记下发过的路由。"""
    sent = []

    def request(route, body=None):
        sent.append(route)
        r = routes.get(route)
        if isinstance(r, str):
            raise p.AllianceError(r)
        return r or {}
    request.sent = sent
    return request


USER = {"user": {"entityId": "1000000001_r3_c0", "name": "测试玩家", "charId": 2097, "level": 168, "fightPower": 2870697}}


class TargetPreviewTests(unittest.TestCase):
    def test_spec_only_reads(self):
        self.assertFalse(tp.SPEC.writes)
        self.assertNotIn(arena.ARENA.begin, tp.SPEC.reads)

    def test_arena_registered(self):
        req = fake({tp.api.R_OFFLINE_USER_INFO: USER, arena.ARENA.details: {"userData": {
            "index": 0, "p1": {"name": "小白龙", "fightPower": 3469671}, "totalScore": 5551228}}})
        text = tp.preview(req, "arena", 1000000001, 3)[0]
        self.assertRegex(text, r"^测试玩家 · .+ · 168 级 · 战力 2,870,697\n")
        self.assertIn("竞技场：已报名 · 出战 小白龙 战力 3,469,671 · 战功 5,551,228", text)

    def test_pet_arena_power_is_total_minus_feats(self):
        req = fake({tp.api.R_OFFLINE_USER_INFO: USER, arena.PET_ARENA.details: {"data": {"totalScore": 7797996, "battleScore": 2954656}}})
        self.assertIn("斗宠大会：已报名 · 宠物战力 4,843,340 · 战功 7,797,996", tp.preview(req, "pet_arena", 1000000001, 3)[0])

    def test_not_registered_and_wrong_role(self):
        req = fake({tp.api.R_OFFLINE_USER_INFO: "该玩家数据暂时无法查询", arena.ARENA.details: "获取竞技场信息错误"})
        text = tp.preview(req, "arena", 1000000001, 99)[0]
        self.assertIn("UID 或角色编号可能填错了", text)
        self.assertIn("可能没报名；执行时会改打榜首", text)

    def test_server_returns_other_role(self):
        other = {"user": dict(USER["user"], entityId="1000000001_r1_c0")}
        text = tp.preview(fake({tp.api.R_OFFLINE_USER_INFO: other}), "treasure_cave", 1000000001, 3)[0]
        self.assertIn("没有角色编号 3：服务器返回的是 角色编号 1", text)

    def test_empty_role_uses_returned_role_and_continues(self):
        req = fake({tp.api.R_OFFLINE_USER_INFO: USER, arena.ARENA.details: {"userData": {"p1": {"name": "小白龙", "fightPower": 1}}}})
        text, role = tp.preview(req, "arena", 1000000001, None)
        self.assertEqual(role, 3)
        self.assertIn("现在用的是角色编号 3，已自动填入", text)
        self.assertIn("竞技场：已报名", text)

    def test_empty_role_without_returned_rid_stops_after_profile(self):
        req = fake({tp.api.R_OFFLINE_USER_INFO: {"user": {"name": "x"}}})
        text, role = tp.preview(req, "arena", 5, None)
        self.assertIsNone(role)
        self.assertIn("请自己填角色编号", text)
        self.assertEqual(req.sent, [tp.api.R_OFFLINE_USER_INFO])

    def test_cave_lists_stars_and_marks_the_one_fought(self):
        req = fake({tp.api.R_OFFLINE_USER_INFO: USER, treasure_cave.R_PUBLISHED: {"data": [
            {"entityId": "x_r3_1", "userdata": {"defensiveLevel": 6}}, {"entityId": "x_r3_2", "userdata": {"defensiveLevel": 8}}]}})
        text = tp.preview(req, "treasure_cave", 1000000001, 3)[0]
        self.assertIn("发布了 2 个洞府：6 星（挑战码 x_r3_1），8 星（挑战码 x_r3_2）← 会打这个", text)
        self.assertIn("没有发布洞府", tp.preview(fake({tp.api.R_OFFLINE_USER_INFO: USER, treasure_cave.R_PUBLISHED: {"data": []}}),
                                                  "treasure_cave", 1000000001, 3)[0])


if __name__ == "__main__":
    unittest.main()
