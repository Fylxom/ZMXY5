"""自动洗练：法宝（免费洗炼，按成长值 / 祝福筛选）、羽毛、精魄。全部离线。"""

import copy
import unittest
from unittest import mock

from zmxy import protocol as p
from zmxy import state
from zmxy.core import BLOCKED, SKIPPED, SUBMITTED, Session, UncertainOutcome
from zmxy.tasks import refine as rf
from tests import support
from tests.support import Fake

WORLD: list = [None]  # 这次 run 用的进角信息：Server 把留下来的洗炼结果写回去，洗完重新读取时读得到


def run(task_id, conn, world, options=None):
    WORLD[0] = world
    return support.run(task_id, conn, world, options)


def reread(session):
    """重新读取：服务器现在的样子另拷一份（真的重新进角也是拿到一份新的，不会跟着服务器变）。"""
    session.info = copy.deepcopy(WORLD[0])
    return session.info


def entity(value, eid):
    if isinstance(value, dict):
        if value.get("entityId") == eid:
            return value
        value = list(value.values())
    for child in value if isinstance(value, list) else ():
        if (got := entity(child, eid)) is not None:
            return got
    return None

FABAO = 130080001  # 清境拂尘：成长 32–65，品质 4（最多 2 个祝福）
FABAO_ADVANCED = 130080005  # 进阶后类型 ID 变化，但仍是同一个 entityId
FEATHER = 160000004  # 金鹏之羽：一次 12 羽枝，生命上限 1922
WING = 150080001  # 孔雀之翼
BLUE = 160000002  # 蓝凫之羽：一次 6 羽枝
FLAG = 220031001  # 天气血魄：一次 297 魄心，生命 454–1375、回血 3–8、闪避 91–275
GROWTH_STONE, WISH_STONE, TWIG, HEART = 101301003, 101301002, 101026002, 101026004


def vip(free=50, level=8):
    """VIP 等级和今天的免费洗炼次数：法宝、羽毛、精魄各还剩 free 次（VIP 8：法宝每天 2 次，羽毛 3 次、能选保留，精魄 1 次）。"""
    row = state.row("VIP", level)
    return {"vip": {"level": level}, "everyDaySetting": {used: state.num(row[cap]) - free for cap, used in rf.FREE_FIELD.values()}}


def info(stock=None, growth=40, wish="1", feather=None, flag=None, bag_extra=None, free=50, level=8):
    """身上一件法宝、翅膀上一根羽毛、阵眼里一个精魄；stock = 背包里的材料 {编号: 数量}，None = 没有背包。"""
    out = {
        **vip(free, level),
        "players": [{"equipments": {
            "amulet": {"entityId": "a1", "id": FABAO, "growthRate": growth, "fiveElements": wish},
            "wing": {"entityId": "w1", "id": WING, "level": 5, "inlay": {"0": {"entityId": "f1", "id": FEATHER, **(feather or {"hpMax": 700, "hit": 30})}}},
        }}],
        "formation": {"110": {"entityId": "g1", "id": FLAG, "part": "110", **(flag or {"hpMax": 500, "reHP": 3, "dodge": 100})}},
    }
    if stock is not None:
        out["bag"] = {"item": [{"id": k, "count": v} for k, v in stock.items()], **(bag_extra or {})}
    return out


def pick(pos="0", feather=None, wing=None):
    """设置里选中要洗的那一根。"""
    return {"wing": wing or WING, "pos": pos, "id": feather or FEATHER}


def on(key, **opt):
    if "kinds" in opt:  # 羽毛的条件挂在翅膀下面；测试里只有一只翅膀
        opt["wings"] = {str(WING): {"kinds": opt.pop("kinds")}}
        opt.setdefault("pick", pick())  # 默认选中 0 号孔的金鹏
    if key == "fabao":
        opt.setdefault("pick", {"entity": "a1", "id": FABAO})  # 默认选中身上那一件
    return {key: {"on": True, "times": 50, **opt}}


class Server:
    """每次“试”按脚本给出下一个新属性；记下每次的选择。"""

    def __init__(self, key, rolls, choose=True):
        """choose：羽毛的免费洗炼能不能选保留原属性（响应里的 needReplace）；False = 洗出来直接覆盖。"""
        self.key, self.rolls, self.choose, self.conn = key, list(rolls), choose, Fake(self.handle)
        self.pending, self.lie = None, None  # lie：重新读取时读到的和洗炼结果不一样（只写回这个）

    def keep(self):
        """留下刚洗出来的：写回进角信息里的那一件（属性整套换掉）。"""
        eid, roll = self.pending
        item = entity(WORLD[0], eid)
        for attr, _ in rf.ATTRS:
            item.pop(attr, None)
        item.update(self.lie or roll)

    def handle(self, route, body):
        if route in (rf.R_FABAO_FREE, rf.R_FEATHER_FREE, rf.R_FLAG):
            roll = self.rolls.pop(0)
            if isinstance(roll, str):
                return roll
            self.pending = (body["entityId"], roll)
            if not self.choose:
                self.keep()
            return {self.key: roll, "needReplace": self.choose}
        if body.get("replace") or body.get("confirm") == 1:
            self.keep()
        return {}


def g(growth, wish="1"):
    return {"growthRate": growth, "fiveElements": wish}


def w(wish, growth=40):
    return g(growth, wish)


def kind(attrs, feather=None):
    """一种羽毛的条件（默认金鹏之羽）。"""
    return {str(feather or FEATHER): {"attrs": attrs}}


def bodies(conn, name):
    """发出去的请求体（二级密码由 Session 自动带上，这里不比）。"""
    return [{k: v for k, v in b.items() if k != "password"} for b in conn.bodies(name)]


REFRESH = mock.patch.object(Session, "refresh", reread)  # 洗之前的重新读取：默认读回来和登录时一样；RefreshTest 另外换


def setUpModule():
    rf.GAP, rf.LIVE = 0, True  # 洗练逻辑按开着测；关着的样子见 DryTest
    REFRESH.start()


def tearDownModule():
    REFRESH.stop()


def fabao(rolls):
    """法宝的免费洗炼：洗出来直接覆盖。"""
    return Server("newMagicWeapon", rolls, choose=False)


GROW = {"growth_on": True, "growth": "60"}


def wish(num, *need):
    return {"wish_on": True, "wish_num": num, "wish": list(need)}


class FabaoTest(unittest.TestCase):
    def test_array_wishes_are_displayed_and_used_for_filtering(self):
        sv = fabao([g(61, ["3", 4])])
        result = run(rf.ID, sv.conn, info(wish=[1, 2.0]), on("fabao", **GROW, **wish(2, 3, 4)))
        self.assertEqual(result.status, SUBMITTED, result.detail)
        self.assertEqual(bodies(sv.conn, "tryResetAttrFreeVIP"), [{"entityId": "a1", "attr": 3}])
        self.assertEqual(result.profile["fabaos"][0]["wish"], [3, 4])
        self.assertIn("祝福玄武、朱雀 已达标", result.detail)

    def test_already_matching_array_wishes_are_not_washed(self):
        sv = fabao([])
        result = run(rf.ID, sv.conn, info(wish=[3, "4"]), on("fabao", **wish(2, 3, 4)))
        self.assertEqual(result.status, SKIPPED, result.detail)
        self.assertEqual(sv.conn.sent, [])
        self.assertEqual(result.profile["fabaos"][0]["wish"], [3, 4])

    def test_wish_formats_and_invalid_values(self):
        for raw, expected in (("1, 4", {1, 4}), ([1, "4"], {1, 4}), ((1, 4), {1, 4}),
                              (3, {3}), (3.0, {3}), ("", set()), ([], set()), (None, None),
                              ([True], None), ([1.5], None), ([float('inf')], None), ([9], None),
                              ([1, "bad"], None), ({"1": 4}, None)):
            with self.subTest(raw=raw):
                self.assertEqual(rf._beasts({"fiveElements": raw}), expected)

    def test_washes_until_growth_reaches_target(self):
        sv = fabao([g(35), g(61, "2"), g(64)])
        r = run(rf.ID, sv.conn, info(), on("fabao", **GROW))
        self.assertEqual(r.status, SUBMITTED)
        self.assertEqual(bodies(sv.conn, "tryResetAttrFreeVIP"), [{"entityId": "a1", "attr": 3}] * 2)  # 61 就停，第三次不洗
        self.assertEqual(len(sv.conn.sent), 2)  # 直接覆盖：没有“保留 / 替换”那一步
        self.assertIn("成长61 祝福青龙 已达标", r.detail)

    def test_already_enough_sends_nothing(self):
        sv = fabao([])
        r = run(rf.ID, sv.conn, info(growth=62), on("fabao", **GROW))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))
        self.assertEqual(r.profile[rf.IDLE], "法宝")  # 达标了免费次数还有：标红

    def test_growth_above_cap_not_tried(self):
        sv = fabao([])
        r = run(rf.ID, sv.conn, info(), on("fabao", growth_on=True, growth="66"))
        self.assertEqual(sv.conn.sent, [])
        self.assertIn("到不了", r.detail)

    def test_last_free_wash_meeting_both_conditions_turns_red(self):
        sv = fabao([g(61, [3, 4])])
        result = run(rf.ID, sv.conn, info(free=1), on("fabao", **GROW, **wish(2, 3, 4)))
        self.assertEqual(result.status, SUBMITTED, result.detail)
        self.assertEqual(len(sv.conn.sent), 1)
        self.assertEqual(result.profile[rf.IDLE], "法宝")
        self.assertIn("今天的免费次数还剩 0", result.detail)
        self.assertNotIn("免费次数还没用完", result.detail)

    def test_already_met_with_no_free_washes_turns_red(self):
        sv = fabao([])
        result = run(rf.ID, sv.conn, info(growth=61, wish=[3, 4], free=0), on("fabao", **GROW, **wish(2, 3, 4)))
        self.assertEqual(sv.conn.sent, [])
        self.assertEqual(result.profile[rf.IDLE], "法宝")

    def test_one_unmet_condition_does_not_turn_red(self):
        sv = fabao([g(61, [1])])
        result = run(rf.ID, sv.conn, info(free=1), on("fabao", **GROW, **wish(2, 3, 4)))
        self.assertEqual(len(sv.conn.sent), 1)
        self.assertEqual(result.profile[rf.IDLE], "")

    def test_wish_needs_count_and_the_wanted_ones(self):
        sv = fabao([w("3"), w("1,2"), w("3,4")])
        r = run(rf.ID, sv.conn, info(), on("fabao", **wish(2, 3)))
        self.assertEqual(len(sv.conn.sent), 3)  # 玄武只有 1 个不够数；白虎青龙够数没有玄武；玄武朱雀才行
        self.assertIn("祝福玄武、朱雀 已达标", r.detail)

    def test_wish_count_alone_takes_any(self):
        sv = fabao([w("4"), w("2,5")])
        r = run(rf.ID, sv.conn, info(), on("fabao", **wish(2)))
        self.assertEqual(len(sv.conn.sent), 2)
        self.assertIn("已达标", r.detail)

    def test_both_conditions_must_hold(self):
        sv = fabao([g(62, "1"), g(40, "3,4"), g(63, "3,4")])
        r = run(rf.ID, sv.conn, info(), on("fabao", **GROW, **wish(2, 3, 4)))
        self.assertEqual(len(sv.conn.sent), 3)  # 成长够了祝福不够 → 接着洗（成长也跟着重洗）
        self.assertIn("成长63 祝福玄武、朱雀 已达标", r.detail)

    def test_wish_more_than_quality_allows(self):
        sv = fabao([])
        r = run(rf.ID, sv.conn, info(), on("fabao", **wish(3)))
        self.assertEqual(sv.conn.sent, [])
        self.assertIn("凑不齐", r.detail)

    def test_switch_off_condition_is_ignored(self):
        sv = fabao([])
        r = run(rf.ID, sv.conn, info(growth=62), on("fabao", **GROW, wish_on=False, wish_num=2, wish=[3, 4]))
        self.assertEqual(sv.conn.sent, [])
        self.assertIn("已达标", r.detail)

    def test_both_switches_off_not_washed(self):
        sv = fabao([])
        r = run(rf.ID, sv.conn, info(), on("fabao", growth="60", wish_num=2))
        self.assertEqual(sv.conn.sent, [])
        self.assertIn("都没开", r.detail)

    def test_only_the_picked_one_is_washed(self):
        sv = fabao([g(61)])
        extra = {"amulet": [{"entityId": "b9", "id": FABAO, "growthRate": 33, "fiveElements": "1"}]}
        run(rf.ID, sv.conn, info({}, growth=62, bag_extra=extra), on("fabao", **GROW, pick={"entity": "b9", "id": FABAO}))
        self.assertEqual([b["entityId"] for b in sv.conn.bodies("tryResetAttrFreeVIP")], ["b9"])  # 背包里那件；身上那件不碰

    def test_no_pick_or_missing_not_washed(self):
        for pick, text in ((None, "没有选"), ({"entity": "zz", "id": FABAO}, "没找到")):
            sv = fabao([])
            r = run(rf.ID, sv.conn, info(), on("fabao", **GROW, pick=pick))
            self.assertEqual(sv.conn.sent, [])
            self.assertIn(text, r.detail)

    def test_advanced_fabao_keeps_saved_selection_and_settings(self):
        world = info(growth=80)
        world["players"][0]["equipments"]["amulet"]["id"] = FABAO_ADVANCED
        sv = fabao([g(110)])
        options = on("fabao", pick={"entity": "a1", "id": FABAO}, per_pick=[
            {"entity": "a1", "id": FABAO, "growth_on": True, "growth": "100", "wish_on": False},
        ])

        result = run(rf.ID, sv.conn, world, options)

        self.assertEqual(result.status, SUBMITTED, result.detail)
        self.assertEqual(bodies(sv.conn, "tryResetAttrFreeVIP"), [{"entityId": "a1", "attr": 3}])
        self.assertEqual(result.profile["fabaos"][0]["id"], FABAO_ADVANCED)
        self.assertIn("已达标", result.detail)

    def test_advanced_fabao_uses_saved_per_pick_settings_after_pick_id_refresh(self):
        world = info(growth=80)
        world["players"][0]["equipments"]["amulet"]["id"] = FABAO_ADVANCED
        sv = fabao([g(110)])
        options = on("fabao", pick={"entity": "a1", "id": FABAO_ADVANCED}, per_pick=[
            {"entity": "a1", "id": FABAO, "growth_on": True, "growth": "100", "wish_on": False},
        ])

        result = run(rf.ID, sv.conn, world, options)

        self.assertEqual(result.status, SUBMITTED, result.detail)
        self.assertEqual(bodies(sv.conn, "tryResetAttrFreeVIP"), [{"entityId": "a1", "attr": 3}])
        self.assertIn("已达标", result.detail)

    def test_stops_when_free_times_run_out(self):
        sv = fabao([g(33), g(34), g(35)])
        r = run(rf.ID, sv.conn, info(free=2), on("fabao", **GROW))
        self.assertEqual(len(sv.conn.sent), 2)
        self.assertIn("免费次数用完", r.detail)

    def test_free_times_come_from_vip_table(self):
        sv = fabao([])
        world = {**info(), "vip": {"level": 3}, "everyDaySetting": {}}
        r = run(rf.ID, sv.conn, world, on("fabao", **GROW))
        self.assertEqual(sv.conn.sent, [])  # VIP 3 法宝每天 0 次免费
        self.assertIn("免费次数用完", r.detail)
        self.assertEqual(rf.free_left({"vip": {"level": 8}, "everyDaySetting": {"amuletResetCnt": 1}}, "fabao"), 1)

    def test_logs_only_what_changed(self):
        sv, logs = fabao([g(40, "2"), g(61, "2")]), []
        with mock.patch.object(rf._Budget, "note", lambda self, text: logs.append(text)):
            run(rf.ID, sv.conn, info(), on("fabao", **GROW))
        self.assertEqual(logs, ["清境拂尘：祝福白虎 → 青龙，直接覆盖", "清境拂尘：成长40 → 61，直接覆盖"])

    def test_unreadable_result_stops(self):
        sv = Server("other", [g(64)], choose=False)
        r = run(rf.ID, sv.conn, info(), on("fabao", **GROW))
        self.assertEqual(len(sv.conn.sent), 1)
        self.assertIn("读不到", r.detail)

    def test_reread_that_differs_stops(self):
        sv = fabao([g(50), g(64)])
        sv.lie = g(51)
        r = run(rf.ID, sv.conn, info(), on("fabao", **GROW))
        self.assertEqual(len(sv.conn.sent), 1)
        self.assertIn("对不上", r.detail)

    def test_refused_stops(self):
        sv = fabao(["次数不足"])
        r = run(rf.ID, sv.conn, info(), on("fabao", **GROW))
        self.assertEqual(r.status, BLOCKED)

    def test_timeout_not_resent(self):
        def handle(route, body):
            raise p.AllianceError("timeout")
        conn = Fake(handle)
        with self.assertRaises(UncertainOutcome):
            run(rf.ID, conn, info(), on("fabao", **GROW))
        self.assertEqual(len(conn.sent), 1)

    def test_profile_lists_worn_first(self):
        extra = {"amulet": [{"entityId": "b9", "id": FABAO, "growthRate": 33, "fiveElements": "1,4"}]}
        got = rf.fabao_profile(info({}, bag_extra=extra))["fabaos"]
        self.assertEqual([(f["entity"], f["worn"], f["growth"], f["wish"]) for f in got], [("a1", True, 40.0, [1]), ("b9", False, 33.0, [1, 4])])
        self.assertEqual(rf.fabao_caps(FABAO), (32.0, 65.0, 2))

    def test_profile_distinguishes_equipped_and_bag_owners(self):
        extra = {"amulet": [{"entityId": eid, "id": FABAO, "index": index, **g(33)}
                            for eid, index in (("b1", 0), ("b2", "1"), ("unknown", 2))]}
        world = info({}, bag_extra=extra)
        world["p1"] = world.pop("players")[0]
        world["p2"] = {"equipments": {"amulet": {"entityId": "a2", "id": FABAO, **g(35)}}}
        got = rf.fabao_profile(world)["fabaos"]
        self.assertEqual([(f["entity"], f["index"], f["worn"]) for f in got],
                         [("a1", 0, True), ("a2", 1, True), ("b1", 0, False), ("b2", 1, False), ("unknown", None, False)])

    def test_profile_keeps_second_player_index_when_first_is_empty(self):
        world = info()
        world["players"] = [None, world["players"][0]]
        self.assertEqual(rf.fabao_profile(world)["fabaos"][0]["index"], 1)

    def test_equipment_owner_wins_over_duplicate_bag_entry(self):
        worn = {"entityId": "a2", "id": FABAO, "index": 0, **g(42)}
        world = {"bag": {"amulet": [{**worn, "growthRate": 33}]}, "p2": {"equipments": {"amulet": worn}}}
        got = rf.fabao_profile(world)["fabaos"]
        self.assertEqual([(f["entity"], f["index"], f["worn"], f["growth"]) for f in got], [("a2", 1, True, 42.0)])

    def test_missing_owner_is_not_assumed_to_be_first_player(self):
        world = {"bag": {"amulet": [{"entityId": "b9", "id": FABAO, **g(33)}]}}
        self.assertIsNone(rf.fabao_profile(world)["fabaos"][0]["index"])

    def test_second_players_same_named_fabao_only_washes_picked_entity(self):
        world = info()
        world["p1"] = world.pop("players")[0]
        world["p2"] = {"equipments": {"amulet": {"entityId": "a2", "id": FABAO, **g(35)}}}
        sv = fabao([g(61)])
        result = run(rf.ID, sv.conn, world, on("fabao", **GROW, pick={"entity": "a2", "id": FABAO}))
        self.assertEqual(bodies(sv.conn, "tryResetAttrFreeVIP"), [{"entityId": "a2", "attr": 3}])
        self.assertEqual(world["p1"]["equipments"]["amulet"]["growthRate"], 40)
        self.assertEqual([(f["entity"], f["index"], f["growth"]) for f in result.profile["fabaos"]], [("a1", 0, 40.0), ("a2", 1, 61.0)])


class FeatherTest(unittest.TestCase):
    def test_locks_done_attrs_and_pays_more(self):
        # 要生命 ≥ 961 和攻击（有就行）：先出攻击（留）、锁攻击再洗、洗出生命 1000（留）
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "hpMax": 800}, {"atk": 10, "hpMax": 1000}])
        r = run(rf.ID, sv.conn, info({TWIG: 60}), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        tries = bodies(sv.conn, "featherVIPRest")
        self.assertEqual([b["lock"] for b in tries], [[], ["atk"], ["atk"]])
        self.assertEqual(tries[0], {"entityId": "f1", "lock": [], "index": 0})
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [True, True, True])
        self.assertEqual(r.status, SUBMITTED)
        self.assertIn("生命1000", r.detail)

    def test_locked_attr_lost_keeps_old(self):
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"hpMax": 1900, "hit": 5}, {"atk": 10, "hpMax": 1000}])
        run(rf.ID, sv.conn, info({TWIG: 90}), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [True, False, True])

    def test_manual_lock_always_sent_and_never_given_up(self):
        # 手动锁了命中（不在要的里面）：每次都带着；要生命和攻击，只剩 1 条空位 → 洗出一条就算到头
        locks = {"locks": {str(WING): {"0": {"id": FEATHER, "attrs": ["hit"]}}}}
        sv = Server("feather", [{"hit": 9, "atk": 10}, {"hit": 30, "atk": 10}])
        r = run(rf.ID, sv.conn, info({TWIG: 90}), {"feather": on("feather", kinds=kind({"hpMax": "961", "atk": "1"}))["feather"] | locks})
        self.assertEqual([b["lock"] for b in bodies(sv.conn, "featherVIPRest")], [["hit"], ["hit"]])
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [False, True])  # 命中变低的那次不要
        self.assertEqual(r.status, SUBMITTED)

    def test_manual_lock_on_everything_is_not_washed(self):
        locks = {"locks": {str(WING): {"0": {"id": FEATHER, "attrs": ["hit", "hpMax"]}}}}
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, info({TWIG: 90}), {"feather": on("feather", kinds=kind({"atk": "1"}))["feather"] | locks})
        self.assertEqual(sv.conn.sent, [])
        self.assertIn(rf.ALL_LOCKED, r.detail)
        self.assertEqual(r.profile[rf.IDLE], "羽毛")  # 全锁着没法洗，免费次数还有：标红

    def test_rereads_after_every_wash_and_uses_what_it_read(self):
        reads = []
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "hpMax": 1000}])
        with mock.patch.object(Session, "refresh", lambda s: reads.append(1) or reread(s)):
            r = run(rf.ID, sv.conn, info({}), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        self.assertEqual(len(reads), 3)  # 开头 1 次 + 每洗完一次 1 次
        self.assertIn("生命1000 攻击10 已达标", r.detail)

    def test_reread_that_differs_from_the_result_stops(self):
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "hpMax": 1000}])
        sv.lie = {"dodge": 9, "hit": 5}
        r = run(rf.ID, sv.conn, info({}), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        self.assertEqual(len(sv.conn.bodies("featherVIPRest")), 1)
        self.assertEqual(r.status, BLOCKED)  # 洗过一次但状态对不上：要人工核对，不算完成
        self.assertIn("对不上", r.detail)

    def test_reread_failure_stops(self):
        calls = []

        def refresh(s):
            calls.append(1)
            if len(calls) > 1:
                raise p.AllianceError("刷新角色状态失败：断线")
            return reread(s)
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "hpMax": 1000}])
        with mock.patch.object(Session, "refresh", refresh):
            r = run(rf.ID, sv.conn, info({}), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        self.assertEqual(len(sv.conn.bodies("featherVIPRest")), 1)
        self.assertIn("重新读取失败", r.detail)

    def test_manual_lock_for_another_kind_in_that_hole_is_ignored(self):
        self.assertEqual(rf.feather_locks({"locks": {"1": {"0": {"id": BLUE, "attrs": ["hit"]}}}}, 1, "0", FEATHER), [])
        self.assertEqual(rf.feather_locks({"locks": {"1": {"0": {"id": FEATHER, "attrs": ["hit", "atk"]}}}}, 1, 0, FEATHER), ["atk", "hit"])

    def test_stops_when_free_times_run_out(self):
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "hit": 6}, {"atk": 10, "hit": 7}])
        r = run(rf.ID, sv.conn, info({}, free=2), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        self.assertEqual(len(sv.conn.bodies("featherVIPRest")), 2)  # 今天只剩 2 次免费：洗完就停，不耗羽枝
        self.assertEqual(r.status, SUBMITTED)
        self.assertIn("免费次数用完", r.detail)
        self.assertEqual(r.profile[rf.IDLE], "")

    def test_no_free_times_left_sends_nothing(self):
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, info({TWIG: 999}, free=0), on("feather", kinds=kind({"hpMax": "961"})))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))  # 羽枝再多也不用
        self.assertIn("免费次数用完", r.detail)

    def test_feather_uses_up_free_times_whatever_times_says(self):
        for times in (1, 0, None):  # 羽毛没有次数框：旧设置里留着的 times 不管是几都不看
            sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "hit": 6}, {"atk": 10, "hit": 7}])
            opt = on("feather", kinds=kind({"hpMax": "961", "atk": "1"}), times=times)
            self.assertEqual(rf.summary(opt), "羽毛")
            r = run(rf.ID, sv.conn, info({}, free=3), opt)
            self.assertEqual(len(sv.conn.bodies("featherVIPRest")), 3)
            self.assertIn("免费次数用完", r.detail)

    def test_unreadable_free_times_blocks(self):
        world = info({})
        del world["everyDaySetting"]
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, world, on("feather", kinds=kind({"hpMax": "961"})))
        self.assertEqual((r.status, sv.conn.sent), (BLOCKED, []))
        self.assertIn("读不到 VIP 等级或今天的免费洗炼次数", r.detail)

    def test_free_times_follow_vip_table(self):
        self.assertEqual([rf.free_left({"vip": {"level": lv}, "everyDaySetting": {"featherResetNum": 1}}, "feather") for lv in (0, 6, 8)], [0, 2, 2])
        self.assertEqual([rf.free_left({"vip": {"level": lv}, "everyDaySetting": {}}, "formation") for lv in (6, 7)], [0, 1])
        self.assertIsNone(rf.free_left({"everyDaySetting": {}}, "feather"))

    def test_low_vip_result_overwrites_directly_so_done_attrs_are_always_locked(self):
        # VIP 6：免费洗炼不能保留原属性（响应没有 needReplace）→ 不发 featherSureRest；“自动锁定”关着也把达标的锁上
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"atk": 10, "dodge": 3}, {"atk": 10, "hpMax": 1000}], choose=False)
        r = run(rf.ID, sv.conn, info({}, level=6, free=3), on("feather", kinds=kind({"hpMax": "961", "atk": "1"}), lock=False))
        self.assertEqual([b["lock"] for b in bodies(sv.conn, "featherVIPRest")], [[], ["atk"], ["atk"]])
        self.assertEqual(sv.conn.bodies("featherSureRest"), [])
        self.assertEqual(r.status, SUBMITTED)
        self.assertIn("生命1000", r.detail)

    def test_low_vip_stops_if_a_locked_attr_was_lost(self):
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"hit": 9, "dodge": 3}, {"atk": 10, "hpMax": 1000}], choose=False)
        r = run(rf.ID, sv.conn, info({}, level=6, free=3), on("feather", kinds=kind({"hpMax": "961", "atk": "1"})))
        self.assertEqual(len(sv.conn.bodies("featherVIPRest")), 2)  # 锁着攻击洗，回来攻击没了：立刻停
        self.assertIn("没保住", r.detail)

    def test_done_with_free_times_left_is_flagged(self):
        sv = Server("feather", [{"hpMax": 1000, "hit": 5}])
        r = run(rf.ID, sv.conn, info({}, free=3), on("feather", kinds=kind({"hpMax": "961"})))
        self.assertEqual(r.profile[rf.IDLE], "羽毛")  # 洗 1 次就达标，还剩 2 次免费：标红提醒
        self.assertIn("今天的免费次数还没用完", r.detail)
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, info({}, feather={"hpMax": 1000}, free=3), on("feather", kinds=kind({"hpMax": "961"})))
        self.assertEqual((r.profile[rf.IDLE], sv.conn.sent), ("羽毛", []))  # 本来就达标
        r = run(rf.ID, Server("feather", []).conn, info({}, feather={"hpMax": 1000}, free=0), on("feather", kinds=kind({"hpMax": "961"})))
        self.assertEqual(r.profile[rf.IDLE], "")  # 免费次数没了：不用提醒

    def test_feather_always_locks_done_attrs(self):
        # 羽毛没有“自动锁定”开关：旧设置里留着 lock=False 也照锁
        sv = Server("feather", [{"atk": 10, "hit": 5}, {"hpMax": 1000, "hit": 5}, {"hpMax": 1000, "atk": 10}])
        run(rf.ID, sv.conn, info({TWIG: 60}), on("feather", kinds=kind({"hpMax": "961", "atk": "1"}), lock=False))
        self.assertEqual([b["lock"] for b in sv.conn.bodies("featherVIPRest")], [[], ["atk"], ["atk"]])
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [True, False, True])

    def test_worse_roll_keeps_old(self):
        sv = Server("feather", [{"hit": 9, "dodge": 9}, {"hpMax": 1900, "hit": 1}])
        run(rf.ID, sv.conn, info({TWIG: 60}), on("feather", kinds=kind({"hpMax": "961"})))
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [False, True])

    def test_nothing_set(self):
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, info({TWIG: 60}), on("feather", kinds={}))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))


class FeatherKindTest(unittest.TestCase):
    """按羽毛种类各用各的条件。"""

    @staticmethod
    def wing(*feathers):
        """翅膀上镶着的几根羽毛 [(实体, 编号, 属性)]。"""
        inlay = {str(i): {"entityId": eid, "id": fid, **attrs} for i, (eid, fid, attrs) in enumerate(feathers)}
        return {**vip(), "players": [{"equipments": {"wing": {"entityId": "w1", "id": WING, "level": 5, "inlay": inlay}}}],
                "bag": {"item": [{"id": TWIG, "count": 999}], "feather": {"x": {"entityId": "bagged", "id": BLUE, "atk": 1}},
                        "wing": {"y": {"entityId": "w2", "id": 150040001, "level": 4, "inlay": {"0": {"entityId": "b1", "id": BLUE, "hit": 7}}},
                                 "z": {"entityId": "w3", "id": 150050001, "level": 1, "inlay": {}}}}}

    def test_each_kind_uses_its_own_filter(self):
        world = self.wing(("f1", FEATHER, {"hit": 30, "dodge": 9}), ("f2", BLUE, {"hit": 30, "dodge": 9}))
        kinds = kind({"hpMax": "961"}) | kind({"atk": "1"}, BLUE)
        sv = Server("feather", [{"atk": 30, "hit": 5}, {"hpMax": 1000, "hit": 5}])
        r = run(rf.ID, sv.conn, world, on("feather", kinds=kinds))  # 选中金鹏，要生命：攻击那次不留
        self.assertEqual([b["entityId"] for b in bodies(sv.conn, "featherVIPRest")], ["f1", "f1"])
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [False, True])
        self.assertEqual(r.status, SUBMITTED)
        sv = Server("feather", [{"atk": 30, "hit": 5}])
        run(rf.ID, sv.conn, world, on("feather", kinds=kinds, pick=pick("1", BLUE)))  # 选中蓝凫，要攻击
        self.assertEqual([b["entityId"] for b in bodies(sv.conn, "featherVIPRest")], ["f2"])
        self.assertEqual([b["replace"] for b in sv.conn.bodies("featherSureRest")], [True])

    def test_picked_kind_without_filter_is_left_alone(self):
        sv = Server("feather", [])
        world = self.wing(("f1", FEATHER, {"hit": 30}), ("f2", BLUE, {"hit": 30}))
        r = run(rf.ID, sv.conn, world, on("feather", kinds=kind({"hpMax": "961"}), pick=pick("1", BLUE)))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))

    def test_only_the_picked_feather_is_washed(self):
        sv = Server("feather", [{"atk": 30}])
        world = self.wing(("f1", BLUE, {"hit": 30}), ("f2", BLUE, {"hit": 30}))  # 两根都是蓝凫、条件一样，选中的是 1 号孔
        r = run(rf.ID, sv.conn, world, on("feather", kinds=kind({"atk": "1"}, BLUE), pick=pick("1", BLUE)))
        self.assertEqual([b["entityId"] for b in bodies(sv.conn, "featherVIPRest")], ["f2"])
        self.assertEqual(r.status, SUBMITTED)

    def test_pick_for_another_kind_in_that_hole_is_ignored(self):
        sv = Server("feather", [])
        world = self.wing(("f1", BLUE, {"hit": 30}))  # 选的时候 0 号孔是金鹏，现在换成了蓝凫
        r = run(rf.ID, sv.conn, world, on("feather", kinds=kind({"atk": "1"}, BLUE)))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))
        self.assertIn("选中的那根羽毛没找到", r.detail)

    def test_nothing_picked_washes_nothing(self):
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, self.wing(("f1", FEATHER, {"hit": 30})), on("feather", kinds=kind({"hpMax": "961"}), pick=None))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))
        self.assertIn("没有选要洗哪一根羽毛", r.detail)

    def test_filter_for_kind_not_on_wing_does_nothing(self):
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, self.wing(("f1", FEATHER, {"hit": 30})), on("feather", kinds=kind({"atk": "1"}, BLUE)))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))  # 条件设在身上那只的蓝凫上，它上面没有蓝凫；背包那只没设条件

    def test_profile_lists_wings_with_their_feathers(self):
        world = self.wing(("f1", FEATHER, {"hit": 1}), ("f2", 160000006, {"mpMax": 2072, "critical": 662, "level": 3}))
        want = {"wings": [
            {"id": WING, "name": "五阶孔雀之翼", "worn": True, "feathers": [
                {"id": FEATHER, "pos": "0", "attrs": {"hit": 1}}, {"id": 160000006, "pos": "1", "attrs": {"mpMax": 2072, "critical": 662}}]},
            {"id": 150040001, "name": "四阶旗之翼", "worn": False, "feathers": [{"id": BLUE, "pos": "0", "attrs": {"hit": 7}}]},
        ]}  # 背包里散着的羽毛、没镶羽毛的翅膀不列
        self.assertEqual(rf.feather_profile(world), want)
        self.assertEqual(run(rf.ID, Fake(), world, {}).profile, want | {"fabaos": [], rf.IDLE: ""})

    def test_only_wings_with_filters_are_washed(self):
        sv = Server("feather", [{"atk": 5}])
        world = self.wing(("f1", BLUE, {"hit": 30}))  # 身上和背包里的翅膀各有一根蓝凫，都在 0 号孔
        opts = {"feather": {"on": True, "times": 9, "wings": {"150040001": {"kinds": kind({"atk": "1"}, BLUE)}}, "pick": pick("0", BLUE, 150040001)}}
        r = run(rf.ID, sv.conn, world, opts)
        self.assertEqual([b["entityId"] for b in bodies(sv.conn, "featherVIPRest")], ["b1"])  # 选中的是背包里那只旗之翼上的
        self.assertIn("四阶旗之翼·蓝凫之羽", r.detail)

    def test_colors_match_the_game(self):
        self.assertEqual([rf.COLORS[rf.color_of(a, v)] for a, v in (("mpMax", 2072), ("critical", 662), ("guardian", 473), ("atk", 161))],
                         ["橙", "红", "黄", "紫"])  # 和游戏截图里的颜色一致
        self.assertEqual(rf.top_band(160000006, "atk"), (6, 403))  # 赤乌：橙色从 403 起
        self.assertEqual(rf.top_band(160000005, "atk"), (5, 265))  # 彤鹤：红色从 265 起
        self.assertEqual(dict(rf.feather_qualities())[str(FEATHER)], 4)

    def test_value_above_kind_max_is_not_washed(self):
        sv = Server("feather", [])
        r = run(rf.ID, sv.conn, self.wing(("f1", FEATHER, {"hit": 30})), on("feather", kinds=kind({"atk": "193"})))  # 金鹏攻击上限 192
        self.assertEqual(sv.conn.sent, [])
        self.assertIn("到不了 攻击 193（上限 192）", r.detail)

    def test_bands_follow_quality_color_table(self):
        self.assertEqual(rf.bands(FEATHER, "atk"), (65, 192, [(2, 65), (3, 111), (4, 180)]))  # 金鹏攻击：蓝 65 起、紫 111、黄 180
        self.assertEqual(rf.bands(160000006, "hpMax"), (1922, 4263, [(4, 1922), (5, 2652), (6, 4029)]))  # 赤乌生命：黄、红、橙
        self.assertEqual(rf.bands(160000001, "reHP"), (1, 1, [(1, 1)]))
        self.assertEqual([rf.color_of("atk", v) for v in (23, 24, 179, 180, 402, 403)], [0, 1, 3, 4, 5, 6])
        self.assertIsNone(rf.bands(FEATHER, "nope"))

    def test_kinds_listed_low_to_high(self):
        self.assertEqual([name for _k, name in rf.feather_kinds()], ["雪鸟之羽", "蓝凫之羽", "紫燕之羽", "金鹏之羽", "彤鹤之羽", "赤乌之羽"])


@mock.patch.object(rf, "UNAVAILABLE", ())  # 保留未开放实现的协议测试；实际入口的关闭行为见下方
class FormationTest(unittest.TestCase):
    def test_only_attrs_the_flag_can_have(self):
        # 攻击这个血魄出不了，不算；生命要 80%（1100）
        sv = Server("formationFlag", [{"hpMax": 900, "reHP": 3, "dodge": 100}, {"hpMax": 1200, "reHP": 3, "dodge": 100}])
        r = run(rf.ID, sv.conn, info({HEART: 1000}), on("formation", attrs={"hpMax": "80", "atk": "90"}))
        self.assertEqual(bodies(sv.conn, "formationFlagRest")[0], {"entityId": "g1", "lock": [], "index": 2})
        self.assertEqual([b["replace"] for b in sv.conn.bodies("formationFlagSureRest")], [True, True])
        self.assertEqual(r.status, SUBMITTED)

    def test_never_locks_everything(self):
        flag = {"hpMax": 1375, "reHP": 8, "dodge": 100}
        sv = Server("formationFlag", [{"hpMax": 1375, "reHP": 8, "dodge": 275}])
        run(rf.ID, sv.conn, info({HEART: 5000}, flag=flag), on("formation", attrs={"hpMax": "100", "reHP": "100", "dodge": "100"}))
        self.assertEqual(sv.conn.bodies("formationFlagRest")[0]["lock"], ["hpMax", "reHP"])

    def test_low_vip_has_no_free_times(self):
        sv = Server("formationFlag", [])
        r = run(rf.ID, sv.conn, {**info({HEART: 9999}), "vip": {"level": 6}, "everyDaySetting": {}}, on("formation", attrs={"hpMax": "80"}))
        self.assertEqual((r.status, sv.conn.sent), (SKIPPED, []))  # VIP 6 精魄每天 0 次免费；魄心再多也不用
        self.assertIn("免费次数用完", r.detail)


class DryTest(unittest.TestCase):
    """LIVE 关着：什么都不发，只报告。"""

    def setUp(self):
        rf.LIVE = False

    def tearDown(self):
        rf.LIVE = True

    def test_nothing_sent_and_reports_plan(self):
        conn = Fake()
        opts = (on("fabao", **GROW) | on("feather", kinds=kind({"hpMax": "961", "hit": "1"}))
                | on("formation", attrs={"hpMax": "80"}))
        r = run(rf.ID, conn, info({GROWTH_STONE: 9, WISH_STONE: 9, TWIG: 60, HEART: 1000}), opts)
        self.assertEqual((r.status, conn.sent), (SKIPPED, []))
        for text in ("没有发出任何洗练请求", "成长40 祝福白虎 要洗", "达标 1/2", "用免费次数（今天还剩 50 次）", "锁定 命中"):
            self.assertIn(text, r.detail)

    def test_done_items_reported_as_done(self):
        conn = Fake()
        r = run(rf.ID, conn, info({GROWTH_STONE: 9}, growth=62), on("fabao", **GROW))
        self.assertEqual(conn.sent, [])
        self.assertIn("成长62 祝福白虎 已达标", r.detail)


class RefreshTest(unittest.TestCase):
    """每次洗之前重新读一次角色状态，按读回来的洗。"""

    def test_uses_fresh_info_not_login_snapshot(self):
        sv, fresh, reads = Server("newMagicWeapon", []), info({GROWTH_STONE: 9}, growth=62), []
        with mock.patch.object(Session, "refresh", lambda s: reads.append(1) or setattr(s, "info", fresh) or fresh):
            r = run(rf.ID, sv.conn, info({GROWTH_STONE: 9}, growth=40), on("fabao", **GROW) | on("formation", attrs={"hpMax": "1"}))
        self.assertEqual((reads, sv.conn.sent), ([1], []))  # 登录时 40、现在已经 62：不洗；几块开着也只读一次
        self.assertIn("成长62 祝福白虎 已达标", r.detail)

    def test_profile_comes_from_fresh_info(self):
        fresh = info({TWIG: 0}, feather={"hpMax": 1500})
        with mock.patch.object(Session, "refresh", lambda s: setattr(s, "info", fresh) or fresh):
            r = run(rf.ID, Fake(), info({TWIG: 0}), on("feather", kinds=kind({"hpMax": "961", "hit": "1"})))
        self.assertEqual(r.profile["wings"][0]["feathers"][0]["attrs"], {"hpMax": 1500})

    def test_refresh_failure_blocks_without_sending(self):
        def fail(s):
            raise p.AllianceError("刷新角色状态失败：断线")
        conn = Fake()
        with mock.patch.object(Session, "refresh", fail):
            r = run(rf.ID, conn, info({GROWTH_STONE: 9}), on("fabao", **GROW))
        self.assertEqual((r.status, conn.sent), (BLOCKED, []))
        self.assertIn("刷新角色状态失败", r.detail)

    def test_all_off_does_not_read(self):
        def fail(s):
            raise AssertionError("不该读")
        with mock.patch.object(Session, "refresh", fail):
            self.assertEqual(run(rf.ID, Fake(), info({}), {}).status, SKIPPED)


class TaskTest(unittest.TestCase):
    def test_missing_fabao_is_not_marked_done_if_feather_washed(self):
        sv = Server("feather", [{"hpMax": 1000, "hit": 30}])
        opts = on("fabao", **GROW, pick={"entity": "missing", "id": FABAO}) | on(
            "feather", kinds=kind({"hpMax": "961"}))

        result = run(rf.ID, sv.conn, info(), opts)

        self.assertEqual(result.status, BLOCKED)
        self.assertIn("选中的那件法宝没找到", result.detail)
        self.assertEqual(len(sv.conn.bodies("featherVIPRest")), 1)

    def test_unavailable_formation_does_not_refresh_or_send(self):
        conn = Fake()
        with mock.patch.object(Session, "refresh", side_effect=AssertionError("阵法未开放，不应读取")):
            r = run(rf.ID, conn, info({HEART: 9999}), on("formation", attrs={"hpMax": "80"}))
        self.assertEqual((r.status, conn.sent), (SKIPPED, []))
        self.assertEqual(r.detail, "阵法：暂未开放")
        self.assertEqual(r.profile[rf.IDLE], "")

    def test_unavailable_formation_does_not_block_fabao(self):
        sv = fabao([g(62)])
        opts = on("fabao", **GROW) | on("formation", attrs={"hpMax": "80"})
        r = run(rf.ID, sv.conn, info({}), opts)
        self.assertEqual(r.status, SUBMITTED)
        self.assertEqual(len(sv.conn.bodies("tryResetAttrFreeVIP")), 1)
        self.assertEqual(sv.conn.bodies("formationFlagRest"), [])
        self.assertEqual(sv.conn.bodies("formationFlagSureRest"), [])
        self.assertIn("阵法：暂未开放", r.detail)

    def test_all_off(self):
        conn = Fake()
        r = run(rf.ID, conn, info({}), {"fabao": {"on": False, "times": 5, "growth": "60"}, "feather": {"on": True, "times": 0}})
        self.assertEqual((r.status, conn.sent), (SKIPPED, []))

    def test_summary(self):
        self.assertEqual(rf.summary({}), "未设置")
        self.assertEqual(rf.summary(on("fabao") | on("formation")), "法宝")
        self.assertEqual(rf.summary(on("formation")), "未设置")


if __name__ == "__main__":
    unittest.main()
