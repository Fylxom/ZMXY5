import sys
import unittest
from unittest import mock

from zmxy import data, dungeon, pickup, protocol as p, quota, state
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from tests.support import LEVEL, Fake, elite_info, run_dungeon as run


_UNLOCK_PATCH = None


def setUpModule():
    """这个文件的用例不关心解锁状态（解锁判断另有 tests/test_unlock.py）。"""
    global _UNLOCK_PATCH
    from unittest import mock
    _UNLOCK_PATCH = mock.patch("zmxy.quota.locked", return_value=None)
    _UNLOCK_PATCH.start()


def tearDownModule():
    _UNLOCK_PATCH.stop()


NIGHTMARE_INFO = {"dungeonTreasure": {"1400200": {"remainCnt": 2}}}
ERR = {"code": 500, "error": "背包空间不足"}


class LevelTests(unittest.TestCase):
    def test_kinds_and_rooms(self):
        self.assertEqual(dungeon.level(51).rooms, (51, 1062))   # 主线 + 传送门 Boss 房
        self.assertEqual(dungeon.level(48).rooms, (48, 1057, 1058))  # 东极妙严宫经过地宫后进入 Boss 房
        self.assertEqual(dungeon.level(5).rooms, (5,))          # 不把隐藏关路线并入主线
        self.assertEqual(dungeon.level(30600).kind, "elite")
        self.assertEqual(dungeon.level(30510).rooms, (30510,))  # 精英直接是 Boss 战，不进 30511
        self.assertEqual(dungeon.level(30120).rooms, (30120,))
        self.assertEqual(dungeon.level(40170).kind, "nightmare")
        for bad in (1062, 999, 300110, 100101, 0):               # Boss 房 / 测试关 / 兽林 / 九宫 / 不存在
            self.assertIsNone(dungeon.level(bad), bad)
        # 表里 difficult=0 的别的玩法：上古蛮荒、北极柜山、关卡测试、封号陷阱、北冥、善见城、宝舟渡口、善法堂
        for other in (490, 900, 985, 997, 998, 1000, 1001, 2001, 2003, 2101, 2201):
            self.assertIsNone(dungeon.level(other), other)

    def test_main_line_is_exactly_stage_1_to_80(self):
        self.assertEqual([m for m, _ in dungeon.catalog("main")], list(range(1, 81)))
        self.assertEqual(dungeon.catalog("main")[-1], (80, "七星殿"))

    def test_first_card_free_for_all_elite_and_nightmare(self):
        self.assertTrue(dungeon.first_card_is_free(30600))
        self.assertTrue(dungeon.first_card_is_free(40170))


class HiddenTests(unittest.TestCase):
    def test_catalog_and_routes(self):
        items = dict(dungeon.catalog("hidden"))
        self.assertEqual(len(items), 23)                       # 旧目录 24 个，去掉要 2000 灵魂的分界廊饰品 BOSS
        self.assertEqual(items[1002], "天墉城墙")
        self.assertEqual(items[1019], "天雷门")
        self.assertEqual(len(set(items.values())), 23)                    # 去掉后缀后名字仍互不重复
        self.assertEqual(dungeon.level(1002).rooms, (5, 1002, 1003))      # 主线房 → 隐藏房 → 出口房
        self.assertEqual(dungeon.level(1009).rooms, (14, 1009, 1011, 1008))
        self.assertEqual(dungeon.level(1019).rooms, (20, 1019))
        for kind in ("main", "elite", "nightmare"):                       # 不污染其他三类
            self.assertFalse(set(items) & {v for v, _ in dungeon.catalog(kind)})
        for bad in (1015, 1052, 1003, 1011):                              # 2000 灵魂 / 返回房 / 路线中间房
            self.assertIsNone(dungeon.level(bad), bad)

    def test_every_hidden_route_ends_at_a_room_that_finishes_its_parent(self):
        table = data.table("Dungeon")
        for mid, _ in dungeon.catalog("hidden"):
            route = dungeon.level(mid).rooms
            self.assertEqual(table[str(route[-1])]["finishBId"], route[0], mid)

    def test_flow_creates_every_room_picks_up_each_and_settles_once_with_last_room(self):
        created = []

        def handler(route, body):
            if route == dungeon.R_CREATE:
                created.append(body["mid"])
                return {"code": 200, "rnd": 100 + len(created), "drops": [{"monsterId": 1, "index": 0}]}

        conn = Fake(handler)
        res = run("dungeon_hidden", conn, {}, {"levels": [1009]})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(created, [14, 1009, 1011, 1008])
        self.assertEqual(conn.routes().count("syncDropInfo"), 4)
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)
        self.assertEqual(conn.routes()[-1], "finishSingleCopy")
        finish = conn.sent[-1][1]
        last_client = [b["rnd"] for r, b in conn.sent if r == dungeon.R_CREATE][-1]
        self.assertEqual(finish["rnd"], dungeon.as3(104) ^ dungeon.as3(last_client))
        self.assertNotIn("turnCard", conn.routes())

    def test_later_room_failure_never_settles_the_earlier_room(self):
        def handler(route, body):
            if route == dungeon.R_CREATE and body["mid"] == 1003:
                return {"code": 500, "error": "x"}

        conn = Fake(handler)
        res = run("dungeon_hidden", conn, {}, {"levels": [1002]})
        self.assertEqual(res.status, "blocked")
        self.assertNotIn("finishSingleCopy", conn.routes())

    def test_kind_mismatch_blocks(self):
        for task_id in ("dungeon_main", "dungeon_elite"):
            conn = Fake()
            self.assertEqual((run(task_id, conn, {}, {"levels": [1002]}).status, conn.sent), ("blocked", []))
        self.assertEqual(run("dungeon_hidden", Fake(), {}, {"levels": [51]}).status, "blocked")

    def test_hidden_task_sits_between_main_and_elite(self):
        ids = [t for t in load_tasks() if t.startswith("dungeon_")]
        self.assertEqual(ids, ["dungeon_main", "dungeon_hidden", "dungeon_elite", "dungeon_nightmare"])
        self.assertEqual(load_tasks()["dungeon_hidden"].choices.times_max, 99)


class CatalogTests(unittest.TestCase):
    def test_every_task_offers_levels_of_its_kind_and_defaults_are_listed(self):
        for kind, size in (("main", 80), ("elite", 80), ("nightmare", 27)):
            spec = load_tasks()[f"dungeon_{kind}"]
            items = spec.choices.items()
            values = [v for v, _ in items]
            self.assertEqual(len(items), size)
            self.assertEqual(values, sorted(values))
            self.assertEqual(len({n for _, n in items}), size)            # 显示名互不重复
            self.assertTrue(all(dungeon.level(v).kind == kind for v in values))


class PinyinTests(unittest.TestCase):
    def test_matching_by_name_id_and_initials_without_icu(self):
        from zmxy import pinyin
        self.assertTrue(pinyin.matches("", "长洲", 60))
        self.assertTrue(pinyin.matches("长", "长洲", 60))
        self.assertTrue(pinyin.matches(" 60 ", "长洲", 60))
        self.assertFalse(pinyin.matches("七星", "长洲", 60))

    @unittest.skipUnless(sys.platform == "win32", "拼音依赖 Windows ICU")
    def test_pinyin_and_polyphonic_readings(self):
        from zmxy import pinyin
        if pinyin._icu() is None:
            self.skipTest("本机没有 icu.dll")
        for query, name in (("biyujing", "碧玉径"), ("byj", "碧玉径"), ("QiXing", "七星殿"), ("qxd", "七星殿"),
                            ("changzhou", "长洲"), ("zhangzhou", "长洲"), ("cz", "长洲"),
                            ("chongridian", "重日殿"), ("zhongridian", "重日殿"), ("crd", "重日殿"), ("zrd", "重日殿")):
            self.assertTrue(pinyin.matches(query, name), (query, name))
        self.assertFalse(pinyin.matches("biyujin9", "碧玉径"))
        self.assertFalse(pinyin.matches("qxd", "碧玉径"))


class QuotaTests(unittest.TestCase):
    def test_main_unlimited(self):
        q = quota.of({}, 51)
        self.assertIsNone(q.key)
        self.assertEqual(q.left, float("inf"))

    def test_elite_daily(self):
        self.assertEqual(quota.of(elite_info(1), 30600).left, 1)
        self.assertEqual(quota.of(elite_info(2), 30600).left, 0)
        self.assertEqual(quota.of({"dungeon": {}}, 30600).left, 2)   # 容器在、无记录 = 没打过

    def test_elite_unknown_is_not_zero(self):
        self.assertIsNone(quota.of({}, 30600).left)                                   # 没有副本记录
        self.assertIsNone(quota.of({"dungeon": {"30600": {"id": 30600}}}, 30600).left)  # 缺 dailyCount
        self.assertIsNone(quota.of({"dungeon": {"30600": {"dailyCount": -1}}}, 30600).left)
        self.assertIsNone(quota.of({"dungeon": {"30600": {"dailyCount": 1.5}}}, 30600).left)

    def test_state_num_rejects_fractional_and_overflow_values(self):
        self.assertIsNone(state.num(1.5))
        self.assertIsNone(state.num(float("inf")))
        self.assertEqual(state.num(1.0), 1)

    def test_vip_schedule_rejects_fractional_and_nonfinite_scalar_values(self):
        self.assertEqual(state.by_vip(3.0, 0), 3)
        for invalid in (3.5, float("nan"), float("inf")):
            self.assertIsNone(state.by_vip(invalid, 0))

    def test_invalid_special_count_configuration_blocks_quota_guess(self):
        cases = (
            {"id": 999001, "countdownMode": "bad", "dailyCountMax": -1, "consume_count": -1},
            {"id": 999001, "countdownMode": None, "treasureShareId": "bad", "dailyCountMax": -1, "consume_count": -1},
        )
        for row in cases:
            with self.subTest(row=row), mock.patch("zmxy.quota.data.row", side_effect=lambda name, _mid: row if name == "Dungeon" else None), \
                    mock.patch("zmxy.quota.data.table", return_value={}):
                self.assertIsNone(quota.of({}, 999001).left)

    def test_nightmare_pool_shared_by_levels(self):
        a, b = quota.of(NIGHTMARE_INFO, 40170), quota.of(NIGHTMARE_INFO, 40200)
        self.assertEqual((a.key, a.left), (b.key, b.left))
        self.assertIsNone(quota.of({}, 40170).left)
        self.assertEqual(quota.of({"dungeonTreasure": {"1400200": {"remainCnt": "x"}}}, 40170).left, None)

    def test_wing_and_tower_are_not_unlimited(self):
        info = {"dungeon": {}, "vip": {"level": 0}, "dungeonTreasure": {}}
        special = [int(k) for k, r in data.table("Dungeon").items()
                   if r.get("type") == 12 or (r.get("countdownMode") or 0) > 0]
        self.assertIn(700601, special)   # 噩梦盘云根（天树王）
        self.assertIn(1000101, special)  # 玉石琵琶-生存（镇妖塔）
        for mid in special:
            self.assertIsNone(quota.of(info, mid).left, mid)


class MainTests(unittest.TestCase):
    def test_main_cannot_send_card_or_treasure_routes(self):
        from zmxy.core import GuardViolation
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn = Fake()
        session.bind(load_tasks()["dungeon_main"])
        for route in (dungeon.R_TURN_CARD, dungeon.R_TREASURE_CLAIM):
            with self.assertRaises(GuardViolation):
                session.request(route, {})

    def test_two_rooms_pickup_xor_and_single_finish(self):
        created = []

        def handler(route, body):
            if route == dungeon.R_CREATE:
                created.append(body)
                return {"code": 200, "rnd": 1000, "drops": [{"monsterId": 1, "index": 0, "type": 1}, {"monsterId": 1, "index": 0, "type": "buff"}]}

        conn = Fake(handler)
        res = run("dungeon_main", conn, {})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual([c["mid"] for c in created], [51, 1062])
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "syncDropInfo", "createSinglePlayerCopy", "syncDropInfo", "finishSingleCopy"])
        client = created[1]["rnd"]
        pick = [b for r, b in conn.sent if r == dungeon.R_PICKUP][1]
        seed = dungeon.as3(1000) ^ dungeon.as3(client)
        noise = pick["key"] ^ dungeon.as3(client)
        self.assertEqual(pick["index"], dungeon.as3(0 + noise) ^ seed)    # 位置 0 的掉落（buff 不拾取）
        finish = conn.sent[-1][1]
        self.assertEqual((finish["win"], finish["star"], finish["rnd"]), (1, 3, seed))

    def test_multi_hop_mainline_picks_every_room_and_finishes_on_boss(self):
        created = []

        def handler(route, body):
            if route == dungeon.R_CREATE:
                created.append(body["mid"])
                return {"code": 200, "rnd": 1000 + len(created), "drops": [{"monsterId": 1, "index": 0}]}

        conn = Fake(handler)
        res = run("dungeon_main", conn, {}, {"levels": [48]})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(created, [48, 1057, 1058])
        self.assertEqual(conn.routes().count("syncDropInfo"), 3)
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)
        self.assertEqual(conn.routes()[-1], "finishSingleCopy")
        finish = conn.sent[-1][1]
        last_client = [b["rnd"] for r, b in conn.sent if r == dungeon.R_CREATE][-1]
        self.assertEqual(finish["rnd"], dungeon.as3(1003) ^ dungeon.as3(last_client))

    def test_main_times(self):
        conn = Fake()
        res = run("dungeon_main", conn, {}, {"levels": [51], "times": 3})
        self.assertEqual(res.status, "submitted")
        self.assertEqual(conn.routes().count("finishSingleCopy"), 3)
        self.assertNotIn("turnCard", conn.routes())

    def test_wrong_kind_or_invalid_level_blocks_before_any_request(self):
        for levels in ([30600], [1062], ["abc"], [51, 40170]):
            conn = Fake()
            res = run("dungeon_main", conn, {}, {"levels": levels})
            self.assertEqual((res.status, conn.sent), ("blocked", []), levels)

    def test_times_limits_and_blank(self):
        for task_id, cap in (("dungeon_main", 999), ("dungeon_elite", 99), ("dungeon_nightmare", 99)):
            self.assertEqual(run(task_id, Fake(), {}, {"times": cap + 1}).status, "blocked", task_id)
            self.assertEqual(run(task_id, Fake(), {}, {"times": -1}).status, "blocked", task_id)
        conn = Fake()
        for blank in (0, "", None):                       # 次数留空 = 不执行
            self.assertEqual((run("dungeon_main", conn, {}, {"times": blank}).status, conn.sent), ("skipped", []))
        self.assertEqual(run("dungeon_main", conn, {}, {"levels": []}).status, "skipped")  # 没选关卡 = 跳过
        self.assertEqual(run("dungeon_elite", conn, elite_info(), {"levels": []}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_times_defaults_to_one_when_unset_and_caps_shown_to_gui(self):
        conn = Fake()
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, {}
        spec = load_tasks()["dungeon_main"]
        session.bind(spec)
        spec.run(TaskContext(session, {"levels": [51]}))     # 没写 times：默认 1 次
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)
        self.assertEqual((spec.choices.times_max, spec.choices.times_default), (999, 1))
        self.assertEqual(load_tasks()["dungeon_elite"].choices.times_max, 99)

    def test_no_default_levels(self):
        for task_id in ("dungeon_main", "dungeon_elite", "dungeon_nightmare"):
            conn = Fake()
            session = Session(p.ClientConfig(uid=1, username="u"))
            session._conn, session.info = conn, elite_info()
            spec = load_tasks()[task_id]
            session.bind(spec)
            self.assertEqual((spec.run(TaskContext(session, {})).status, conn.sent), ("skipped", []))


class EliteTests(unittest.TestCase):
    def test_uses_remaining_free_count_then_card(self):
        conn = Fake()
        res = run("dungeon_elite", conn, elite_info(used=1))
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "finishSingleCopy", "getCard", "turnCard"])

    def test_no_count_left_sends_nothing(self):
        conn = Fake()
        res = run("dungeon_elite", conn, elite_info(used=2))
        self.assertEqual((res.status, conn.sent), ("skipped", []))

    def test_unreadable_count_blocks_instead_of_assuming(self):
        conn = Fake()
        res = run("dungeon_elite", conn, {})
        self.assertEqual((res.status, conn.sent), ("blocked", []))

    def test_times_cannot_exceed_free_count(self):
        conn = Fake()
        run("dungeon_elite", conn, elite_info(used=1), {"times": 5})
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)

    def test_rejected_create_is_not_charged_and_blocks(self):
        conn = Fake(lambda r, b: {"code": 500, "error": "前置关卡未通关"} if r == dungeon.R_CREATE else None)
        res = run("dungeon_elite", conn, elite_info())
        self.assertEqual(res.status, "blocked")
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy"])

    def test_card_not_free_blocks_before_create(self):
        from unittest import mock
        conn = Fake()
        with mock.patch.object(dungeon, "first_card_is_free", return_value=False):
            res = run("dungeon_elite", conn, elite_info())
        self.assertEqual((res.status, conn.sent), ("blocked", []))

    def test_card_failure_after_settle_still_counts_the_attempt(self):
        conn = Fake(lambda r, b: {"code": 500, "error": "x"} if r == dungeon.R_TURN_CARD else None)
        res = run("dungeon_elite", conn, elite_info())
        self.assertEqual(res.status, "blocked")
        self.assertIn("已结算", res.detail)
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)

    def test_refused_battle_item_is_reported_as_item_failure(self):
        from dataclasses import replace
        from zmxy import api
        conn = Fake(lambda r, b: "道具不足" if r == api.R_USE_ITEM else None)
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, elite_info()
        spec = load_tasks()["dungeon_elite"]
        session.bind(replace(spec, writes=spec.writes | {api.R_USE_ITEM}))
        with self.assertRaises(dungeon.Stop) as caught:
            dungeon.play(TaskContext(session, {}), dungeon.level(30600), dungeon.Gains(), battle_items=[101009001])
        self.assertIn("副本内使用道具 101009001 失败", str(caught.exception))
        self.assertNotIn("拾取未完成", str(caught.exception))
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "useItem"])  # 不拾取、不结算

    def test_counts_are_not_reused_within_one_connection(self):
        conn = Fake()
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, elite_info(used=1)
        spec = load_tasks()["dungeon_elite"]
        session.bind(spec)
        opts = {"levels": [30600], "times": 1}
        first = spec.run(TaskContext(session, opts))
        second = spec.run(TaskContext(session, opts))
        self.assertEqual((first.status, second.status), ("submitted", "skipped"))
        self.assertEqual(conn.routes().count("createSinglePlayerCopy"), 1)


class NightmareTests(unittest.TestCase):
    def handler(self, route, body):
        if route == dungeon.R_TREASURE_DROP:
            return {"code": 200, "drop": [{"index": 0}, None, {"index": 2, "dropIndex": 5}]}

    def test_claims_before_finish_then_card_and_stops_at_pool(self):
        conn = Fake(self.handler)
        res = run("dungeon_nightmare", conn, NIGHTMARE_INFO, {"times": 10})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.routes().count("finishSingleCopy"), 2)    # 池里只有 2 次
        one = conn.routes()[:conn.routes().index("turnCard") + 1]
        self.assertEqual(one, ["createSinglePlayerCopy", "getDungeonTreasureDrop", "getDungeonTreasureReward",
                               "getDungeonTreasureReward", "finishSingleCopy", "getCard", "turnCard"])
        claims = [b for r, b in conn.sent if r == dungeon.R_TREASURE_CLAIM][:2]
        self.assertEqual(claims, [{"i": 0}, {"i": 5}])

    def key_run(self, items, stock, options=None):
        turns = iter(x if isinstance(x, dict) else {"type": "item", "subType": str(x), "count": 1} for x in items)  # 翻到的牌总是物品对象

        def handler(route, body):
            if route == dungeon.R_TREASURE_DROP:
                return {"code": 200, "drop": [{"index": 0}]}
            if route == dungeon.R_GET_CARD:
                return {"code": 200, "items": items}
            if route == dungeon.R_TURN_CARD:
                return {"code": 200, "give": [next(turns, {"type": "item", "subType": "5", "count": 1})]}

        conn = Fake(handler)
        info = NIGHTMARE_INFO | {"bag": {"item": [{"id": 101006501, "count": stock}]}}
        run("dungeon_nightmare", conn, info, {"times": 1, "levels": [40050]} | (options or {}))
        return conn.routes().count("turnCard")

    def test_keys_used_when_box_odds_at_least_two_thirds(self):
        box = {"type": "item", "subType": "101098002", "count": 1}
        other = {"type": "item", "subType": "5", "count": 1}
        on = {"key_gem_box": True}
        self.assertEqual(self.key_run([other, box, box, box], 9, on), 2)     # 剩 3/3 → 翻一张，之后即使 2/2 也不再翻（最多一次）
        self.assertEqual(self.key_run([other, box, box, other], 9, on), 2)   # 剩 2/3 → 翻
        self.assertEqual(self.key_run([box, box, box, other], 9, on), 2)     # 剩 2/3 → 翻
        self.assertEqual(self.key_run([other, box, other, other], 9, on), 1)  # 剩 1/3 不翻
        self.assertEqual(self.key_run([other, box, box, box], 9), 1)         # 没开选项
        self.assertEqual(self.key_run([other, box, box, box], 0, on), 1)     # 没钥匙

    def test_cards_given_as_bare_item_ids(self):
        on = {"key_gem_box": True}  # 服务器只给四个物品 id（没有数量）：照样能认出宝石盒
        self.assertEqual(self.key_run([5, 101098002, 101098002, 101098002], 9, on), 2)
        self.assertEqual(self.key_run(["5", "101098002", "5", "5"], 9, on), 1)
        self.assertEqual(self.key_run([], 9, on), 1)                          # 一张都没给：不猜

    def test_card_decisions_are_logged(self):
        box = {"type": "item", "subType": "101098002", "count": 1}
        other = {"type": "item", "subType": "5", "count": 1}

        def logs(items, stock):
            turns, lines = iter(items), []

            def handler(route, body):
                if route == dungeon.R_TREASURE_DROP:
                    return {"code": 200, "drop": [{"index": 0}]}
                if route == dungeon.R_GET_CARD:
                    return {"code": 200, "items": items}
                if route == dungeon.R_TURN_CARD:
                    return {"code": 200, "give": [next(turns)]}
            session = Session(p.ClientConfig(uid=1, username="u"))
            session._conn, session.info = Fake(handler), NIGHTMARE_INFO | {"bag": {"item": [{"id": 101006501, "count": stock}]}}
            spec = load_tasks()["dungeon_nightmare"]
            session.bind(spec)
            spec.run(TaskContext(session, {"times": 1, "levels": [40050], "key_gem_box": True}, log=lines.append))
            return [x for x in lines if "翻牌" in x]

        used = logs([other, box, box, box], 9)
        self.assertEqual(len(used), 3, used)                                  # 免费翻到什么 + 剩余、概率并用钥匙、钥匙翻到什么
        self.assertIn("再翻一张", used[1])
        self.assertIn("钥匙翻到", used[2])
        self.assertIn("概率不够", logs([other, box, other, other], 9)[-1])
        self.assertIn("钥匙不够", logs([other, box, box, box], 0)[-1])

    def test_no_drop_means_no_card(self):
        conn = Fake(lambda r, b: {"code": 200} if r == dungeon.R_TREASURE_DROP else None)
        run("dungeon_nightmare", conn, NIGHTMARE_INFO, {"times": 1})
        self.assertNotIn("turnCard", conn.routes())
        self.assertIn("finishSingleCopy", conn.routes())

    def test_empty_pool_or_unreadable_pool(self):
        for info, status in (({"dungeonTreasure": {"1400200": {"remainCnt": 0}}}, "skipped"), ({}, "blocked")):
            conn = Fake()
            self.assertEqual((run("dungeon_nightmare", conn, info).status, conn.sent), (status, []))

    def test_claim_failure_does_not_settle(self):
        def handler(route, body):
            if route == dungeon.R_TREASURE_DROP:
                return {"code": 200, "drop": [{"index": 0}]}
            if route == dungeon.R_TREASURE_CLAIM:
                return {"code": 500, "error": "boom"}
        conn = Fake(handler)
        res = run("dungeon_nightmare", conn, NIGHTMARE_INFO)
        self.assertEqual(res.status, "blocked")
        self.assertNotIn("finishSingleCopy", conn.routes())


class BatchConn(Fake):
    """支持流水线的假连接：request_many 一次收一批。"""

    def __init__(self, handler=None, many_exc=None):
        super().__init__(handler)
        self.batches, self.many_exc = [], many_exc

    def request_many(self, route, bodies):
        self.batches.append((route, len(bodies)))
        if self.many_exc:
            raise self.many_exc
        return [self.request(route, b) for b in bodies]


class SpeedAndTransportTests(unittest.TestCase):
    def drops(self, n):
        return lambda r, b: ({"code": 200, "rnd": 5, "drops": [{"monsterId": 1, "index": i} for i in range(n)]}
                             if r == dungeon.R_CREATE else None)

    def test_pickups_are_pipelined_in_batches_of_24(self):
        conn = BatchConn(self.drops(60))
        res = run("dungeon_elite", conn, elite_info())
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual([n for _, n in conn.batches], [24, 24, 12])
        self.assertEqual(conn.routes().count("syncDropInfo"), 60)

    def test_protocol_level_timeout_is_uncertain_not_a_rejection(self):
        # 回归：协议层的"等待响应超时"是 AllianceError，曾被当成普通失败并退还次数
        class Conn(Fake):
            def request(self, route, body):
                raise p.AllianceError(f"等待 {route} 响应超时")
        with self.assertRaises(UncertainOutcome):
            run("dungeon_elite", Conn(), elite_info())

    def test_batch_failure_is_uncertain_and_not_settled(self):
        conn = BatchConn(self.drops(30), many_exc=p.AllianceError("等待响应超时；连接已关闭"))
        with self.assertRaises(UncertainOutcome):
            run("dungeon_main", conn, {})
        self.assertNotIn("finishSingleCopy", conn.routes())

    def test_batch_item_rejected_by_server_is_reported_after_the_batch(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"code": 200, "rnd": 5, "drops": [{"monsterId": 1, "index": i} for i in range(3)]}
            if route == dungeon.R_PICKUP and body is not None:
                handler.n = getattr(handler, "n", 0) + 1
                return ERR if handler.n == 2 else None
        conn = BatchConn(handler)
        res = run("dungeon_main", conn, {})
        self.assertEqual(res.status, "blocked")                 # 任务层只报阻塞，转暂停由 run_account 统一兜底
        self.assertIn("背包空间不足", res.detail)
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)


class ProgressTests(unittest.TestCase):
    def test_progress_is_logged_and_stop_works_inside_a_task(self):
        import threading
        lines, stop = [], threading.Event()
        session = Session(p.ClientConfig(uid=1, username="u"))
        conn = Fake()
        session._conn, session.info = conn, {}
        spec = load_tasks()["dungeon_main"]
        session.bind(spec)

        def log(message):
            lines.append(message)
            if message.endswith(" 2/5"):
                stop.set()

        res = spec.run(TaskContext(session, {"levels": [51], "times": 5}, log=log, stop=stop))
        self.assertEqual(conn.routes().count("finishSingleCopy"), 2)      # 做完第 2 次就停
        self.assertEqual(res.status, "blocked")
        self.assertIn("已被用户停止", res.detail)
        self.assertEqual(lines, [f"玄洲 {n}/5" for n in (0, 1, 2)])   # 只报进度数字，不报创建/拾取/结算步骤


class SafetyTests(unittest.TestCase):
    def test_timeout_in_pickup_is_uncertain_never_retried_never_settled(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"code": 200, "rnd": 1, "drops": [{"monsterId": 1, "index": 0}]}
            if route == dungeon.R_PICKUP:
                raise TimeoutError("t")
        conn = Fake(handler)
        with self.assertRaises(UncertainOutcome):
            run("dungeon_main", conn, {})
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "syncDropInfo"])

    def test_bag_full_settles_this_attempt_then_stops(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"code": 200, "rnd": 1, "drops": [{"monsterId": 1, "index": 0}]}
            if route == dungeon.R_PICKUP:
                return ERR
        conn = Fake(handler)
        res = run("dungeon_main", conn, {}, {"times": 3})
        self.assertEqual(res.status, "blocked")
        self.assertIn("背包空间不足", res.detail)
        self.assertIn("玄洲", res.detail)
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "syncDropInfo", "finishSingleCopy"])

    def test_bag_full_records_progress_and_resume_subtracts_it(self):
        def full(route, body):
            if route == dungeon.R_CREATE:
                return {"code": 200, "rnd": 1, "drops": [{"monsterId": 1, "index": 0}]}
            if route == dungeon.R_PICKUP:
                return ERR
        res = run("dungeon_main", Fake(full), {}, {"times": 3, "levels": [51]})
        self.assertEqual(dict(res.progress), {"51": 1})            # 背包满那次已结算，算 1 次
        conn = Fake()
        res = run("dungeon_main", conn, {}, {"times": 3, "levels": [51], "resume_done": {"51": 1}})
        self.assertEqual(conn.routes().count("finishSingleCopy"), 2)   # 只补剩下的 2 次
        self.assertEqual(dict(res.progress), {"51": 3})
        conn = Fake()
        res = run("dungeon_main", conn, {}, {"times": 3, "levels": [51], "resume_done": {"51": 3}})
        self.assertEqual((res.status, conn.sent), ("submitted", []))   # 都打完了：什么也不发

    def test_other_pickup_error_does_not_settle(self):
        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"code": 200, "rnd": 1, "drops": [{"monsterId": 1, "index": 0}]}
            if route == dungeon.R_PICKUP:
                return {"code": 500, "error": "别的错误"}
        conn = Fake(handler)
        self.assertEqual(run("dungeon_main", conn, {}).status, "blocked")
        self.assertNotIn("finishSingleCopy", conn.routes())

    def test_missing_rnd_does_not_settle(self):
        conn = Fake(lambda r, b: {"code": 200} if r == dungeon.R_CREATE else None)
        self.assertEqual(run("dungeon_main", conn, {}).status, "blocked")
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy"])

    def test_dict_drops_without_dropindex_are_not_guessed(self):
        conn = Fake(lambda r, b: {"code": 200, "rnd": 1, "drops": {"a": {"monsterId": 1}}} if r == dungeon.R_CREATE else None)
        self.assertEqual(run("dungeon_main", conn, {}).status, "blocked")
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy"])

    def test_uncertain_is_not_swallowed_by_other_tasks(self):
        # 回归：UncertainOutcome 曾继承 AllianceError，被任务里的 except AllianceError 吞掉
        from zmxy.tasks import vip_gift
        conn = Fake(lambda r, b: (_ for _ in ()).throw(TimeoutError("t")))
        with self.assertRaises(UncertainOutcome):
            run("vip_gift", conn, {"vip": {"level": 3}})
        self.assertEqual(len(conn.sent), 1)


class NoPickupTests(unittest.TestCase):
    ALL = [key for key, _ in pickup.OPTIONS]

    def test_kinds_come_from_the_tables(self):
        for drop, want in (({"type": 2}, "exp"), ({"type": 3}, "spirit"), ({"type": 73}, "pet_exp"),
                           ({"type": "coin", "subType": "spirit"}, "spirit"), ({"type": "petAttr", "subType": "petEXP"}, "pet_exp"),
                           ({"type": 4, "subType": 101014003}, "potion"), ({"type": "item", "itemId": "101014004"}, "potion"),
                           ({"type": 1}, None), ({"type": 68}, None), ({"type": "item", "subType": "101045121"}, None),
                           ({"type": "item", "subType": "x"}, None), ({"monsterId": 1, "index": 0}, None)):
            self.assertEqual(pickup.kind(drop), want, drop)
        for prop in range(101014001, 101014009):                  # 萝卜、蓝瓶、蓝壶、蟠桃各档
            self.assertEqual(pickup.kind({"type": "item", "subType": str(prop)}), "potion")

    DROPS = [{"type": 2, "index": 0}, {"type": 3, "index": 0}, {"type": 73, "index": 0},
             {"type": "item", "subType": "101014001", "index": 0}, {"type": "item", "subType": "101045121", "index": 0}]

    def picked(self, no_pickup):
        seen = {}

        def handler(route, body):
            if route == dungeon.R_CREATE:
                seen["client"] = body["rnd"]
                return {"code": 200, "rnd": 9, "drops": self.DROPS}

        conn = Fake(handler)
        res = run("dungeon_elite", conn, elite_info(used=1), no_pickup=no_pickup)
        self.assertEqual(res.status, "submitted", res.detail)
        client = dungeon.as3(seen["client"])
        seed = dungeon.as3(9) ^ client
        return [(b["index"] ^ seed) - (b["key"] ^ client) for r, b in conn.sent if r == dungeon.R_PICKUP], res.detail

    def test_skipped_drops_send_no_request_and_others_keep_their_positions(self):
        self.assertEqual(self.picked(())[0], [0, 1, 2, 3, 4])
        self.assertEqual(self.picked(["spirit", "potion"])[0], [0, 2, 4])
        positions, detail = self.picked(self.ALL)
        self.assertEqual(positions, [4])
        self.assertIn("按设置未拾取 4 项", detail)
        self.assertNotIn("未拾取", self.picked(())[1])

    def test_nightmare_chest_drops_are_filtered_too(self):
        def handler(route, body):
            if route == dungeon.R_TREASURE_DROP:
                return {"code": 200, "drop": [{"type": 3}, {"type": "item", "subType": "101014008"}, {"type": 4, "subType": 101045121}]}

        conn = Fake(handler)
        res = run("dungeon_nightmare", conn, NIGHTMARE_INFO, {"times": 1}, no_pickup=["spirit", "potion"])
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual([b for r, b in conn.sent if r == dungeon.R_TREASURE_CLAIM], [{"i": 2}])
        self.assertIn("turnCard", conn.routes())                  # 宝箱还在，照常翻牌

    STONE_DROPS = [{"type": "item", "subType": "101005201", "index": 0},   # 灵纹妖玉：强化道具，名字不带"石"
                   {"type": "item", "subType": "101212002", "index": 0},   # 守护石：宝石，不是强化道具
                   {"type": 3, "index": 0}, {"monsterId": 1, "index": 0},
                   {"type": 4, "subType": 101005001, "index": 0}]          # 万能强化石

    def stones(self, task_id, info, options):
        conn = Fake(lambda r, b: {"code": 200, "rnd": 9, "drops": self.STONE_DROPS} if r == dungeon.R_CREATE else None)
        res = run(task_id, conn, info, options)
        self.assertEqual(res.status, "submitted", res.detail)
        return conn.routes().count("syncDropInfo"), conn.routes(), res.detail

    def test_elite_stones_only_picks_just_enhancement_props_by_table_category(self):
        self.assertEqual([pickup.is_stone(d) for d in self.STONE_DROPS], [True, False, False, False, True])
        self.assertEqual(sum(pickup.is_stone({"subType": k}) for k in data.table("Prop")), 42)
        picked, routes, detail = self.stones("dungeon_elite", elite_info(used=1), {"stones_only": True})
        self.assertEqual(picked, 2)
        self.assertIn("按设置未拾取 3 项", detail)
        self.assertEqual(routes[-3:], ["finishSingleCopy", "getCard", "turnCard"])   # 结算、翻牌照常
        self.assertEqual(self.stones("dungeon_elite", elite_info(used=1), {"stones_only": False})[0], 5)

    def main_run(self, options):
        conn = Fake()
        res = run("dungeon_main", conn, {}, options)
        self.assertEqual(res.status, "submitted", res.detail)
        return conn

    def test_main_no_clear_settles_as_a_loss(self):
        conn = self.main_run({"no_clear": True})
        self.assertEqual(conn.routes().count("createSinglePlayerCopy"), 2)   # 仍然进 Boss 房
        finish = [b for r, b in conn.sent if r == dungeon.R_FINISH][0]
        self.assertEqual((finish["win"], finish["star"]), (0, 0))
        normal = [b for r, b in self.main_run({}).sent if r == dungeon.R_FINISH][0]
        self.assertEqual((normal["win"], normal["star"]), (1, 3))

    def test_hidden_no_clear_settles_as_a_loss(self):
        conn = Fake()
        res = run("dungeon_hidden", conn, {}, {"no_clear": True})
        self.assertEqual(res.status, "submitted", res.detail)
        finish = [b for r, b in conn.sent if r == dungeon.R_FINISH][0]
        self.assertEqual((finish["win"], finish["star"]), (0, 0))

    def test_main_surface_only_skips_the_boss_room(self):
        conn = self.main_run({"surface_only": True})
        self.assertEqual(conn.routes().count("createSinglePlayerCopy"), 1)
        self.assertEqual(conn.routes()[-1], "finishSingleCopy")
        self.assertEqual([b for r, b in conn.sent if r == dungeon.R_FINISH][0]["win"], 1)

    def test_nightmare_takes_a_single_level(self):
        limits = {t: load_tasks()[t].choices.limit for t in LEVEL}
        self.assertEqual(limits, {"dungeon_main": 0, "dungeon_hidden": 0, "dungeon_elite": 0, "dungeon_nightmare": 1})
        conn = Fake(lambda route, body: {"code": 200})
        r = run("dungeon_nightmare", conn, NIGHTMARE_INFO, {"times": 1, "levels": [40050, 40060]})  # 以前存的多关：不执行
        self.assertEqual((r.status, conn.sent), ("blocked", []))

    def test_stones_only_is_an_elite_option_only(self):
        self.assertEqual(self.stones("dungeon_main", {}, {"stones_only": True})[0], 10)   # 主线两间房，全部照捡
        toggles = {t: load_tasks()[t].choices.toggles for t in LEVEL}
        self.assertEqual(toggles.pop("dungeon_elite"), (("stones_only", "只捡强化石"),))
        self.assertEqual(toggles.pop("dungeon_main"), (("surface_only", "只刷表层"), ("no_clear", "不要通关")))
        self.assertEqual(toggles.pop("dungeon_hidden"), (("no_clear", "不要通关"),))
        self.assertEqual(toggles.pop("dungeon_nightmare"), (("key_gem_box", "宝石盒概率≥2/3用钥匙"),))
        self.assertEqual(toggles, {})

    def test_setting_travels_from_the_saved_account_to_the_task(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from zmxy import core
        from zmxy.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "s.json")
            saved = {"username": "u", "saves": {"1": {"tasks": ["dungeon_main"], "no_pickup": ["exp"]}}}
            self.assertEqual(store.to_account(saved).no_pickup, frozenset({"exp"}))
            self.assertEqual(store.to_account({"username": "u"}).no_pickup, frozenset())
            account = store.to_account(saved)
        seen = []
        spec = core.TaskSpec("dungeon_main", "t", lambda ctx: seen.append(ctx.no_pickup) or core.TaskResult("dungeon_main", "skipped"))

        class Sess(Session):
            def __enter__(self):
                return self

        with mock.patch("zmxy.session.Session", Sess), mock.patch("zmxy.runner.load_tasks", return_value={"dungeon_main": spec}):
            core.run_account(account)
        self.assertEqual(seen, [frozenset({"exp"})])


class RewardTests(unittest.TestCase):
    def test_only_give_counts_and_push_is_fallback(self):
        gains = dungeon.Gains()
        gains.add({"drops": [{"itemId": 1, "count": 9}], "give": {"items": [{"itemId": 101045121, "count": 2}], "spirit": 50}}, [])
        gains.add({"code": 200}, [{"give": {"items": [{"itemId": 101045121, "count": 1}]}}])
        gains.add({"give": {"items": [{"itemId": 101045121, "count": 1}]}}, [{"give": {"items": [{"itemId": 101045121, "count": 1}]}}])
        self.assertEqual(gains.items["101045121"], 4)   # 响应有 give 时不再叠加推送
        self.assertEqual(gains.items["spirit"], 50)
        self.assertNotIn("1", gains.items)


class MixConn(BatchConn):
    """还支持不同路由一起发：记下每次一起发的路由，transport 给了就在一起发时抛出。"""

    def __init__(self, handler=None, transport=None):
        super().__init__(handler)
        self.mixed, self.transport = [], transport

    def request_mixed(self, items):
        self.mixed.append([route.split(".")[-1] for route, _ in items])
        if self.transport:
            for route, body in items[:1]:
                self.sent.append((route, body))
            raise self.transport
        return [self.request(route, body) for route, body in items]


class MainPipelineTests(unittest.TestCase):
    """主线：每间最后一批拾取和开下一间 / 结算一起发（2026-11-08 真机验证过服务器按顺序处理）。"""

    def handler(self, n=3, reject=None):
        rooms = []

        def handle(route, body):
            if route == dungeon.R_CREATE:
                rooms.append(body["mid"])
                return {"code": 200, "rnd": 10 + len(rooms), "drops": [{"monsterId": 1, "index": i} for i in range(n)]}
            return reject(route, body, rooms) if reject else None
        handle.rooms = rooms
        return handle

    def test_last_pickups_go_with_next_room_and_with_settlement(self):
        handle = self.handler(30)
        conn = MixConn(handle)
        res = run("dungeon_main", conn, {})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(handle.rooms, [51, 1062])
        self.assertEqual(conn.batches, [(dungeon.R_PICKUP, 24), (dungeon.R_PICKUP, 24)])  # 每间前 24 条照常
        self.assertEqual(conn.mixed, [["syncDropInfo"] * 6 + ["createSinglePlayerCopy"],
                                      ["syncDropInfo"] * 6 + ["finishSingleCopy"]])
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)
        self.assertEqual(conn.routes().count("syncDropInfo"), 60)

    def test_pickups_use_their_own_room_random_and_settlement_uses_last_room(self):
        conn = MixConn(self.handler(1))
        run("dungeon_main", conn, {})
        picks = [b for r, b in conn.sent if r == dungeon.R_PICKUP]
        creates = [b for r, b in conn.sent if r == dungeon.R_CREATE]
        finish = next(b for r, b in conn.sent if r == dungeon.R_FINISH)
        self.assertEqual(len(picks), 2)
        for pick, server, create in zip(picks, (11, 12), creates):  # 掉落序号 0：index = 噪声 ^ (服务器随机数 ^ 客户端随机数)
            client = dungeon.as3(create["rnd"])
            self.assertEqual(pick["index"], dungeon.as3(pick["key"] ^ client) ^ (dungeon.as3(server) ^ client))
        self.assertEqual(finish["rnd"], dungeon.as3(12) ^ dungeon.as3(creates[-1]["rnd"]))

    def test_room_without_drops_still_sends_next_request(self):
        conn = MixConn(self.handler(0))
        res = run("dungeon_main", conn, {})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.mixed, [["createSinglePlayerCopy"], ["finishSingleCopy"]])

    def test_bag_full_in_first_room_settles_with_the_room_already_opened(self):
        def reject(route, body, rooms):
            return ERR if route == dungeon.R_PICKUP and len(rooms) == 1 else None
        conn = MixConn(self.handler(2, reject))
        res = run("dungeon_main", conn, {}, {"times": 3})
        self.assertEqual(res.status, "blocked")
        self.assertIn("背包空间不足", res.detail)
        self.assertEqual(conn.routes().count("createSinglePlayerCopy"), 2)
        self.assertEqual(conn.routes()[-1], "finishSingleCopy")
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)

    def test_bag_full_with_settlement_counts_as_settled_and_never_settles_again(self):
        def reject(route, body, rooms):
            return ERR if route == dungeon.R_PICKUP and len(rooms) == 2 else None
        conn = MixConn(self.handler(2, reject))
        res = run("dungeon_main", conn, {}, {"times": 3})
        self.assertEqual(res.status, "blocked")
        self.assertIn("背包空间不足", res.detail)
        self.assertEqual(conn.routes().count("createSinglePlayerCopy"), 2)  # 只打了这一次，没开下一次
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)        # 和拾取一起结算过，不再补结算

    def test_rejected_next_room_stops_without_settling(self):
        handle = self.handler(1)

        def handler(route, body):
            if route == dungeon.R_CREATE and handle.rooms:
                handle.rooms.append(body["mid"])
                return "房间错误"
            return handle(route, body)
        conn = MixConn(handler)
        res = run("dungeon_main", conn, {})
        self.assertEqual(res.status, "blocked")
        self.assertIn("进入房间 1062 失败", res.detail)
        self.assertNotIn("finishSingleCopy", conn.routes())

    def test_rejected_settlement_is_not_resent(self):
        def reject(route, body, rooms):
            return "结算失败" if route == dungeon.R_FINISH else None
        conn = MixConn(self.handler(1, reject))
        res = run("dungeon_main", conn, {}, {"times": 2})
        self.assertEqual(res.status, "blocked")
        self.assertIn("结算未确认", res.detail)
        self.assertEqual(conn.routes().count("finishSingleCopy"), 1)

    def test_transport_failure_is_uncertain_and_nothing_is_resent(self):
        conn = MixConn(self.handler(1), transport=p.AllianceError("等待响应超时；连接已关闭"))
        with self.assertRaises(UncertainOutcome):
            run("dungeon_main", conn, {})
        self.assertEqual(len(conn.mixed), 1)
        self.assertNotIn("finishSingleCopy", conn.routes())

    def test_only_main_levels_mix(self):
        conn = MixConn(lambda r, b: {"code": 200, "rnd": 5, "drops": [{"monsterId": 1, "index": 0}]} if r == dungeon.R_CREATE else None)
        res = run("dungeon_elite", conn, elite_info())
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.mixed, [])
        conn = MixConn(self.handler(1))
        run("dungeon_hidden", conn, {})
        self.assertEqual(conn.mixed, [])

    def test_connection_without_mixing_keeps_old_order(self):
        conn = BatchConn(self.handler(1))
        run("dungeon_main", conn, {})
        self.assertEqual(conn.routes(), ["createSinglePlayerCopy", "syncDropInfo", "createSinglePlayerCopy", "syncDropInfo", "finishSingleCopy"])


if __name__ == "__main__":
    unittest.main()
