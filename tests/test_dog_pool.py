"""狗卡轮转：送卡先后（next_gift）和整轮（play）用假的服务器离线跑。"""

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from zmxy import protocol as p
from zmxy.core import UncertainOutcome
from zmxy.tasks.events import dog_pool as dp
from zmxy.tasks.events.double11_dog import R_DATA, R_DRAW

KINDS = [{"id": i, "consume_subType": f"dog_{i}", "name": f"狗{i}"} for i in range(1, 6)]


class World:
    def __init__(self, refuse=False):
        self.codes = {}  # 码 → (送的账号, 种类)
        self.spent = {}  # 兑换掉的码：游戏里“我的兑换码”照样列着（2026-11-15 真机）
        self.refuse = refuse  # 服务器拒绝兑换（别的原因）


class FakeAcc:
    """一个号在服务器上的状态：抽卡按 draws 的顺序出；每天第一次赠送加 present 次。"""

    def __init__(self, world, user, cards=None, act=(), draws=(), used=0, extra=0, present=1, base=1, lose_code=False):
        self.world, self.user = world, user
        self.cards = {k["consume_subType"]: 0 for k in KINDS}
        self.cards.update(cards or {})
        self.act = set(act)
        self.draws, self.used, self.extra, self.present, self.base = list(draws), used, extra, present, base
        self.gifted, self.lose_code, self.reward = extra > 0, lose_code, 0
        self.routes = []

    def refresh(self):
        return {"everyDaySetting": {"double11LotteryCnt": self.used, "addDouble11LotteryCnt": self.extra}}

    def request(self, route, body):
        self.routes.append(route)
        kind = f"dog_{body['id']}" if "id" in body else None
        if route == R_DATA:
            log = [[0, int(k[4:]), c] for c, (u, k) in {**self.world.codes, **self.world.spent}.items() if u == self.user]  # 兑换掉的也在
            return {"data": {**self.cards, "dogPos": {str(int(k[4:])): True for k in self.act}, "getPoolRewardTime": self.reward,
                             "codeLog": log}}
        if route == R_DRAW:
            assert self.used < self.base + self.extra, "次数用完还在抽"
            got = self.draws.pop(0) if self.draws else "dog_1"
            self.used += 1
            self.cards[got] += 1
            return {"give": [{"type": "coin", "subType": got, "count": 1}]}
        if route == dp.R_ACTIVE:
            assert self.cards[kind] > 0 and kind not in self.act
            self.cards[kind] -= 1
            self.act.add(kind)
            return {}
        if route == dp.R_TO_CODE:
            assert kind in self.act and self.cards[kind] > 0, "没激活或没有卡还在送"
            self.cards[kind] -= 1
            if not self.gifted:
                self.gifted, self.extra = True, self.extra + self.present
            code = f"C{len(self.world.codes) + 1}"
            self.world.codes[code] = (self.user, kind)
            return {} if self.lose_code else {"cdkey": code}
        if route == dp.R_TO_DOG:
            owner, got = self.world.codes.get(body["cdkey"], (None, None))
            if owner is None:
                raise p.AllianceError("CDKEY错误或者已经使用过")
            if self.world.refuse:
                raise p.AllianceError("服务器繁忙")
            self.world.spent[body["cdkey"]] = self.world.codes.pop(body["cdkey"])
            assert owner != self.user, "同一个账号自己兑换"
            self.cards[got] += 1
            return {}
        if route == dp.R_MONEY:
            self.reward = 1
            return {}
        raise AssertionError(route)


def member(acc, collect=False, slot=1, hub=False):
    return dp.Member(acc, acc.user, slot, acc.user, collect or hub, KINDS, acc.present, acc.base, hub)


class Stub:
    """next_gift 只看状态的假号。"""

    def __init__(self, user, cards=None, act=(), gifted=False, collect=False, hub=False):
        self.user, self.collect, self.act, self.hub = user, collect, set(act), hub
        self.cards = {k["consume_subType"]: 0 for k in KINDS}
        self.cards.update(cards or {})
        self.ids = {k["consume_subType"]: k["id"] for k in KINDS}
        self._gifted = gifted

    gifted = property(lambda self: self._gifted)
    spare, spares, missing = dp.Member.spare, dp.Member.spares, dp.Member.missing


class NextGiftTests(unittest.TestCase):
    def test_collector_first(self):
        a = Stub("a", {"dog_2": 1}, {"dog_2"}, gifted=True)
        b = Stub("b", {"dog_3": 1}, {"dog_3"})
        c = Stub("c", act={"dog_1"}, collect=True)
        self.assertEqual(dp.next_gift([a, b, c]), (a, c, "dog_2"))
        a2 = Stub("a", {"dog_2": 1}, {"dog_2"}, gifted=True)
        b2 = Stub("b", {"dog_2": 1}, {"dog_2"})
        self.assertEqual(dp.next_gift([a2, b2, c]), (b2, a2, "dog_2"))  # 能接力：先接力（b2 拿到加抽、卡还在），最后才送集卡号

    def test_collector_after_relay(self):
        """集卡号缺的狗2：先在激活过的号之间接力，不给别的号激活；没人要接了才送集卡号。"""
        a = Stub("a", {"dog_2": 1}, {"dog_2"})
        b = Stub("b", act={"dog_2"})
        x = Stub("x", act={"dog_1"}, gifted=True)
        c = Stub("c", act={"dog_1"}, gifted=True, collect=True)
        self.assertEqual(dp.next_gift([a, b, x, c]), (a, b, "dog_2"))
        a._gifted, b.cards["dog_2"], a.cards["dog_2"], b._gifted = True, 1, 0, True
        self.assertEqual(dp.next_gift([a, b, x, c]), (b, c, "dog_2"))

    def test_revive_idle_member_with_kind_it_has(self):
        a = Stub("a", {"dog_2": 2}, {"dog_2"}, gifted=True)
        b = Stub("b", act={"dog_2"})  # 没有可送的卡、今天没送过
        self.assertEqual(dp.next_gift([a, b]), (a, b, "dog_2"))

    def test_relay_uses_kind_others_can_pass_on(self):
        """救活时选别的没送过的号也激活过的种类（常见卡），即使手里稀有卡更多。"""
        a = Stub("a", {"dog_2": 1, "dog_5": 3}, {"dog_2", "dog_5"}, gifted=True)
        b = Stub("b", act={"dog_2", "dog_5"})
        c = Stub("c", act={"dog_2"})
        d = Stub("d", act={"dog_2"})
        self.assertEqual(dp.next_gift([a, b, c, d]), (a, b, "dog_2"))

    def test_keep_last_relay_card(self):
        """最后一张能接力的狗2：不送去激活，交给激活过的号（卡还在）；有两张时照常送一张去激活。"""
        a = Stub("a", {"dog_2": 1}, {"dog_2"})
        b = Stub("b", act={"dog_2", "dog_3"}, gifted=True)
        c = Stub("c", act={"dog_4"}, gifted=True)
        self.assertEqual(dp.next_gift([a, b, c]), (a, c, "dog_2"))  # 最后一天：送去激活
        self.assertEqual(dp.next_gift([a, b, c], keep=True), (a, b, "dog_2"))
        a.cards["dog_2"] = 2
        self.assertEqual(dp.next_gift([a, b, c], keep=True), (a, c, "dog_2"))

    def test_keep_one_relay_kind_per_member(self):
        """b 只激活过狗3：狗2 有两张也只留够 a、c 的，狗3 最后一张要留给 b 接。"""
        a = Stub("a", {"dog_2": 2, "dog_3": 1}, {"dog_2", "dog_3"})
        b = Stub("b", act={"dog_3"}, gifted=True)
        c = Stub("c", act={"dog_2"}, gifted=True)
        d = Stub("d", act={"dog_1"}, gifted=True)
        g, r, kind = dp.next_gift([a, b, c, d], keep=True)
        self.assertEqual((g, kind), (a, "dog_2"))  # 狗2 多一张，送去激活；狗3 不动
        self.assertIn(r, (b, d))
        a.cards["dog_2"] = 1
        self.assertNotEqual(dp.next_gift([a, b, c, d], keep=True)[1], d)  # 狗2、狗3 都只剩一张：不送去激活

    VALUES = {"dog_1": 2, "dog_2": 3, "dog_3": 6, "dog_4": 7, "dog_5": 20}  # 5 种：多抽一次的期望价值 = 5，上限 5 × 1.3 = 6.5

    def test_value_relay_costs_nothing(self):
        """按价值：送给激活过的号（卡还在）比送去激活（花掉价值）好，哪怕是便宜卡。"""
        a = Stub("a", {"dog_2": 1}, {"dog_2"})
        b = Stub("b", act={"dog_1"}, gifted=True)
        c = Stub("c", act={"dog_2"}, gifted=True)
        self.assertEqual(dp.next_gift([a, b, c], values=self.VALUES), (a, c, "dog_2"))
        self.assertEqual(dp.next_gift([a, b, c]), (a, b, "dog_2"))  # 不按价值：先送没激活的

    def test_value_limit_for_activation(self):
        """没人能接力时：送去激活的成本 = 价值 ×（1 − 1/价值）^收卡号剩余次数，低于送卡号激活过的种类数才送；集卡号照送。"""
        b = Stub("b", act={"dog_1"}, gifted=True)
        cheap = Stub("a", {"dog_3": 1}, {"dog_3", "dog_1"})  # 好处 2
        self.assertIsNone(dp.next_gift([cheap, b], values=self.VALUES, days=5))  # 6 × (5/6)^5 ≈ 2.4，比好处 2 大
        self.assertEqual(dp.next_gift([cheap, b], values=self.VALUES, days=10), (cheap, b, "dog_3"))  # 6 × (5/6)^10 ≈ 0.97
        dear = Stub("a", {"dog_5": 1}, {"dog_5", "dog_1"})
        self.assertIsNone(dp.next_gift([dear, b], values=self.VALUES, days=10))  # 20 × 0.95^10 ≈ 12 > 2
        b.collect = True
        self.assertIsNotNone(dp.next_gift([dear, b], values=self.VALUES, days=5))

    def test_activation_cost(self):
        self.assertAlmostEqual(dp.activation_cost("k", {"k": 40}, 0), 40)
        self.assertAlmostEqual(dp.activation_cost("k", {"k": 40}, 28), 40 * 0.975 ** 28)
        self.assertLess(dp.activation_cost("k", {"k": 5}, 28), 0.01)

    def test_card_values_from_table(self):
        with mock.patch.object(dp.state, "row", return_value={"subType": "dog_1;dog_2", "probability": "3;1"}):
            self.assertEqual(dp.card_values(), {"dog_1": 4 / 3, "dog_2": 4.0})
        with mock.patch.object(dp.state, "row", return_value={"subType": "dog_1;dog_2", "probability": "3"}):
            self.assertEqual(dp.card_values(), {})

    def test_rare_spare_not_kept(self):
        """只有自己激活过的种类不算接力卡，照常送去激活。"""
        a = Stub("a", {"dog_5": 1}, {"dog_5"})
        c = Stub("c", act={"dog_4"}, gifted=True)
        self.assertEqual(dp.next_gift([a, c], keep=True), (a, c, "dog_5"))

    def test_own_spare_goes_to_one_lacking_it(self):
        a = Stub("a", {"dog_2": 1}, {"dog_2"})
        b = Stub("b", {"dog_3": 1}, {"dog_2", "dog_3"}, gifted=True)
        c = Stub("c", act={"dog_4"}, gifted=True)
        self.assertEqual(dp.next_gift([a, b, c]), (a, c, "dog_2"))

    def test_hub_collects_leftovers_it_has_activated(self):
        """都送过了：多余的卡汇总到集卡号，它没激活过的种类留在原号。"""
        a = Stub("a", {"dog_2": 2, "dog_5": 1}, {"dog_2", "dog_5"}, gifted=True)
        h = Stub("h", act={"dog_2"}, gifted=True, hub=True)
        self.assertEqual(dp.next_gift([a, h]), (a, h, "dog_2"))
        a.cards["dog_2"] = 0
        self.assertIsNone(dp.next_gift([a, h]))  # 狗5 集卡号没激活：不送

    def test_never_same_account_and_stops(self):
        a = Stub("a", {"dog_2": 3}, {"dog_2"})
        self.assertIsNone(dp.next_gift([a]))
        done = Stub("b", act={"dog_2"}, gifted=True)
        a2 = Stub("a", act={"dog_2"}, gifted=True)
        self.assertIsNone(dp.next_gift([a2, done]))

    def test_unactivated_cards_are_not_given(self):
        a = Stub("a", {"dog_2": 3})  # 有卡但没激活：不能送
        b = Stub("b", act={"dog_1"}, collect=True)
        self.assertIsNone(dp.next_gift([a, b]))


class PlayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "dog_pool.json"
        self.codes = Path(self.dir.name) / "狗卡" / "兑换码记录.txt"
        patch = mock.patch.object(dp, "code_log", return_value=self.codes)  # 不往项目里的 狗卡/ 写
        patch.start()
        self.addCleanup(patch.stop)
        self.lines = []

    def tearDown(self):
        self.dir.cleanup()

    def test_round_everyone_gifts_and_collector_gains(self):
        w = World()
        a = FakeAcc(w, "a", {"dog_2": 1}, {"dog_2"}, draws=["dog_3", "dog_4"])
        b = FakeAcc(w, "b", act={"dog_5"}, draws=["dog_5", "dog_2"])
        c = FakeAcc(w, "c", act={"dog_1"}, draws=["dog_1", "dog_1"])
        ms = [member(a), member(b), member(c, collect=True)]
        dp.play(ms, self.lines.append, self.path)
        self.assertTrue(all(x.gifted for x in (a, b, c)), self.lines)
        self.assertEqual(c.act, {"dog_1", "dog_2", "dog_5"}, self.lines)  # 狗3、狗4 a 只有一张，自己激活了，没有多的
        self.assertEqual(dp.load_record(self.path).get("codes"), [])
        self.assertFalse(w.codes)
        self.assertEqual([x.used for x in (a, b, c)], [x.base + x.extra for x in (a, b, c)])
        self.assertTrue(all(m.gave >= 1 for m in ms))
        rows = [r.split(dp.TAB) for r in self.codes.read_text(encoding="utf-8").splitlines()[1:]]
        self.assertEqual([r[1] for r in rows].count("生成"), sum(m.gave for m in ms))
        self.assertEqual([r[1] for r in rows].count("已兑换"), sum(m.gave for m in ms))
        self.assertTrue(all(len(r) == 5 and r[4].startswith("C") for r in rows))
        self.assertEqual(self.codes.read_text(encoding="utf-8").splitlines()[0].split(dp.TAB)[2], "卡")
        self.assertTrue(all(r[2].startswith("狗") for r in rows))  # 每个码对应的卡名

    def test_one_card_relayed_starts_everyone(self):
        """只有一张多余的狗2、大家都激活过狗2：接力转赠，每个号都拿到当天首次赠送。"""
        w = World()
        accs = [FakeAcc(w, u, {"dog_2": 1} if u == "a" else {}, {"dog_2"}, used=1, draws=["dog_1"]) for u in "abcde"]
        dp.play([member(x) for x in accs], self.lines.append, self.path)
        self.assertTrue(all(x.gifted for x in accs), self.lines)
        self.assertEqual(sum(" 送 " in x for x in self.lines), 5)

    def test_refused_redeem_goes_to_failed(self):
        """服务器拒绝兑换：码记进 failed，这一轮停下（不再送、也不拿去试别的号）。"""
        w = World(refuse=True)
        a = FakeAcc(w, "a", {"dog_2": 2}, {"dog_2"}, used=1)
        b = FakeAcc(w, "b", act={"dog_2"}, used=1)
        with self.assertRaisesRegex(dp.Bad, "兑换失败"):
            dp.play([member(a), member(b)], self.lines.append, self.path)
        rec = dp.load_record(self.path)
        self.assertEqual(len(rec.get("failed", [])), 1, self.lines)  # 停下了，没再送第二张
        self.assertEqual(rec.get("codes"), [])
        self.assertEqual(a.cards["dog_2"], 1)

    def test_takers_for_absent_target(self):
        """原定收卡号没登录：先给集卡号，再给激活过这种的号；送卡号自己、没激活的普通号都不给。"""
        a = Stub("a", act={"dog_3"})
        b = Stub("b", act={"dog_3"})
        c = Stub("c", act={"dog_1"})
        h = Stub("h", act={"dog_1"}, collect=True)
        self.assertEqual(dp.takers_for("dog_3", "a", [a, b, c, h]), [h, b])
        self.assertEqual(dp.takers_for("dog_3", "a", [a, c]), [])

    def test_own_code_from_game_used_first(self):
        """集卡号在游戏里手动点出来的码：先交给激活过这种的号兑换，之后照常汇总回集卡号；不会重复收。"""
        w = World()
        w.codes["MINE"] = ("h", "dog_2")
        h = FakeAcc(w, "h", act={"dog_2"}, used=2, extra=1)
        b = FakeAcc(w, "b", act={"dog_2"}, used=2, extra=1)
        dp.play([member(h, hub=True), member(b)], self.lines.append, self.path)
        self.assertNotIn("MINE", w.codes, self.lines)
        self.assertTrue(any("先拿来用" in x for x in self.lines), self.lines)
        self.assertEqual((h.cards["dog_2"], b.cards["dog_2"]), (1, 0), self.lines)  # b 收下后汇总回 h
        self.assertEqual(dp.load_record(self.path).get("codes"), [])
        events = [r.split(dp.TAB)[1] for r in self.codes.read_text(encoding="utf-8").splitlines()[1:]]
        self.assertEqual(events.count("收下游戏里的码"), 1)

    def test_own_code_known_or_failed_not_taken_again(self):
        w = World()
        w.codes["OLD"] = ("a", "dog_2")
        dp.update_record(lambda r: r.setdefault("failed", []).append({"id": "x", "code": "OLD"}), self.path)
        a = FakeAcc(w, "a", act={"dog_2"}, used=2, extra=1)
        b = FakeAcc(w, "b", act={"dog_2"}, used=2, extra=1)
        dp.play([member(a), member(b)], self.lines.append, self.path)
        self.assertIn("OLD", w.codes)

    def test_redeemed_codes_still_listed_in_game_are_not_taken_again(self):
        """2026-11-15 真机：前一轮兑换掉的码“我的兑换码”里还在，第二轮不能再收（原来收了、兑换失败、整轮停下）。"""
        w = World()
        a = FakeAcc(w, "a", {"dog_2": 1}, {"dog_2"}, used=1)
        b = FakeAcc(w, "b", act={"dog_2"}, used=1)
        dp.play([member(a), member(b)], self.lines.append, self.path)
        self.assertTrue(w.spent, self.lines)
        self.assertEqual(dp.load_record(self.path).get("used"), list(w.spent))
        self.codes.unlink()  # 打包版没有留底文件：只靠账本的 used
        self.lines.clear()
        dp.play([member(a), member(b)], self.lines.append, self.path)
        self.assertFalse(any("先拿来用" in x or "兑换不了" in x for x in self.lines), self.lines)
        self.assertEqual(dp.load_record(self.path).get("failed", []), [])

    def test_stale_own_code_refused_does_not_stop_the_round(self):
        """账本里没记过的旧码（修之前兑换掉的）被收进来、兑换被拒：记 failed，接着抽卡送卡。"""
        w = World()
        w.spent["OLD"] = ("a", "dog_2")
        a = FakeAcc(w, "a", {"dog_2": 1}, {"dog_2"}, used=0, draws=["dog_1"])
        b = FakeAcc(w, "b", act={"dog_2"}, used=1)
        dp.play([member(a), member(b)], self.lines.append, self.path)
        rec = dp.load_record(self.path)
        self.assertEqual([e["code"] for e in rec.get("failed", [])], ["OLD"])
        self.assertEqual(a.used, a.base + a.extra, self.lines)  # 照常抽完、送完
        self.assertTrue(a.gifted, self.lines)

    def test_stale_own_code_already_in_ledger_is_dropped(self):
        """上一轮误收进账本、其实兑换过的码（留底文件里有“已兑换”）：拿掉，不再去兑换。"""
        w = World()
        w.spent["OLD"] = ("a", "dog_2")
        dp.update_record(lambda r: r.setdefault("codes", []).append(
            {"id": "x", "state": "made", "code": "OLD", "from": "a#1", "to": None, "kind": "dog_2", "own": True}), self.path)
        self.codes.parent.mkdir(parents=True, exist_ok=True)
        self.codes.write_text(dp.TAB.join(["t", "已兑换", "狗2", "a#1 → b#1", "OLD"]) + chr(10), encoding="utf-8")
        a = FakeAcc(w, "a", act={"dog_2"}, used=1)
        b = FakeAcc(w, "b", act={"dog_2"}, used=1)
        dp.play([member(a), member(b)], self.lines.append, self.path)
        rec = dp.load_record(self.path)
        self.assertEqual(rec.get("codes"), [])
        self.assertEqual(rec.get("failed", []), [])
        self.assertNotIn(dp.R_TO_DOG, a.routes + b.routes)

    def test_hub_gets_extra_draw_card(self):
        """a 首次赠送后补抽到的狗3，前面几步没得做，汇总给 h；第 1 步先把 h 缺的狗4 送过去。"""
        w = World()
        a = FakeAcc(w, "a", {"dog_2": 1, "dog_4": 1}, {"dog_2", "dog_3", "dog_4"}, used=1, draws=["dog_3"])
        h = FakeAcc(w, "h", act={"dog_2", "dog_3"}, used=2, extra=1)
        dp.play([member(a), member(h, hub=True)], self.lines.append, self.path)
        self.assertIn("dog_4", h.act, self.lines)
        self.assertEqual(sum(a.cards.values()), 0, self.lines)
        self.assertEqual((h.cards["dog_2"], h.cards["dog_3"]), (1, 1), self.lines)

    def entry(self, code, state="made", to="b#1", kind="dog_3", **kw):
        e = {"id": code, "state": state, "code": code, "from": "a#1", "to": to, "kind": kind, **kw}
        dp.update_record(lambda r: r.setdefault("codes", []).append(e), self.path)

    def zero(self):
        return {k["consume_subType"]: 0 for k in KINDS}

    def test_leftover_code_redeemed_first(self):
        w = World()
        w.codes["OLD"] = ("a", "dog_3")
        self.entry("OLD")
        self.entry("BAD", kind="dog_4")
        a = FakeAcc(w, "a", extra=1, used=2)
        b = FakeAcc(w, "b", extra=1, used=2)
        with self.assertRaises(dp.Bad) as got:  # 无效码兑换被拒：记进 failed，这一轮停下
            dp.play([member(a), member(b)], self.lines.append, self.path)
        self.assertIn("dog_3", b.act)  # 前一张照常兑换后激活
        rec = dp.load_record(self.path)
        self.assertEqual(rec.get("codes"), [])
        self.assertEqual([f["code"] for f in rec.get("failed", [])], ["BAD"])
        self.assertNotIn("BAD", str(got.exception))  # 结果里不写明码
        events = [r.split(dp.TAB)[1] for r in self.codes.read_text(encoding="utf-8").splitlines()[1:]]
        self.assertEqual(events, ["已兑换", "兑换失败"])

    def test_redeeming_confirmed_by_counts_not_resent(self):
        """上次兑换发出去后断了：比对卡数，已经到账的直接认，不再发兑换请求。"""
        w = World()
        b = FakeAcc(w, "b", {"dog_3": 1}, extra=1, used=2)
        self.entry("X", state="redeeming", before=self.zero())
        dp.play([member(FakeAcc(w, "a", extra=1, used=2)), member(b)], self.lines.append, self.path)
        self.assertNotIn(dp.R_TO_DOG, b.routes)
        self.assertEqual(dp.load_record(self.path)["codes"], [])
        self.assertIn("dog_3", b.act)

    def test_redeeming_unchanged_is_sent_once(self):
        """卡数和兑换前一样：请求没到服务器，照常兑换一次。"""
        w = World()
        w.codes["Z"] = ("a", "dog_3")
        b = FakeAcc(w, "b", extra=1, used=2)
        self.entry("Z", state="redeeming", before=self.zero())
        dp.play([member(FakeAcc(w, "a", extra=1, used=2)), member(b)], self.lines.append, self.path)
        self.assertEqual(b.routes.count(dp.R_TO_DOG), 1)
        self.assertIn("dog_3", b.act)

    def test_redeeming_unclear_goes_to_unknown(self):
        w = World()
        w.codes["Y"] = ("a", "dog_3")
        b = FakeAcc(w, "b", {"dog_2": 1}, extra=1, used=2)  # 卡数和兑换前、兑换后都对不上
        self.entry("Y", state="redeeming", before=self.zero())
        notes = dp.play([member(FakeAcc(w, "a", extra=1, used=2)), member(b)], self.lines.append, self.path)
        self.assertNotIn(dp.R_TO_DOG, b.routes)
        rec = dp.load_record(self.path)
        self.assertEqual([e["code"] for e in rec["unknown"]], ["Y"])
        self.assertTrue(any("结果不明" in n for n in notes))

    def test_sending_goes_to_unknown(self):
        w = World()
        dp.update_record(lambda r: r.setdefault("codes", []).append(
            {"id": "s1", "state": "sending", "from": "a#1", "to": "b#1", "kind": "dog_2"}), self.path)
        notes = dp.play([member(FakeAcc(w, "a", extra=1, used=2)), member(FakeAcc(w, "b", extra=1, used=2))],
                        self.lines.append, self.path)
        self.assertEqual(len(dp.load_record(self.path)["unknown"]), 1)
        self.assertTrue(any("我的兑换码" in n for n in notes))

    def test_code_saved_before_check(self):
        """赠送拿到码以后核对断了：码已经在账本里（made）。"""
        w = World()
        a = FakeAcc(w, "a", {"dog_2": 1}, {"dog_2"}, used=1)
        b = FakeAcc(w, "b", act={"dog_1"}, used=2, extra=1)
        real = a.request
        calls = []

        def flaky(route, body):
            calls.append(route)
            if route == R_DATA and dp.R_TO_CODE in calls:
                raise TimeoutError("断线")
            return real(route, body)
        a.request = flaky
        with self.assertRaises(TimeoutError):
            dp.play([member(a), member(b)], self.lines.append, self.path)
        rec = dp.load_record(self.path)
        self.assertEqual([(e["state"], e["code"]) for e in rec["codes"]], [("made", "C1")])

    def test_broken_record_not_overwritten(self):
        self.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(dp.Bad):
            dp.update_record(lambda r: r.__setitem__("x", 1), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{bad")
        self.assertEqual(dp.peek_record(self.path)[0], {})

    def test_halt_stops_before_next_write(self):
        w = World()
        a = FakeAcc(w, "a", {"dog_2": 1}, {"dog_2"}, used=0, draws=["dog_3"])
        b = FakeAcc(w, "b", act={"dog_2"}, used=1)
        ticks = []

        def halt():
            ticks.append(1)
            if len(ticks) > 1:
                raise dp.Stopped("用户停止")
        with self.assertRaises(dp.Stopped):
            dp.play([member(a), member(b)], self.lines.append, self.path, halt=halt)
        self.assertEqual(a.routes.count(R_DRAW), 1)
        self.assertNotIn(dp.R_ACTIVE, a.routes)

    def test_draw_counter_must_move(self):
        w = World()
        a = FakeAcc(w, "a", used=0)
        a.refresh = lambda: {"everyDaySetting": {"double11LotteryCnt": 0, "addDouble11LotteryCnt": 0}}  # 抽了次数不变
        with self.assertRaises(dp.Bad):
            dp.play([member(a)], self.lines.append, self.path)

    def test_grew_by_one_checks_every_kind(self):
        before = {"dog_1": 1, "dog_2": 2, "dog_3": 0}
        self.assertEqual(dp.grew_by_one(before, {"dog_1": 1, "dog_2": 2, "dog_3": 1}), "dog_3")
        self.assertIsNone(dp.grew_by_one(before, {"dog_1": 3, "dog_2": 0, "dog_3": 1}))  # 别的种类 +2 / -2

    def test_max_gifts_not_complete(self):
        w = World()
        h = FakeAcc(w, "h", act={"dog_2"}, used=2, extra=1)
        a = FakeAcc(w, "a", {"dog_2": 3}, {"dog_2"}, used=2, extra=1)
        with mock.patch.object(dp, "MAX_GIFTS", 2), self.assertRaises(dp.Bad):
            dp.play([member(a), member(h, hub=True)], self.lines.append, self.path)
        self.assertEqual(h.cards["dog_2"], 2)

    def test_last_day_limit_is_givers_kinds(self):
        """最后一天：收卡号再也抽不到，成本是整张卡（3）；送卡号只激活过 1 种，好处只有 1，不送。还剩很多天时成本很低，送。"""
        values = {"dog_1": 2, "dog_2": 3, "dog_3": 4, "dog_4": 6, "dog_5": 20}
        a = Stub("a", {"dog_2": 1}, {"dog_2"})
        b = Stub("b", act={"dog_1"}, gifted=True)
        self.assertEqual(dp.next_gift([a, b], values=values, days=20), (a, b, "dog_2"))
        self.assertIsNone(dp.next_gift([a, b], values=values, days=0))

    def test_missing_code_stops(self):
        w = World()
        a = FakeAcc(w, "a", {"dog_2": 1}, {"dog_2"}, used=1, lose_code=True)
        b = FakeAcc(w, "b", used=1)
        with self.assertRaises(UncertainOutcome):
            dp.play([member(a), member(b)], self.lines.append, self.path)
        self.assertEqual(a.routes.count(dp.R_TO_CODE), 1)  # 不重发
        self.assertFalse(self.codes.exists())  # 没拿到码就没有可记的

    def test_claim_reward_once(self):
        w = World()
        a = FakeAcc(w, "a", act={"dog_1"})
        b = FakeAcc(w, "b")  # 没激活过：不领
        notes = dp.claim([member(a), member(b)], self.lines.append)
        self.assertEqual((a.reward, b.reward), (1, 0))
        self.assertEqual(len(notes), 1)
        self.assertEqual(dp.claim([member(a)], self.lines.append), [])


class FakeCtx:
    """run_round 用的假上下文：两个号，各自一个 FakeAcc 会话。"""

    def __init__(self, world, now, uids=(1, 2)):
        self.accs = {"a": FakeAcc(world, "a", used=0), "b": FakeAcc(world, "b", used=0)}
        for acc, uid in zip(self.accs.values(), uids):
            acc.info = {"uid": uid}
        self.session = self.accs["a"]
        self.config = type("C", (), {"username": "a", "record_index": 0})()
        self.clock, self.stop_now, self.lines = now, False, []
        self.log = self.lines.append

    @property
    def now(self):
        return self.clock

    stopped = property(lambda self: self.stop_now)
    winding = property(lambda self: False)

    def open_peer(self, user, slot):
        from contextlib import nullcontext
        return nullcontext(self.accs[user])


class RunRoundTests(unittest.TestCase):
    POOL = {"members": [{"user": "a", "slot": 1}, {"user": "b", "slot": 1}]}

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "dog_pool.json"
        from datetime import datetime
        from zmxy import state
        self.now = datetime(2026, 11, 12, 10, tzinfo=state._CN).timestamp()
        cfg = type("Cfg", (), {"keys": tuple(k["consume_subType"] for k in KINDS), "base": 1, "start": 1})()
        for target, kw in ((dp, {"record_path": mock.DEFAULT, "code_log": mock.DEFAULT, "card_values": mock.DEFAULT, "days_left": mock.DEFAULT}),):
            patch = mock.patch.multiple(target, **kw)
            mocks = patch.start()
            self.addCleanup(patch.stop)
        mocks["record_path"].return_value = self.path
        mocks["code_log"].return_value = Path(d.name) / "c.txt"
        mocks["card_values"].return_value = {}
        mocks["days_left"].return_value = 5
        from zmxy.tasks.events import double11_dog as d11
        for name, value in (("config", lambda: cfg), ("opened", lambda now: True), ("reward_open", lambda now: False)):
            patch = mock.patch.object(d11, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(dp.state, "row", return_value={"dailyPresentDrawNum": 1})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(dp.state, "table_rows", return_value=KINDS)
        patch.start()
        self.addCleanup(patch.stop)

    def test_once_a_day_unless_manual(self):
        ctx = FakeCtx(World(), self.now)
        self.assertEqual(dp.run_round(ctx, self.POOL, {}, reward=False).status, "submitted")
        self.assertEqual(dp.load_record(self.path)["done"], "2026-11-12")
        self.assertEqual(dp.run_round(ctx, self.POOL, {}, reward=False).status, "skipped")
        self.assertEqual(dp.run_round(ctx, self.POOL, {}, reward=False, manual=True).status, "submitted")

    def test_crossing_midnight_stops_without_done(self):
        ctx = FakeCtx(World(), self.now)
        real = ctx.accs["a"].request

        def later(route, body):
            ctx.clock = self.now + 86400  # 第一个请求以后就到了第二天
            return real(route, body)
        ctx.accs["a"].request = later
        result = dp.run_round(ctx, self.POOL, {}, reward=False)
        self.assertEqual(result.status, "skipped", result.detail)
        self.assertIn("零点", result.detail)
        self.assertNotIn("done", dp.load_record(self.path))
        self.assertNotIn(R_DRAW, ctx.accs["a"].routes + ctx.accs["b"].routes)

    def test_same_uid_blocked(self):
        ctx = FakeCtx(World(), self.now, uids=(7, 7))
        result = dp.run_round(ctx, self.POOL, {}, reward=False)
        self.assertEqual(result.status, "blocked")
        self.assertIn("同一个账号", result.detail)

    def test_broken_record_blocks(self):
        self.path.write_text("{bad", encoding="utf-8")
        result = dp.run_round(FakeCtx(World(), self.now), self.POOL, {}, reward=False)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{bad")


class CodeLogTests(unittest.TestCase):
    def test_packaged_writes_nothing(self):
        with mock.patch("zmxy.tablesync.frozen", return_value=True):
            self.assertIsNone(dp.code_log())
        with mock.patch("zmxy.tablesync.frozen", return_value=False):
            self.assertEqual(dp.code_log().parent.name, "狗卡")


class MembersTests(unittest.TestCase):
    def test_one_slot_per_account_and_bad_rows_dropped(self):
        pool = {"members": [{"user": "a", "slot": 2, "hub": True}, {"user": "a", "slot": 3},
                            {"user": "b", "slot": 9}, {"user": "c", "slot": True}, "x", {"user": "d", "slot": 1, "hub": True}]}
        self.assertEqual(dp.members_of(pool), [{"user": "a", "slot": 2, "hub": True}, {"user": "d", "slot": 1, "hub": False}])  # 集卡号只留第一个
        self.assertEqual(dp.members_of(None), [])


if __name__ == "__main__":
    unittest.main()
