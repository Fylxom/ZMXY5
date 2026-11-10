"""宠物捕捉（定时页）。全部离线。"""

import unittest
from unittest import mock

from zmxy import login, protocol as p
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from zmxy.tasks import pet_catch
from tests.support import FakeConn, run_daily as run

CREATE = "scene.copyHandler.createSinglePlayerCopy"
FINISH = "scene.copyHandler.finishSingleCopy"
CAP = "scene.petHandler.capture"
FILTERABLE = pet_catch.FILTERABLE
WOOD, FIRE, GOLD = 101017001, 101017002, 101017003


def bag(**counts):
    return {"bag": {"item": [{"id": int(k[1:]), "count": v} for k, v in counts.items()]}}


def conn_with(results):
    it = iter(results)
    return FakeConn({CREATE: {"rnd": 77}, FINISH: {}, CAP: lambda b: {"success": next(it)}})


def go(conn, info, options):
    return run("pet_catch", conn, info, options)


class PetCatchTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(pet_catch, "GAP", 0)  # 两批购买之间真的会等 1 秒：测试里不等
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_buy_gourds_in_batches_of_9999(self):
        buy = "scene.shopHandler.buy"
        conn = FakeConn({buy: {}})
        r = run("pet_gourd_buy", conn, {"spirit": 10 ** 9}, {"buy": "25000"})
        self.assertEqual(r.status, "submitted")
        self.assertEqual([b["count"] for b in conn.bodies(buy)], [9999, 9999, 5002])
        self.assertTrue(all(b["shopId"] == 1 for b in conn.bodies(buy)))

    def test_buy_limited_by_spirit(self):
        buy = "scene.shopHandler.buy"
        conn = FakeConn({buy: {}})
        run("pet_gourd_buy", conn, {"spirit": 5000}, {"buy": "10"})
        self.assertEqual([b["count"] for b in conn.bodies(buy)], [2])

    def test_kicked_while_buying_keeps_confirmed_progress_and_resume_deducts(self):
        buy = "scene.shopHandler.buy"
        calls = []

        def kick(body):
            calls.append(body["count"])
            if len(calls) == 2:
                raise p.AllianceError("服务器踢出了当前会话")
            return {}

        info = {"spirit": 10 ** 9}
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info = FakeConn({buy: kick}), info
        s._link = login.Link(s._conn, s.config, [], {"time": 1_000_000})
        spec = load_tasks()["pet_gourd_buy"]
        s.bind(spec)
        ctx = TaskContext(s, {"buy": "25000"})
        with self.assertRaises(UncertainOutcome):
            spec.run(ctx)
        self.assertEqual(dict(ctx.progress), {"n": 9999})  # 只记确认过的那一批
        conn = FakeConn({buy: {}})
        r = run("pet_gourd_buy", conn, info, {"buy": "25000", "resume_done": {"n": 9999}})
        self.assertEqual([b["count"] for b in conn.bodies(buy)], [9999, 5002])

    def test_buy_over_cap_blocks_and_empty_skips(self):
        conn = FakeConn()
        self.assertEqual(run("pet_gourd_buy", conn, {"spirit": 10 ** 9}, {"buy": "10000000"}).status, "blocked")
        self.assertEqual(run("pet_gourd_buy", conn, {"spirit": 10 ** 9}, {"buy": ""}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_catalog_has_four_pets(self):
        self.assertEqual([n for _, n in pet_catch.catalog()], ["天马", "灵花", "白虎", "熊猫"])

    def test_retries_in_same_copy_until_caught_then_settles(self):
        conn = conn_with([0, 1])
        r = go(conn, bag(**{f"i{WOOD}": 5}), {"levels": [10002], "times": 1})
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.routes(), [CREATE, CAP, CAP, FINISH])
        self.assertEqual(conn.bodies(CAP)[0], {"itemId": WOOD, "petId": 170511101})

    def test_settles_as_failure_so_pass_count_stays(self):
        conn = conn_with([1])
        go(conn, bag(**{f"i{WOOD}": 5}), {"levels": [10001], "times": 1})
        sent = conn.bodies(FINISH)[0]
        self.assertEqual((sent["win"], sent["star"]), (0, 0))  # 和主线“不要通关”一样：passNum 不增加

    def test_only_first_gourd_is_used(self):
        conn = conn_with([0, 0, 0])
        go(conn, bag(**{f"i{WOOD}": 2, f"i{FIRE}": 3, f"i{GOLD}": 3}), {"levels": [10001], "times": 1})
        self.assertEqual([b["itemId"] for b in conn.bodies(CAP)], [WOOD, WOOD])

    def test_describe_sex_and_stars(self):
        self.assertEqual(pet_catch.describe({"give": {"pet": [{"sex": 2, "quality": 3}]}}, []), "雌 3星")
        self.assertEqual(pet_catch.describe({}, [{"x": {"sex": 1, "itemId": 170621101}}]), "雄 2星")
        self.assertIn("找不到", pet_catch.describe({"success": 1}, []))

    def test_no_gourds_skips_without_entering(self):
        conn = conn_with([])
        r = go(conn, bag(**{f"i{WOOD}": 0}), {"levels": [10003], "times": 2})
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_missing_bag_blocks_without_request(self):
        conn = conn_with([])
        r = go(conn, {}, {"levels": [10001], "times": 1})
        self.assertEqual(r.status, "blocked")
        self.assertIn("读不到背包里的葫芦数量", r.detail)
        self.assertEqual(conn.sent, [])

    def test_missing_success_flag_stops_without_resend(self):
        conn = FakeConn({CREATE: {"rnd": 1}, FINISH: {}, CAP: {}})
        r = go(conn, bag(**{f"i{WOOD}": 5}), {"levels": [10001], "times": 1})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(conn.bodies(CAP)), 1)

    def test_no_selection_or_blank_times_skips(self):
        self.assertEqual(go(FakeConn(), bag(), {"times": 1}).status, "skipped")
        self.assertEqual(go(FakeConn(), bag(), {"levels": [10001], "times": ""}).status, "skipped")

    def test_unknown_pet_blocks(self):
        self.assertEqual(go(FakeConn(), bag(), {"levels": [10099], "times": 1}).status, "blocked")

    def test_filter_check(self):
        pet = {"sex": 1, "quality": 2, "hpQualification": 900, "reHpQualification": 800, "atkQualification": 700,
               "defQualification": 600, "physical": 50, "bone": 40, "capacity": 30, "fightPower": 5000}
        flt = {"stars": [2], "s2": {"hp": "900", "def": "700"}, "s1": {"hp": "1"}}
        self.assertEqual(pet_catch.check(pet, flt), (False, "2星防御资质 600<700"))
        self.assertEqual(pet_catch.check(pet, {**flt, "s2": {"hp": "900"}}), (True, ""))
        self.assertEqual(pet_catch.check(pet, {"stars": [1]}), (False, "2星不在所选星级里"))
        self.assertEqual(pet_catch.check(pet, {"stars": [1, 2], "s1": {"hp": "9999"}, "s2": {"gg": "41"}}), (False, "2星根骨 40<41"))  # 1星那组不管 2星宠物
        self.assertEqual(pet_catch.check(pet, {"stars": [2], "s2": {"fp": "5001"}}), (False, "2星战力 5000<5001"))
        self.assertEqual(pet_catch.check({"sex": 1, "quality": 1}, {"stars": [1], "s1": {"fp": "1"}}), (None, "读不到1星战力"))
        self.assertFalse(pet_catch.active({"stars": [], "s1": {"hp": "", "tz": " "}, "tz": "3"}))  # 不分星级的旧键不认
        self.assertFalse(pet_catch.active({"s2": {"atk": "1"}}))  # 开关没开，填了也不算
        self.assertEqual(pet_catch.check(pet, {"s2": {"hp": "9999"}}), (True, ""))
        self.assertEqual(pet_catch.check(pet, {"stars": [1], "s2": {"hp": "9999"}}), (False, "2星不在所选星级里"))
        self.assertTrue(pet_catch.active({"stars": [2]}))

    def test_filter_range_has_both_ends(self):
        pet = {"sex": 1, "quality": 1, "hpQualification": 500, "atkQualification": 300}
        self.assertEqual(pet_catch.check(pet, {"stars": [1], "s1": {"hp": "400", "hp_max": "500"}}), (True, ""))
        self.assertEqual(pet_catch.check(pet, {"stars": [1], "s1": {"hp_max": "499"}}), (False, "1星生命资质 500>499"))
        self.assertEqual(pet_catch.check(pet, {"stars": [1], "s1": {"atk": "301", "atk_max": "460"}}), (False, "1星攻击资质 300<301"))
        self.assertTrue(pet_catch.active({"stars": [2], "s2": {"def_max": "700"}}))
        self.assertEqual(pet_catch.check(pet, {"stars": [1, 2], "s2": {"hp_max": "1"}}), (True, ""))  # 2星那行不管 1星宠物

    def test_ranges_come_from_pet_table(self):
        self.assertEqual(pet_catch.ranges(10001)[1]["hp"], (340, 550))   # 天马 1 星
        self.assertEqual(pet_catch.ranges(10001)[2]["def"], (470, 760))  # 天马 2 星
        self.assertEqual(pet_catch.ranges(10002)[2]["rehp"], (560, 900))  # 灵花 2 星
        self.assertEqual(sorted(pet_catch.ranges(10002)[1]), ["atk", "def", "fp", "gg", "hp", "ql", "rehp", "tz"])
        self.assertEqual((pet_catch.ranges(10001)[1]["fp"], pet_catch.ranges(10002)[2]["fp"]), ((979, 3350), (1799, 4647)))
        self.assertEqual((pet_catch.ranges(10001)[1]["tz"], pet_catch.ranges(10001)[2]["ql"]), ((1, 4), (1, 5)))

    def real_power(self, pet):
        """真机读到的宠物（2026-10-06）：按公式重算战力。"""
        from zmxy import data, state
        chars = {r["id"]: r for r in state.table_rows("PetCharacter")}
        perks = {r["id"]: r for r in state.table_rows("PetPerks")}
        quals = {"hpMax": pet["hp"], "atk": pet["atk"], "phyDef": pet["def"], "reHP": pet["rehp"]}
        return pet_catch.power(data.row("Pet", pet["id"]), quals, chars[pet["character"]], pet["bone"],
                               [perks[f"{sid}_{lv}"] for sid, lv in pet["perks"].items()], pet["level"])

    def test_power_formula_matches_real_pets(self):
        # 1 级汪汪：两个潜能（矫捷之姿、光之庇佑），性格胆小
        self.assertEqual(self.real_power({"id": 171121201, "level": 1, "bone": 2, "character": 11, "hp": 623, "atk": 744, "def": 588, "rehp": 666,
                                          "perks": {"8200810": 1, "8201110": 1}}), 3336)
        # 164 级神威青龙：五个潜能（其中一个 2 级），性格悠闲
        self.assertEqual(self.real_power({"id": 170921201, "level": 164, "bone": 3, "character": 9, "hp": 635, "atk": 873, "def": 520, "rehp": 485,
                                          "perks": {"8200110": 1, "8201010": 2, "8201110": 1, "8201310": 1, "8201410": 1}}), 85356)

    def test_power_range_of_fresh_pets(self):
        self.assertEqual(pet_catch.power_range(10001), {1: (979, 3350), 2: (1638, 4483)})  # 天马
        self.assertEqual(pet_catch.power_range(10002), {1: (959, 3325), 2: (1799, 4647)})  # 灵花

    def test_talent_and_power_ranges_are_per_star(self):
        pet = {"sex": 1, "quality": 2, "physical": 3, "bone": 5, "capacity": 1, "fightPower": 1200}
        self.assertEqual(pet_catch.check(pet, {"stars": [2], "s2": {"tz": "3", "tz_max": "3", "gg": "4", "ql_max": "2", "fp": "1200", "fp_max": "1200"}}), (True, ""))
        self.assertEqual(pet_catch.check(pet, {"stars": [2], "s2": {"gg_max": "4"}}), (False, "2星根骨 5>4"))
        self.assertEqual(pet_catch.check(pet, {"stars": [2], "s2": {"ql": "2"}}), (False, "2星潜力 1<2"))
        self.assertEqual(pet_catch.check(pet, {"stars": [2], "s2": {"fp_max": "1199"}}), (False, "2星战力 1200>1199"))
        self.assertEqual(pet_catch.check(pet, {"stars": [1, 2], "s1": {"fp_max": "1", "tz": "9"}}), (True, ""))  # 1星那组不管 2星宠物
        self.assertTrue(pet_catch.active({"stars": [1], "s1": {"tz_max": "4"}}))

    def filtered(self, pets, flt, pet=10001, times=1, gourds=10):
        it = iter(pets)
        conn = FakeConn({CREATE: {"rnd": 77}, FINISH: {}, CAP: lambda b: {"success": 1, "give": {"pet": [next(it)]}}})
        return conn, go(conn, bag(**{f"i{WOOD if pet in FILTERABLE else 101017004}": gourds}), {"levels": [pet], "times": times, "filters": {str(pet): flt}})

    def test_times_count_every_catch_not_only_qualified(self):
        low = {"sex": 1, "quality": 2, "hpQualification": 100}
        high = {"sex": 2, "quality": 2, "hpQualification": 950}
        conn, r = self.filtered([low, high, low, high], {"stars": [2], "s2": {"hp": "900"}}, times=3)
        self.assertEqual(r.status, "submitted")
        self.assertEqual(len(conn.bodies(CAP)), 3)  # 捕 3 只就停，不管合格几只
        self.assertEqual(conn.routes().count(FINISH), 3)  # 每只都结算副本
        self.assertIn("天马×3，合格 1 只，不合格 2 只", r.detail)
        self.assertIn("生命资质 100<900", r.detail)
        conn, r = self.filtered([low, low], {"stars": [2], "s2": {"hp": "900"}}, times=2)
        self.assertEqual((r.status, len(conn.bodies(CAP))), ("submitted", 2))  # 一只合格的都没有也照次数停
        self.assertIn("天马×2，合格 0 只，不合格 2 只", r.detail)

    def test_qualified_pet_is_listed_in_detail(self):
        pet = {"sex": 1, "quality": 1, "hpQualification": 400, "reHpQualification": 380, "atkQualification": 300, "defQualification": 350,
               "physical": 1, "bone": 2, "capacity": 3, "fightPower": 1163, "skill": {"passivity": {"8200610": 2}, "pet": {"4100210": 1}}}
        self.assertEqual(pet_catch.detail(pet), "战力 1163，资质 生命 400/回血 380/攻击 300/防御 350，体质 1/根骨 2/潜力 3，潜能 百折不屈 2级")
        self.assertIn("潜能 无", pet_catch.detail({**pet, "skill": {"passivity": {}}}))
        self.assertIn("战力 ?", pet_catch.detail({"sex": 1}))
        conn, r = self.filtered([pet], {"stars": [1], "s1": {"fp_max": "1200"}})
        self.assertIn("雄 1星 合格：战力 1163，资质 生命 400", r.detail)

    def test_each_pet_has_its_own_filter(self):
        low = {"sex": 1, "quality": 2, "hpQualification": 100}
        conn = FakeConn({CREATE: {"rnd": 77}, FINISH: {}, CAP: lambda b: {"success": 1, "give": {"pet": [low]}}})
        r = go(conn, bag(**{f"i{WOOD}": 10}), {"levels": [10001], "times": 1, "filters": {"10002": {"stars": [2], "s2": {"hp": "900"}}}})
        self.assertNotIn("不合格", r.detail)  # 灵花的筛选不管天马
        r = go(conn, bag(**{f"i{WOOD}": 10}), {"levels": [10001], "times": 1, "filter": {"stars": [2], "s2": {"hp": "900"}}})
        self.assertNotIn("不合格", r.detail)  # 不分宠物的旧键不认

    def test_stop_switch_ends_at_first_qualified(self):
        low = {"sex": 1, "quality": 2, "hpQualification": 100}
        high = {"sex": 2, "quality": 2, "hpQualification": 950}
        conn, r = self.filtered([low, high, high, high], {"stars": [2], "s2": {"hp": "900"}, "stop": True}, times=4)
        self.assertEqual((r.status, len(conn.bodies(CAP))), ("submitted", 2))
        self.assertIn("天马×2，合格 1 只，不合格 1 只", r.detail)
        conn, r = self.filtered([low, low], {"stars": [2], "s2": {"hp": "900"}, "stop": True}, times=2)
        self.assertEqual(len(conn.bodies(CAP)), 2)  # 没捕到合格的：次数到了也停
        conn, r = self.filtered([high, high], {"stars": [2], "s2": {"hp": "900"}, "stop": "1"}, times=2)
        self.assertEqual(len(conn.bodies(CAP)), 2)  # 只认 True，默认关

    def test_filter_stops_when_gourds_run_out(self):
        low = {"sex": 1, "quality": 2, "hpQualification": 100}
        conn, r = self.filtered([low] * 5, {"stars": [2], "s2": {"hp": "900"}}, times=5, gourds=2)
        self.assertEqual(len(conn.bodies(CAP)), 2)
        self.assertIn("不合格 2 只", r.detail)

    FREE = "scene.petHandler.free"

    @staticmethod
    def fresh(n, hp, **over):
        """服务器发来的完整实体：刚捕到的 2 星天马。"""
        return {"entityId": f"p{n}", "id": 170121101, "level": 1, "nickName": "", "equipments": {}, "quality": 2, "sex": 1,
                "hpQualification": hp, "skill": {"pet": {"4114120": 1}}, **over}

    def releasing(self, pets, flt, free_reply=None, times=2):
        it = iter(pets)
        conn = FakeConn({CREATE: {"rnd": 77}, FINISH: {}, self.FREE: free_reply or {},
                         CAP: lambda b: {"success": 1, "give": {"pet": [next(it)]}}})
        return conn, go(conn, bag(**{f"i{WOOD}": 10}), {"levels": [10001], "times": times, "filters": {"10001": flt}})

    def test_release_is_off_by_default(self):
        conn, r = self.releasing([self.fresh(1, 100), self.fresh(2, 950)], {"stars": [2], "s2": {"hp": "900"}})
        self.assertEqual(conn.bodies(self.FREE), [])
        self.assertIn("没放生", r.detail)

    def test_release_frees_rejected_after_settling(self):
        conn, r = self.releasing([self.fresh(1, 100), self.fresh(2, 950)], {"stars": [2], "s2": {"hp": "900"}, "release": True})
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.routes(), [CREATE, CAP, FINISH, self.FREE, CREATE, CAP, FINISH])
        sent = conn.bodies(self.FREE)[0]
        self.assertEqual((sent["entityId"], sent["petId"], sent["petLevel"]), ("p1", 170121101, 1))
        self.assertIn("不合格 1 只（已放生）", r.detail)

    def test_release_never_frees_qualified_pet(self):
        conn, r = self.releasing([self.fresh(1, 950)], {"stars": [2], "s2": {"hp": "900"}, "release": True}, times=1)
        self.assertEqual((r.status, conn.bodies(self.FREE)), ("submitted", []))

    def test_release_blocked_by_guard_stops(self):
        for bad in ({"nickName": "小白"}, {"level": 2}, {"equipments": {"petElf": {"id": 1}}}, {"skill": {"pet": {"4114120": 2}}}):
            conn, r = self.releasing([self.fresh(1, 100, **bad), self.fresh(2, 950)], {"stars": [2], "s2": {"hp": "900"}, "release": True})
            self.assertEqual(conn.bodies(self.FREE), [], bad)  # 闸门拦下，没发出去
            self.assertEqual((r.status, len(conn.bodies(CAP))), ("blocked", 1), bad)
            self.assertIn("放生拦截", r.detail)

    def test_release_refused_by_server_stops(self):
        conn, r = self.releasing([self.fresh(1, 100), self.fresh(2, 950)], {"stars": [2], "s2": {"hp": "900"}, "release": True}, "不能放生")
        self.assertEqual((r.status, len(conn.bodies(CAP)), len(conn.bodies(self.FREE))), ("blocked", 1, 1))

    def test_filter_unreadable_stops_without_more_throws(self):
        conn, r = self.filtered([{"sex": 1, "quality": 2}] * 3, {"stars": [2], "s2": {"hp": "900"}}, times=3)
        self.assertEqual((r.status, len(conn.bodies(CAP))), ("blocked", 1))
        self.assertIn("无法筛选", r.detail)

    def test_filter_ignored_for_other_pets(self):
        conn, r = self.filtered([{"sex": 1, "quality": 1}], {"stars": [2], "s2": {"hp": "900"}}, pet=10003)
        self.assertEqual((r.status, len(conn.bodies(CAP))), ("submitted", 1))
        self.assertNotIn("不合格", r.detail)

    def test_transport_failure_is_uncertain(self):
        with self.assertRaises(UncertainOutcome):
            go(FakeConn(exc=TimeoutError("t")), bag(**{f"i{WOOD}": 5}), {"levels": [10001], "times": 1})


if __name__ == "__main__":
    unittest.main()
