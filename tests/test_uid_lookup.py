"""UID 资料的只读请求、字段语义、无损归属和紧凑展示回归。"""

import copy
import unittest

from zmxy import data, protocol as p
from zmxy.uid import fields, profile
from zmxy.tasks import uid_lookup as lookup
from tests.support import FakeConn, run


class UidLookupTests(unittest.TestCase):
    def payload(self, response):
        return profile.build_payload(7, response, "2026-10-16T12:00:00+08:00")

    def assert_complete(self, payload):
        audit = payload["audit"]
        self.assertEqual(audit["missing"], [])
        self.assertEqual(audit["extra"], [])
        self.assertEqual(audit["duplicates"], [])
        self.assertEqual(audit["total"], audit["displayed"] + audit["technical"] + audit["pending"])

    def test_missing_uid_skips_without_request(self):
        conn = FakeConn()
        self.assertEqual(run(lookup.ID, conn, {}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_popup_uses_cg1_name_and_retains_original_response(self):
        item_id = next(iter(data.table("Prop")))
        name = data.row("Prop", item_id)["name"]
        raw = {"user": {"uid": 7, "medical": {item_id: 2}}, "bag": {"item": {item_id: 3}}}
        before = copy.deepcopy(raw)
        conn = FakeConn({lookup.R_LOOK: raw})
        result = run(lookup.ID, conn, {}, options={"uid": 7})
        self.assertEqual(result.status, "submitted")
        self.assertEqual(conn.sent, [(lookup.R_LOOK, {"uid": 7})])
        payload = result.popup["payload"]
        self.assertIn(name, result.popup["text"])
        self.assertNotIn("nameSource", result.popup["text"])
        self.assertNotIn("cg1/", result.popup["text"])
        self.assertEqual(raw, before)
        self.assertEqual(payload["rawResponse"]["user"], raw["user"])
        self.assert_complete(payload)

    def test_records_returned_target(self):
        entity_id = "123456789_r414_c2"
        raw = {"user": {"uid": 123456789, "entityId": entity_id, "name": "对手"}}
        result = run(lookup.ID, FakeConn({lookup.R_LOOK: raw}), {}, options={"uid": 123456789})
        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.popup["payload"]["returnedTarget"], {
            "rid": "123456789_r414", "index": 2, "entityId": entity_id, "source": "user.entityId",
        })
        self.assertIn("123456789_r414", result.popup["text"])
        self.assertIn("服务端实际返回 RID", result.detail)

    def test_explicit_rid_index_still_queries_exact_target(self):
        options = {"uid": 7, "rid": "7_r414", "index": 2}
        conn = FakeConn({lookup.R_LOOK: {"user": {"uid": 7}}})
        result = run(lookup.ID, conn, {}, options=options)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(conn.sent, [(lookup.R_LOOK, options)])
        self.assertEqual(result.popup["payload"]["query"], options)

    def test_partial_target_is_blocked_without_request(self):
        conn = FakeConn()
        result = run(lookup.ID, conn, {}, options={"uid": 7, "rid": "7_r1"})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_query_failure_is_reported(self):
        conn = FakeConn(exc=p.AllianceError("服务器拒绝"))
        result = run(lookup.ID, conn, {}, options={"uid": 7})
        self.assertEqual(result.status, "blocked")
        self.assertIn("查询 UID 7 失败", result.detail)

    def test_small_modules_share_major_pages(self):
        payload = self.payload({"user": {
            "name": "对手", "baoZhu": {"lv": 2, "exp": 0}, "wingSkill": 0,
            "petBadge": {"badges": {}, "curBadge": 0}, "pvp2Season": {},
            "banChatTime": 0, "showTitle": 1,
        }})
        self.assertEqual([p.title for p in payload["pages"]], ["概览", "技能养成", "伙伴", "其他资料"])
        self.assertLessEqual(len(payload["pages"]), 7)
        self.assert_complete(payload)

    def test_count_and_num_do_not_overwrite_each_other(self):
        payload = self.payload({"bag": {"item": {
            "101001001": {"id": 101001001, "count": 94, "num": 3},
        }}})
        text = profile.display_text(payload)
        self.assertIn("数量：94", text)
        self.assertIn("数量：3", text)
        self.assert_complete(payload)

    def test_wrappers_preserve_same_named_sibling_fields(self):
        raw = {"code": 200, "user": {"name": "外层角色"}, "data": {
            "user": {"name": "内层角色"}, "code": 201,
        }, "serverExtra": {"keepThis": True}}
        payload = self.payload(raw)
        text = profile.display_text(payload, technical=True)
        self.assertIn("外层角色", text)
        self.assertIn("内层角色", text)
        self.assertIn("接口状态码：200", text)
        self.assertIn("接口状态码：201", text)
        self.assertIn("keepThis", text)
        self.assert_complete(payload)

    def test_show_flags_are_not_resolved_as_item_ids(self):
        payload = self.payload({"user": {"showTitle": 1, "showWing": 0, "showHalo": 1}})
        text = profile.display_text(payload)
        self.assertIn("称号显示：开启", text)
        self.assertIn("翅膀显示：关闭", text)
        self.assertNotIn("未知物品", text)
        self.assert_complete(payload)

    def test_gem_slot_is_separate_from_item_identity(self):
        payload = self.payload({"user": {"equipments": {"weapon": {
            "id": 110708063, "type": "equipment", "name": "天枢伞",
            "inlay": {"0": 101203005, "1": 101209004},
        }}}})
        text = profile.display_text(payload)
        self.assertIn("孔位 1：5级攻击石", text)
        self.assertIn("孔位 2：4级暴击石", text)
        self.assertNotIn("未知物品（编号 0）", text)
        self.assertNotIn("数值：101203005", text)
        self.assert_complete(payload)

    def test_medical_is_taken_count_not_inventory(self):
        payload = self.payload({"user": {"medical": {"101008001": 3}},
                                "bag": {"item": {"101001001": 94}}})
        text = profile.display_text(payload)
        self.assertIn("已服用：3", text)
        self.assertIn("数量：94", text)
        self.assertEqual([p.title for p in payload["pages"]], ["技能养成", "背包仓库"])
        self.assert_complete(payload)

    def test_consumption_enum_has_three_meanings(self):
        self.assertEqual(fields.scalar("isConsume", 0), "普通物品")
        self.assertEqual(fields.scalar("isConsume", 1), "消耗品")
        self.assertEqual(fields.scalar("isConsume", 2), "卡片")

    def test_simple_item_is_compact_and_technical_fields_are_optional(self):
        payload = self.payload({"bag": {"item": [{"id": 101001001, "type": "item",
                "name": "wrong server translation", "count": 94, "isConsume": 1,
                "updateTime": 1791986467583, "entityId": "entity-item-123"}]}})
        entry = payload["pages"][0].sections[0].entries[0]
        self.assertEqual(entry.title, "还魂丹")
        summary = profile.entry_summary(entry)
        self.assertNotIn("\n", summary)
        self.assertLess(len(summary), 110)
        self.assertIn("消耗品", summary)
        self.assertIn("更新时间：2026-10-14 22:01:07", summary)
        visible = profile.display_text(payload)
        technical = profile.display_text(payload, technical=True)
        self.assertNotIn("entity-item-123", visible)
        self.assertNotIn("wrong server translation", visible)
        self.assertIn("entity-item-123", technical)
        self.assertIn("wrong server translation", technical)
        self.assertIn("1791986467583", technical)
        self.assert_complete(payload)

    def test_nested_equipment_keeps_grouping_without_duplicate_level(self):
        payload = self.payload({"user": {"equipments": {"weapon": {
            "id": 110708063, "name": "天枢伞", "level": 168,
            "recast": {"atk": {"level": 9, "add": 1415}},
        }}}})
        entry = payload["pages"][0].sections[0].entries[0]
        summary = profile.entry_summary(entry)
        self.assertEqual(summary.count("等级：168"), 1)
        details = profile.field_text(entry.fields)
        self.assertIn("洗练\n  攻击\n    等级：9\n    加成：1415", details)
        self.assert_complete(payload)

    def test_unknown_item_remains_distinguishable(self):
        payload = self.payload({"bag": {"item": {"999999999": 1, "999999998": 2}}})
        text = profile.display_text(payload)
        self.assertIn("未知物品（编号 999999999）", text)
        self.assertIn("未知物品（编号 999999998）", text)
        self.assertGreater(payload["audit"]["pending"], 0)
        self.assert_complete(payload)

    def test_nested_unknown_fields_zero_null_empty_are_all_retained(self):
        value = {"zero": 0, "none": None, "emptyList": [], "emptyMap": {}, "emptyText": ""}
        for _ in range(15):
            value = {"nested": value}
        payload = self.payload({"customField": value})
        text = profile.display_text(payload)
        self.assertIn("未提供", text)
        self.assertIn("空列表", text)
        self.assertIn("无记录", text)
        self.assertIn("未设置", text)
        self.assertIn("：0", text)
        self.assertNotIn("嵌套过深", text)
        self.assert_complete(payload)

    def test_named_custom_number_is_not_hidden_by_suffix(self):
        payload = self.payload({"user": {"unknownId": 987654}})
        self.assertIn("987654", profile.display_text(payload))
        self.assert_complete(payload)

    def test_wardrobe_scalar_references_use_cg1_names(self):
        payload = self.payload({"user": {"equipments": {"wardRobeArmor": 110608041}}})
        text = profile.display_text(payload)
        self.assertIn(fields.resolve_name(110608041, "fashion")[0], text)
        self.assertNotIn("衣柜衣服：110608041", text)
        self.assert_complete(payload)

    def test_inventory_instance_keys_do_not_replace_item_identity(self):
        payload = self.payload({"bag": {"equipment": {"instance-abc": {
            "id": 110708063, "type": "equipment", "name": "天枢伞", "level": 168,
        }}}})
        text = profile.display_text(payload)
        self.assertIn("天枢伞", text)
        self.assertNotIn("待核实字段（instance-abc）", text)
        self.assert_complete(payload)

    def test_keyboard_item_and_skill_references_are_named(self):
        payload = self.payload({"user": {"keyboard": {"skill1": 1108010, "Item1": 101001001}}})
        text = profile.display_text(payload)
        self.assertIn("还魂丹", text)
        self.assertIn(fields.resolve_name(1108010, "skill")[0], text)
        self.assertNotIn("物品1：101001001", text)
        self.assert_complete(payload)

    def test_expiry_and_timestamps_preserve_semantics(self):
        self.assertEqual(fields.scalar("expiredTime", -1), "永久")
        self.assertEqual(fields.scalar("updateTime", 1791986467583), "2026-10-14 22:01:07")
        self.assertEqual(fields.scalar("time", 30), "30")
        self.assertEqual(fields.scalar("count", 1791986467583), "1791986467583")
        self.assertEqual(fields.scalar("queriedAt", "2026-10-16T12:00:00+08:00"), "2026-10-16 12:00:00")

    def test_audit_detects_dropped_fields_instead_of_trusting_raw_copy(self):
        payload = self.payload({"user": {"name": "对手", "level": 168}})
        audit = profile.audit_fields(payload["rawResponse"], ())
        self.assertEqual(set(audit["missing"]), {("user", "name"), ("user", "level")})

    def test_pvp_log_hides_technical_tokens_but_complete_copy_keeps_them(self):
        raw = "1791986467583 0 对手 0 7_r1_c0 pvp12345"
        payload = self.payload({"user": {"pvpSeason": {"log": [raw]}}})
        text = profile.display_text(payload)
        self.assertIn("结果：失败", text)
        self.assertIn("对手：对手", text)
        self.assertNotIn("pvp12345", text)
        self.assertIn(raw, profile.display_text(payload, technical=True))
        self.assert_complete(payload)

    def test_pvp_result_two_matches_client_abnormal_battle_tip(self):
        payload = self.payload({"user": {"pvpSeason": {"log": ["1791986467583 2 对手 0 7_r1_c0 pvp12345"]}}})
        text = profile.display_text(payload)
        self.assertIn("结果：战斗异常", text)
        self.assertNotIn("结果：逃跑", text)

    def test_multiple_roles_and_inventory_owners_stay_separate(self):
        payload = self.payload({"p1": {"name": "同名", "level": 5},
                                "p2": {"name": "同名", "level": 6},
                                "bag": {"item": {"101001001": 3}},
                                "warehouse": {"item": {"101001001": 4}}})
        overview = payload["pages"][0]
        self.assertEqual([s.title for s in overview.sections], ["一号角色 · 角色信息", "二号角色 · 角色信息"])
        inventory = next(p for p in payload["pages"] if p.title == "背包仓库")
        self.assertEqual([s.title for s in inventory.sections], ["背包", "仓库"])
        self.assert_complete(payload)

    def test_album_groups_pet_photos_without_losing_scenes_or_rewards(self):
        raw = {"user": {"petAlbum": {
            "1": {"id": 171541301, "name": "苍穹蒲牢", "level": 149, "time": 1791986467583},
            "2": {"id": 171541301, "name": "苍穹蒲牢", "level": 168, "time": 1792072867583},
            "reward": {"201": 1},
        }}}
        before = copy.deepcopy(raw)
        payload = self.payload(raw)
        section = next(s for p in payload["pages"] for s in p.sections if s.title == "宠物相册")
        pets = [e for e in section.entries if e.title == "苍穹蒲牢"]
        self.assertEqual(len(pets), 1)
        self.assertEqual(len(pets[0].fields), 2)
        self.assertIn("相片：2 张", profile.entry_summary(pets[0]))
        self.assertIn("相片记录等级：149～168", profile.entry_summary(pets[0]))
        details = profile.field_text(pets[0].fields)
        self.assertIn("碧玉径：苍穹蒲牢", details)
        self.assertIn("碧玉径BOSS：苍穹蒲牢", details)
        self.assertIn("等级：149", details)
        self.assertIn("等级：168", details)
        self.assertEqual(len(section.entries), 2)  # 奖励单独保留，不计入相片数。
        self.assertEqual(raw, before)
        self.assert_complete(payload)

    def test_album_grouping_does_not_merge_current_pet_team(self):
        same_name = [key for key, row in data.table("Pet").items() if row.get("name") == "苍穹蒲牢"]
        self.assertGreaterEqual(len(same_name), 2)
        payload = self.payload({"user": {
            "petAlbum": {"1": {"id": same_name[0], "level": 149},
                         "2": {"id": same_name[1], "level": 168},
                         "3": {"id": 171741201, "level": 168}},
            "petTeam": [{"id": same_name[0], "entityId": "pet-a"},
                        {"id": same_name[0], "entityId": "pet-b"}],
        }})
        sections = {s.title:s for p in payload["pages"] for s in p.sections}
        self.assertEqual(len(sections["宠物相册"].entries), 2)
        self.assertEqual(len(sections["宠物队伍"].entries), 2)
        self.assert_complete(payload)

    def test_unknown_album_pets_with_same_server_name_stay_distinct(self):
        payload = self.payload({"user": {"petAlbum": {
            "1": {"id": 999999999, "name": "未知宠物", "level": 1},
            "2": {"id": 999999998, "name": "未知宠物", "level": 2},
            "3": {"id": 999999999, "name": "未知宠物", "level": 3},
        }}})
        section = next(s for p in payload["pages"] for s in p.sections if s.title == "宠物相册")
        self.assertEqual(len(section.entries), 2)
        self.assertNotEqual(section.entries[0].title, section.entries[1].title)
        self.assertEqual([len(e.fields) for e in section.entries], [2, 1])
        self.assert_complete(payload)


if __name__ == "__main__":
    unittest.main()
