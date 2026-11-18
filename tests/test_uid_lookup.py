"""UID 资料查询：只读请求、返回目标、资料分页和翻译（2026-11-14 重做，uid/sheet.py）。"""

import copy
import unittest

from zmxy import data, protocol as p
from zmxy.uid import sheet as sh
from zmxy.tasks import uid_lookup as lookup
from tests.support import FakeConn, run


def first(table):
    key = next(iter(data.table(table)))
    return key, data.row(table, key)["name"]


def page(s, title):
    return next(x for x in s.pages if x.title == title)


def group(s, page_title, group_title):
    return next(g for g in page(s, page_title).groups if g.title == group_title)


class UidLookupTaskTests(unittest.TestCase):
    def test_missing_uid_skips_without_request(self):
        conn = FakeConn()
        self.assertEqual(run(lookup.ID, conn, {}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_popup_carries_sheet_and_text_without_changing_response(self):
        item_id, item_name = first("Prop")
        raw = {"user": {"uid": 7, "name": "对手", "bag": {"item": {"a": {"id": int(item_id), "count": 3}}}}}
        before = copy.deepcopy(raw)
        conn = FakeConn({lookup.R_LOOK: raw})
        result = run(lookup.ID, conn, {}, options={"uid": 7})
        self.assertEqual(result.status, "submitted")
        self.assertEqual(conn.sent, [(lookup.R_LOOK, {"uid": 7})])
        self.assertIsInstance(result.popup["sheet"], sh.Sheet)
        self.assertIn(f"{item_name}  ×3", result.popup["text"])
        self.assertEqual(raw, before)

    def test_records_returned_target(self):
        raw = {"user": {"uid": 123456789, "entityId": "123456789_r414_c1", "name": "对手"}}
        result = run(lookup.ID, FakeConn({lookup.R_LOOK: raw}), {}, options={"uid": 123456789})
        self.assertIn("对方现在的角色是 角色编号 414 · 2P", result.detail)
        self.assertIn("对方现在的角色是 角色编号 414 · 2P", result.popup["sheet"].query)

    def test_role_number_and_side_build_rid_and_index(self):
        for side, index, shown in (("1p", 0, "1P"), ("2p", 1, "2P")):
            with self.subTest(side=side):
                conn = FakeConn({lookup.R_LOOK: {"user": {"uid": 7, "entityId": f"7_r2_c{index}"}}})
                result = run(lookup.ID, conn, {}, options={"uid": "7", "role": "2", "side": side})
                self.assertEqual(result.status, "submitted", result.detail)
                self.assertEqual(conn.sent, [(lookup.R_LOOK, {"uid": 7, "rid": "7_r2", "index": index})])
                self.assertIn(f"角色编号 2 · {shown}", result.detail)
                self.assertNotIn("没有这个角色", result.detail)

    def test_empty_role_ignores_side_and_queries_current_character(self):
        conn = FakeConn({lookup.R_LOOK: {"user": {"uid": 7}}})
        run(lookup.ID, conn, {}, options={"uid": 7, "role": "", "side": "2p"})
        self.assertEqual(conn.sent, [(lookup.R_LOOK, {"uid": 7})])

    def test_role_zero_is_a_real_number(self):
        conn = FakeConn({lookup.R_LOOK: {"user": {"uid": 7}}})
        run(lookup.ID, conn, {}, options={"uid": 7, "role": "0"})
        self.assertEqual(conn.sent, [(lookup.R_LOOK, {"uid": 7, "rid": "7_r0", "index": 0})])

    def test_missing_role_is_reported_when_server_returns_another(self):
        raw = {"user": {"uid": 7, "entityId": "7_r0_c0"}}
        result = run(lookup.ID, FakeConn({lookup.R_LOOK: raw}), {}, options={"uid": 7, "role": "1", "side": "1p"})
        self.assertIn("没有这个角色，服务器返回的是 角色编号 0 · 1P", result.detail)
        self.assertIn("没有这个角色", result.popup["sheet"].query)

    def test_bad_role_is_blocked_without_request(self):
        conn = FakeConn()
        result = run(lookup.ID, conn, {}, options={"uid": 7, "role": "-1"})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_query_failure_is_reported(self):
        result = run(lookup.ID, FakeConn(exc=p.AllianceError("服务器拒绝")), {}, options={"uid": 7})
        self.assertEqual(result.status, "blocked")
        self.assertIn("查询 UID 7 失败", result.detail)


class SheetTests(unittest.TestCase):
    def build(self, user, **extra):
        return sh.build(7, {"user": user, **extra}, "2026-11-14T12:00:00+08:00")

    def test_returned_target_parsing(self):
        self.assertEqual(sh.resolve_returned_target({"user": {"entityId": "9_r3_c1"}}),
                         {"rid": "9_r3", "index": 1, "entityId": "9_r3_c1"})
        self.assertIsNone(sh.resolve_returned_target({"user": {"entityId": "9_r3"}}))
        self.assertIsNone(sh.resolve_returned_target({}))

    def test_head_and_query(self):
        s = sh.build(7, {"user": {"name": "对手", "level": 168, "fightPower": 2870697.6}}, "2026-11-14T12:00:00+08:00", query="角色编号 3 · 1P")
        self.assertEqual(s.name, "对手")
        self.assertIn("168 级", s.head[0])
        self.assertIn("战力 2,870,698", s.head[0])
        self.assertEqual(s.query, "UID 7　角色编号 3 · 1P　查询时间 2026-11-14 12:00:00")

    def test_numbers_and_times(self):
        self.assertEqual(sh.fmt(252392.39), "252,392")
        self.assertEqual(sh.fmt(1.25), "1.25")
        self.assertEqual(sh.fmt(3.0), "3")
        self.assertEqual(sh.when(-1), "永久")
        self.assertEqual(sh.when(0), "")
        self.assertEqual(sh.when(1791986467583), "2026-10-14 22:01")
        self.assertEqual(sh.when(1791986467), "2026-10-14 22:01")

    def test_empty_pages_are_dropped(self):
        titles = [x.title for x in self.build({"name": "x", "level": 1}).pages]
        self.assertEqual(titles, ["概览"])

    def test_medical_is_taken_count(self):
        mid, mname = first("Prop")  # 测试表里没有 Medical：名字退到 Prop 查
        g = group(self.build({"medical": {mid: 12}}), "养成", "丹药")
        self.assertEqual(g.note, "已服用的数量")
        self.assertTrue(g.compact)
        self.assertEqual((g.items[0].name, g.items[0].tag), (mname, "×12"))

    def test_skills_and_keyboard_are_named(self):
        sid, sname = first("Skill")
        s = self.build({"skill": {sid: 8}, "keyboard": {"skill1": int(sid), "skill2": 0}})
        skill = group(s, "技能", "技能").items[0]
        self.assertEqual((skill.name, skill.tag), (sname, "Lv8"))
        keys = group(s, "技能", "键位").items
        self.assertEqual([(k.name, k.tag) for k in keys], [(sname, "技能键 1")])

    def test_worn_equipment_has_slot_quality_and_details(self):
        eid, ename = first("Prop")  # 测试表里没有 Equipment：名字退到 Prop 查
        rec = {"id": int(eid), "level": 168, "quality": 5, "atk": 1000,
               "recast": {"atk": {"level": 9, "add": 1415}}, "expiredTime": -1}
        item = group(self.build({"equipments": {"weapon": rec, "showWing": 1}}), "穿戴", "身上").items[0]
        self.assertEqual(item.name, ename)
        self.assertTrue(item.tag.endswith("Lv168"))
        self.assertEqual(item.color, sh.QUALITY_COLOR[5])
        self.assertTrue(any("1,415（9 级）" in line for line in item.lines))
        self.assertFalse(any("到期" in line for line in item.lines))  # 永久不写

    def test_display_switches_are_not_shown(self):
        s = self.build({"showTitle": 1, "showFashion": 0, "showWing": 1})
        self.assertNotIn("showTitle", s.text())
        self.assertNotIn("其他", [x.title for x in s.pages])

    @staticmethod
    def props(consume, n=2):
        """测试表里 isConsume 是这个值、品质各不相同的 n 个道具：[(编号, 名字, 品质)]，品质从低到高。"""
        seen = {}
        for key, r in data.table("Prop").items():
            if r.get("isConsume") == consume and r.get("quality") not in seen:
                seen[r["quality"]] = (int(key), r["name"], r["quality"])
        return [seen[q] for q in sorted(seen)][:n]

    def test_bag_splits_by_consume_sorts_by_quality_and_colors(self):
        (lo, lo_name, lo_q), (hi, hi_name, hi_q) = self.props(0)
        use = self.props(1, 1)[0]
        bag = {"itemBagMax": 200, "item": {"a": {"id": lo, "count": 324}, "b": {"id": hi, "count": 1},
                                           "c": {"id": use[0], "count": 7}}}
        s = self.build({"bag": bag})
        titles = [g.title for g in page(s, "背包").groups]
        self.assertEqual(titles, ["容量", "道具", "消耗品"])
        items = group(s, "背包", "道具").items
        self.assertEqual([i.name for i in items], [hi_name, lo_name])  # 品质高的在前
        self.assertEqual(items[0].color, sh.QUALITY_COLOR[hi_q])
        self.assertEqual(items[1].text(), f"{lo_name}  ×324")
        self.assertEqual(group(s, "背包", "消耗品").items[0].name, use[1])
        self.assertIn("200", group(s, "背包", "容量").items[0].lines[0])

    def test_same_quality_sorts_by_id(self):
        same = sorted(int(k) for k, r in data.table("Prop").items() if r.get("isConsume") == 0 and r.get("quality") == 3)[:2]
        rows = [{"id": same[1], "count": 1}, {"id": same[0], "count": 1}]
        self.assertEqual([i.name for i in sh.prop_items(rows)], [sh.name(same[0], "Prop"), sh.name(same[1], "Prop")])

    def test_warehouse_is_own_page_right_after_bag(self):
        item, use = self.props(0, 1)[0], self.props(1, 1)[0]
        bag = {"itemBagMax": 10, "itemWarehouseMax": 250, "item": {"a": {"id": item[0], "count": 2}},
               "warehouseItem": {"b": {"id": item[0], "count": 5}, "c": {"id": use[0], "count": 3}}}
        s = self.build({"bag": bag})
        titles = [x.title for x in s.pages]
        self.assertEqual(titles.index("仓库"), titles.index("背包") + 1)
        self.assertNotIn("250", group(s, "背包", "容量").items[0].lines[0])
        self.assertEqual([g.title for g in page(s, "仓库").groups], ["容量", "道具", "消耗品"])
        self.assertEqual(group(s, "仓库", "道具").items[0].text(), f"{item[1]}  ×5")
        self.assertEqual(group(s, "仓库", "消耗品").items[0].text(), f"{use[1]}  ×3")
        self.assertIn("道具 250", group(s, "仓库", "容量").items[0].lines[0])
        self.assertNotIn("仓库", [x.title for x in self.build({"bag": {"itemWarehouseMax": 250}}).pages])

    def test_album_groups_photos_per_pet(self):
        pet_id, pet_name = first("Pet")
        album = {"p1": {"id": int(pet_id), "level": 150, "time": 1791986467583},
                 "p2": {"id": int(pet_id), "level": 160, "time": 1791986467583}}
        g = group(self.build({"petAlbum": album}), "相册", "灵宠相片")
        self.assertEqual(len(g.items), 1)
        self.assertEqual((g.items[0].name, g.items[0].tag), (pet_name, "2 张"))
        self.assertEqual(g.note, "共 2 张")

    def test_hidden_tabs_are_translated(self):
        info = group(self.build({"hideList": [0, 7]}), "概览", "基本资料").items[0]
        self.assertIn("个人展示里隐藏了：称号、相册", info.lines)

    def test_unknown_fields_are_kept_in_other_page(self):
        s = self.build({"newThing": {"a": 1}, "flag": 5}, extra=3)
        names = [i.name for i in group(s, "其他", "没认出来的字段").items]
        self.assertEqual(names, ["newThing", "flag", "extra"])

    def test_unknown_id_is_marked(self):
        self.assertEqual(sh.name(999999999, "Prop"), "未知（999999999）")
        self.assertEqual(sh.name("999999999.0", "Prop", fallback="服务器名"), "服务器名")

    def test_text_lists_every_page(self):
        sid, _ = first("Skill")
        text = self.build({"name": "对手", "skill": {sid: 1}}).text()
        self.assertIn("════ 概览 ════", text)
        self.assertIn("════ 技能 ════", text)
        self.assertIn("【技能】", text)


if __name__ == "__main__":
    unittest.main()
