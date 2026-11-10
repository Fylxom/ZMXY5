import json
import shutil
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import BOSS_LOG
from tests.support import Fake, run
from zmxy import activity
from zmxy import protocol as p
from zmxy.core import Session, UncertainOutcome
from zmxy.tasks import boss_fight, boss_rules
from zmxy.tasks import boss_raid as b

HP = 2e5  # 世界 Boss 的血量：我的战力 1000，在打的时候每秒 310–440，一分钟打掉一成左右
PEAK = max(v for _, v in boss_rules.CURVE)  # 一条命里最猛的那一阵是平均的几倍
BEAT = boss_rules.beat  # 真人的节拍（单独测）
HARD = boss_rules.HARD[960101]              # 测试里打的是金之祖巫：同战力在它身上打得出来的是基准的几倍
# 一次上报最多算几秒的伤害：setUp 换成的平节拍每 STEP 秒一格、一格 STEP 秒的量，上报窗口最长约 3 秒，正好能盖住相隔 STEP 秒的两格
MOST = 2 * boss_rules.boss_beat.STEP


class Raid(Fake):
    """攻击应答后塞进推送；第 n 次攻击后（联盟 Boss）/ 到 finish_at 那一刻（世界 Boss）塞结算。世界 Boss 的累计伤害 = 报上去的伤害之和。"""

    def __init__(self, handler, finish_after, finish_route, now=None):
        super().__init__(handler)
        self.hits, self.finish_after, self.finish_route = 0, finish_after, finish_route
        self.now, self.finish_at = now, None  # 世界 Boss：到点结算（停手的时候不上报，不能数上报次数）
        self.queue = []
        self.total = 0.0
        self.times = []         # 每次世界 Boss 上报的时刻
        self.board = None       # 累计伤害 → 榜单推送
        self.finish = {"success": True}

    def request(self, route, body):
        out = super().request(route, body)
        if route in (b.R_UNION_HIT, boss_rules.R_WORLD_HIT):
            self.hits += 1
            self.total += body["hurt"]
            world = route == boss_rules.R_WORLD_HIT
            world and self.times.append(self.now())
            self.queue.append(SimpleNamespace(route=boss_rules.P_SELF if world else "onBossCopySelfHit", body={"hit": self.total if world else self.hits * 10.0}))
            if self.board is not None:
                self.queue.append(SimpleNamespace(route=boss_rules.P_RANK, body=self.board(self.total)))
            if self.finish_after and self.hits >= self.finish_after:
                self.queue.append(SimpleNamespace(route=self.finish_route, body=self.finish))
        return out

    def drain_pushes(self):
        if self.finish_at is not None and self.board is not None and not self.queue:   # 世界 Boss 的榜单不用我出手也会推过来
            self.queue.append(SimpleNamespace(route=boss_rules.P_RANK, body=self.board(self.total)))
        if self.finish_at is not None and self.now() >= self.finish_at:
            self.queue.append(SimpleNamespace(route=self.finish_route, body=self.finish))
            self.finish_at = None
        out, self.queue = self.queue, []
        return out


def board(*others, kind=2):
    """榜单推送：others = [(uid, 占比)]，我是 uid 7。"""
    def make(total):
        rows = sorted([*others, (7, total / HP * 100)], key=lambda r: -r[1])
        return {"type": kind, "high" if kind == 1 else "low": [{"uid": u, "rank": i + 1, "percent": round(s, 2)} for i, (u, s) in enumerate(rows)]}
    return make


def people(*powers):
    return [{"uid": i + 1, "userdata": {"fightPower": pw}} for i, pw in enumerate(powers)]


class BossRaidTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        patch = mock.patch.object(boss_rules, "beat", lambda boss: (1.0,) * 40)   # 节拍换成平的（单独测）：不然比两场的总伤害是碰运气
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(boss_rules, "event_beat", lambda boss: tuple(((i+1)*boss_rules.boss_beat.STEP, boss_rules.boss_beat.STEP) for i in range(40)))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(boss_rules, "LINGER_ODDS", 0.0)   # 复活后多躺一阵的（单独测）先关掉：不然停多久没有上限可比
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(boss_rules, "BASE_POWER", 1000)   # 测试里自己的战力是 1000：按它定速度，不换算
        patch.start()
        self.addCleanup(patch.stop)

        def sleep(ctx, seconds, background=True):
            self.clock += seconds
            return not ctx.stopped

        for patch in (mock.patch("zmxy.core.TaskContext.sleep", sleep), mock.patch.object(b.time, "monotonic", lambda: self.clock)):
            patch.start()
            self.addCleanup(patch.stop)

    def union(self, info, response=None, finish_after=3):
        response = response or {"code": 200, "Boss": {"hpMax": 1.0e9}, "endTime": (time.time() + 600) * 1000}
        conn = Raid(lambda r, bd: response if r == b.R_UNION_ENTER else None, finish_after, "onFinishLeagueBossCopy")
        return conn, run("union_boss", conn, info, {})

    def test_union_needs_a_league(self):
        conn, res = self.union({})
        self.assertEqual(res.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_union_reports_until_finish_then_exits(self):
        conn, res = self.union({"league": {"leagueId": 1}})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.routes(), ["challengeBoss", "attackBossCopy", "attackBossCopy", "attackBossCopy", "exitLeagueCopy"])
        hurts = [bd["hurt"] for r, bd in conn.sent if r == b.R_UNION_HIT]
        rate = 1.0e9 * 0.2 / 100 / 60
        self.assertTrue(all(rate / 4 <= h <= rate * 7 / 4 for h in hurts))

    def test_union_without_boss_hp_does_not_report_but_still_exits(self):
        conn, res = self.union({"league": {"leagueId": 1}}, {"code": 200, "Boss": {}})
        self.assertEqual(res.status, "blocked")
        self.assertEqual(conn.routes(), ["challengeBoss", "exitLeagueCopy"])

    # ---------------------------------------------------------------- 世界 Boss
    ROOMS = [{"roomId": "a", "userCnt": 6, "boss": {"hp": HP, "hpMax": HP}}, {"roomId": "b", "userCnt": 2, "boss": {"hp": HP * 0.7, "hpMax": HP}},
             {"roomId": "c", "userCnt": 2, "boss": {"hp": HP * 0.9, "hpMax": HP}}, {"roomId": "d", "userCnt": 0, "boss": {"hp": 0, "hpMax": HP}},
             {"roomId": "e", "userCnt": 1, "boss": {"hp": HP * 0.2, "hpMax": HP}}, {"roomId": "f", "userCnt": 10, "boss": {"hp": HP, "hpMax": HP}}]
    INFO = {"worldBoss": {"count": 1}, "uid": 7, "power": 1000}

    def world(self, info=None, options=None, seconds=300, inside=(), ranks=None, finish=None, rooms=None, rooms_of=None, late=(), look=None, stale=()):
        def handler(route, body):
            if route == p.ROUTE_ENTER:  # 世界 Boss 开始前刷新账号共享次数
                return {"info": self.INFO if info is None else info, "serverTime": 5e6}
            if route == boss_rules.R_WORLD_LIST:  # rooms(第几次查房间列表) → 那一次的列表；rooms_of(祖巫) → 这个祖巫的列表
                if rooms_of:
                    return {"rooms": rooms_of(body["bossId"])}
                return {"rooms": rooms(len(conn.bodies("getWorldBossRoomList"))) if rooms else self.ROOMS}
            if route == boss_rules.R_LOOK:  # 进房前看自己的资料
                return look
            if route == boss_rules.R_WORLD_JOIN:
                conn.joined, conn.finish_at = self.clock, self.clock + seconds - 10   # 每场 seconds 秒，Boss 在到点前 10 秒被打死
                conn.queue.extend(SimpleNamespace(route=boss_rules.P_JOIN, body=[m]) for m in late)   # late：不在进房应答里、随后才推来的成员（真机上自己的资料就是这样来的）
                return {"roomId": body["roomId"], "serverTime": 5e6, "endTime": 5e6 + seconds * 1e3, "message": list(inside)}
        conn = Raid(handler, None, boss_rules.P_FINISH, lambda: self.clock)
        conn.board = ranks
        conn.queue.extend(stale)  # 进房前就攒着的推送（上一个房间留下的）
        conn.finish = finish or conn.finish
        res = run("world_boss", conn, self.INFO if info is None else info, {"levels": [960101]} if options is None else {"levels": [960101], **options})
        return conn, res

    def test_forecast_follows_the_same_numbers_the_fight_uses(self):
        """估伤：低档 = 正常输出的中间速度 × 在打时间占比，高档 = 追赶的中间速度；装备、深渊、各祖巫的系数都算进去；不给祖巫 = 全部。"""
        boss, other = 961501, 961301
        hp = lambda i: float(boss_rules.data.row("Monster", i)["hpMax"])
        (which, low, high), (_, low2, _) = b.forecast(2_000_000, None, False, [boss, other])
        each = 2_000_000 * boss_rules.scale(2_000_000) * boss_rules.HARD[boss] / hp(boss) * 100 * 60
        self.assertEqual(which, boss)
        self.assertAlmostEqual(low, sum(boss_rules.PACE) / 2 * each * boss_rules.LIVE)
        self.assertAlmostEqual(high, sum(boss_rules.CHASE) / 2 * each * boss_rules.DUTY)
        self.assertGreater(high, low)
        self.assertAlmostEqual(low2 / low, boss_rules.HARD[other] / boss_rules.HARD[boss] * hp(boss) / hp(other))
        (_, dry_low, dry_high), = b.forecast(2_000_000, boss_rules.ABYSS, True, [boss])
        self.assertAlmostEqual(dry_low, low * boss_rules.ABYSS * boss_rules.ABYSS_STAY)
        self.assertAlmostEqual(dry_high, high * boss_rules.ABYSS)
        self.assertEqual(b.forecast(2_000_000, None, False, [123]), [])
        self.assertEqual([r[0] for r in b.forecast(2_000_000, None, False)], [i for i, _ in b.world_bosses()])

    def test_estimate_reads_power_and_gear_without_fighting(self):
        """估伤只登录、看一眼自己的资料：不进房间；全部祖巫都估；看资料失败就按没有装备加成算。"""
        asked = []
        worn = {"equipments": {"ornament": {"quality": 8}, "weaponSpirit": {"id": 28000205}}}

        def request(route, body):
            asked.append((route, body))
            return {"user": worn}

        session = SimpleNamespace(info={"uid": 7, "power": 2_000_000, "level": 168}, request=request)
        power, gear, rows, level = b.estimate(session)
        self.assertEqual(asked, [(boss_rules.R_LOOK, {"uid": 7})])
        self.assertEqual((power, gear, level), (2_000_000, boss_rules.ABYSS * boss_rules.LOTUS, 168))
        self.assertEqual(rows, b.forecast(2_000_000, gear, True))

        def broken(route, body):
            raise b.p.AllianceError("x")

        power, gear, rows, _ = b.estimate(SimpleNamespace(info={"uid": 7, "power": 2_000_000}, request=broken))
        self.assertIsNone(gear)
        self.assertEqual(rows, b.forecast(2_000_000, None, False))
        with self.assertRaises(b.p.AllianceError):
            b.estimate(SimpleNamespace(info={"uid": 7}, request=broken))

    def test_several_bosses_take_turns_from_the_last_one_down(self):
        info = {**self.INFO, "worldBoss": {"count": 4}}
        conn, res = self.world(info=info, options={"levels": [960101, 961501, 961301]})   # 金、三才、五行：从三才往金打，一轮完了回到第一个
        self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")], [961501, 961301, 960101, 961501])
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(res.detail.count("占比"), 4)             # 四场各一句
        conn, res = self.world(info=info)                       # 只选一个：次数用完为止
        self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")], [960101] * 4)
        conn, res = self.world(info=info, options={"times": 2})  # 行上填了 2 次：还有 4 次也只打 2 场
        self.assertEqual(len(conn.bodies("joinWorldBossCopy")), 2)
        conn, res = self.world(info=info, options={"times": 0})  # 填 0：不打
        self.assertEqual((res.status, len(conn.bodies("joinWorldBossCopy"))), ("skipped", 0), res.detail)

    def test_growth_plan_keeps_for_wing_week(self):
        """定时开着“按养成周”（plan=growth）：不看次数框，按现有次数留给翅膀光环养成周；开始那天游戏没更新就一场不打。"""
        from unittest import mock
        info = {**self.INFO, "worldBoss": {"count": 4}}
        with mock.patch.object(b.state, "growth_keep", return_value=3), mock.patch.object(b.online, "growth_week", return_value=None):
            conn, res = self.world(info=info, options={"times": 0, "plan": "growth"})
        self.assertEqual(len(conn.bodies("joinWorldBossCopy")), 1)  # 4 次留 3 次
        with mock.patch.object(b.state, "growth_keep", return_value=None), mock.patch.object(b.online, "growth_week", return_value=None):
            conn, res = self.world(info=info, options={"plan": "growth"})
        self.assertEqual((res.status, len(conn.bodies("joinWorldBossCopy"))), ("skipped", 0))
        self.assertIn("还没更新", res.detail)

    def test_finish_this_fight_then_stop(self):
        """“打完这场再停”：正打着的这一场照常打完，不再进下一个房间；还没进房间就选了的，一场也不打。"""
        from zmxy import core
        info = dict(self.INFO, worldBoss={"count": 4})
        asked = iter([False])  # 第一次进房前还没选，之后就是选了
        with mock.patch.object(core.TaskContext, "winding", new_callable=mock.PropertyMock, side_effect=lambda: next(asked, True)):
            conn, res = self.world(info=info)
        self.assertEqual(len(conn.bodies("joinWorldBossCopy")), 1)
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertIn("打完这一场后停止", res.detail)
        with mock.patch.object(core.TaskContext, "winding", new_callable=mock.PropertyMock, return_value=True):
            conn, res = self.world(info=info)
        self.assertEqual(len(conn.bodies("joinWorldBossCopy")), 0)

    def test_failed_finish_does_not_consume_a_local_chance_or_retry(self):
        info = {**self.INFO, "worldBoss": {"count": 2}}
        conn, res = self.world(info=info, finish={"success": False})
        self.assertEqual(res.status, "blocked", res.detail)
        self.assertEqual(len(conn.bodies("joinWorldBossCopy")), 1)
        self.assertIn("未按成功场次扣次数", res.detail)

    def test_revive_starts_a_fresh_attack_clock(self):
        self.assertEqual(boss_rules.lag(200.0, None, 38.0), 0.0)  # 复活后的第一笔不把之前躺着的时间算成请求延迟

    def test_exit_uncertain_result_is_not_silenced(self):
        ctx = SimpleNamespace(request=mock.Mock(side_effect=UncertainOutcome("退出结果未知")))
        with self.assertRaises(UncertainOutcome):
            b._exit(ctx, boss_rules.R_WORLD_EXIT)

    def test_stale_join_push_with_a_room_id_is_dropped(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 1.5, room_id="new")
        room.absorb(boss_rules.P_JOIN, {"roomId": "old", "message": people(900)}, 0)
        self.assertEqual(room.powers, {})
        room.absorb(boss_rules.P_JOIN, {"roomId": "new", "message": people(900)}, 0)
        self.assertEqual(sorted(room.powers), [1])

    def test_world_uses_lookup_power_when_login_snapshot_has_none(self):
        seen = []

        def fake_fight(ctx, room, hit_body, left):
            seen.append(room.power)
            room.finished = {"success": True}
            return boss_rules.DONE, "测试结算"

        with mock.patch.object(b, "world_fight", side_effect=fake_fight):
            conn, res = self.world(info={"worldBoss": {"count": 1}, "uid": 7}, look={"user": {"fightPower": 1234}})
        self.assertEqual(seen, [1234])
        self.assertEqual(res.status, "submitted", res.detail)

    def test_late_join_power_updates_the_room_power(self):
        room = b.Room(HP, 7, None, None, 5.5, 1.5)
        room.absorb(boss_rules.P_JOIN, [{"uid": 7, "userdata": {"fightPower": 3_000_000}}], 0)
        self.assertEqual((room.power, room.powers[7]), (3_000_000, 3_000_000))

    def test_power_pushed_during_a_life_scales_the_current_rate(self):
        late = {"uid": 7, "userdata": {"fightPower": 3_000}}
        with mock.patch.object(boss_rules, "WARMUP", ((0.0, 0.0), (1.0, 0.0))), \
                mock.patch.object(boss_rules, "WORLD_INTERVAL", (2.0, 2.0)), \
                mock.patch.object(boss_rules, "PACE", (0.2, 0.2)), \
                mock.patch.object(boss_rules, "CALM", 1e9), \
                mock.patch.object(boss_rules, "life_seconds", return_value=1000.0):
            low, _ = self.world(info={"worldBoss": {"count": 1}, "uid": 7, "power": 1000}, seconds=60)
            real_request = Raid.request

            def push_after_first_hit(conn, route, body):
                out = real_request(conn, route, body)
                if route == boss_rules.R_WORLD_HIT and conn.hits == 1:
                    conn.queue.append(SimpleNamespace(route=boss_rules.P_JOIN, body=[late]))
                return out

            with mock.patch.object(Raid, "request", push_after_first_hit):
                high, _ = self.world(info={"worldBoss": {"count": 1}, "uid": 7, "power": 1000}, seconds=60)
        self.assertGreater(high.total, low.total * 3)

    def test_tells_the_window_when_it_is_inside_a_room(self):
        """进了房间才算“正打着”（界面据此决定点停止要不要问）；出来以后不算。"""
        seen = []
        real = b.world_fight

        def fight(ctx, *args):
            seen.append(threading.get_ident() in activity._ENGAGED)
            return real(ctx, *args)

        with mock.patch.object(b, "world_fight", fight):
            self.world()
        self.assertEqual(seen, [True])
        self.assertNotIn(threading.get_ident(), activity._ENGAGED)

    def test_moves_on_to_the_next_boss_when_this_one_has_no_room(self):
        full = [{**r, "userCnt": 10} for r in self.ROOMS]
        conn, res = self.world(options={"levels": [960101, 961501]}, rooms_of=lambda boss: full if boss == 961501 else self.ROOMS)
        self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")], [960101])   # 三才的房间都满了：不等，先打金
        self.assertEqual(res.status, "submitted", res.detail)
        inside, busy = people(1500, 2000, 3000, 800), board((1, 3), (2, 4), (3, 4))        # 两个祖巫的房间里都有三个强的：各试 ROUND 个，再一起等新房间
        conn, res = self.world(options={"levels": [960101, 961501]}, inside=inside, ranks=busy, rooms=self.no_new_rooms)
        self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")][:3], [961501] * 3)
        self.assertEqual(res.status, "skipped", res.detail)
        self.assertIn("极·三才祖巫进了 3 个房间都争不到前 3", res.detail)

    def test_legendary_records_do_not_affect_runs_without_the_toggle(self):
        gold = BOSS_LOG.with_name("world_boss_gold.json")
        self.addCleanup(lambda: gold.unlink(missing_ok=True))
        gold.unlink(missing_ok=True)
        self.assertEqual(b.golden({"rankReward": {"type": "item", "subType": "220021005,220031005", "count": "1;1"}}), ["天气阵眼·阴"])   # 只有最高那档算
        self.assertEqual(b.golden({"success": True}), [])
        info, both = {**self.INFO, "worldBoss": {"count": 3}}, [960101, 961501]
        win = {"success": True, "rank": 1, "rankReward": {"type": "item", "subType": "220031005", "count": "1"}}
        conn, res = self.world(info=info, options={"levels": both}, finish=win)                      # 开关没开：只记下来，照打
        self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")], [961501, 960101, 961501])
        self.assertIn("出了传说阵眼（天气阵眼·阴）", res.detail)
        self.assertEqual(b.bosslog.golds(7), {960101, 961501})
        self.assertEqual(b.bosslog.golds(8), set())                                                    # 别的存档各记各的

    def test_legendary_skip_only_uses_this_run_even_when_history_contains_all_bosses(self):
        gold = BOSS_LOG.with_name("world_boss_gold.json")
        self.addCleanup(lambda: gold.unlink(missing_ok=True))
        gold.unlink(missing_ok=True)
        both = [960101, 961501]
        for boss in both:
            b.bosslog.gold(7, boss)
        win = {"success": True, "rank": 1, "rankReward": {"type": "item", "subType": "220031005", "count": "1"}}
        for _ in range(2):  # 连续两轮都重新挑战，不能把上一轮的出金带进来
            with mock.patch.object(b.bosslog, "golds", side_effect=AssertionError("历史出金记录不能决定本轮队列")):
                conn, res = self.world(info={**self.INFO, "worldBoss": {"count": 3}},
                                       options={"levels": both, "skip_gold": True}, finish=win)
            self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")], [961501, 960101])
            self.assertEqual(res.status, "submitted", res.detail)
            self.assertEqual(b.bosslog.golds(7), set(both))  # 保留历史记录

    def test_only_the_boss_that_drops_a_legendary_this_run_is_removed(self):
        with mock.patch.object(b, "golden", side_effect=[["天气阵眼·阴"], [], []]), \
                mock.patch.object(b.bosslog, "gold"):
            conn, res = self.world(info={**self.INFO, "worldBoss": {"count": 3}},
                                   options={"levels": [960101, 961501], "skip_gold": True})
        self.assertEqual([bd["bossId"] for bd in conn.bodies("joinWorldBossCopy")], [961501, 960101, 960101])
        self.assertEqual(res.status, "submitted", res.detail)

    def no_new_rooms(self, n):
        """前三次查到的是 ROOMS，之后每个房间的 Boss 都只剩一半血：再也没有“新房间”。"""
        return self.ROOMS if n <= 3 else [{**r, "boss": {"hp": r["boss"]["hp"] * 0.5, "hpMax": HP}} for r in self.ROOMS]

    def hurts(self, conn):
        return [bd["hurt"] for r, bd in conn.sent if r == boss_rules.R_WORLD_HIT]

    def test_world_requires_a_pick_and_known_count(self):
        self.assertEqual(self.world(options={"levels": []})[1].status, "skipped")
        conn, res = self.world(info={})
        self.assertEqual(res.status, "blocked")
        self.assertEqual(conn.bodies("joinWorldBossCopy"), [])
        self.assertEqual(self.world(info={"worldBoss": {"count": 0}})[1].status, "skipped")

    def test_room_is_healthy_and_least_crowded(self):
        pick = lambda tried=(): (b.pick_room(self.ROOMS, set(tried), 10, 5.5) or {}).get("roomId")
        self.assertEqual(pick(), "c")                     # 2 人的里血量高的；e 人更少但血量不到一半，f 满员，d 已倒
        self.assertEqual(pick("c"), "b")
        self.assertEqual(pick("cb"), "a")
        self.assertEqual(pick("cba"), "e")                # 没有血量过半的了：血量还够打出两倍目标的也行
        self.assertIsNone(pick("cbae"))
        conn, res = self.world()
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.bodies("joinWorldBossCopy")[0], {"bossId": 960101, "roomId": "c", "type": 0})
        self.assertEqual(conn.routes()[-1], "exitWorldBossCopy")
        self.assertTrue(all(bd["roomId"] == "c" and bd["bossId"] == 960101 for bd in conn.bodies("attack")))

    def rests(self, conn):
        """两次上报之间明显长过上报间隔的空档 = 停手。"""
        gaps = [later - earlier for earlier, later in zip(conn.times, conn.times[1:])]
        # 比最长的短停还长的才算（倒下等复活）：短停完先攒一轮才报（2–3 秒），那一轮没碰上节拍格就再等一轮
        return [g for g in gaps if g > boss_rules.PAUSE[-1][1] + 2 * boss_rules.WORLD_INTERVAL[1] + 0.5]

    def plans(self):
        """最近存的那一场记录里，打法每次变化时写的说明。"""
        file = max(BOSS_LOG.glob("*.json"), key=lambda f: f.stat().st_mtime_ns)
        return [why for _t, _target, why in json.loads(file.read_text(encoding="utf-8"))["plan"]]

    def test_keeps_hitting_at_a_normal_pace_until_the_boss_dies(self):
        for inside, ranks in (((), None), (people(5000, 5000), board((1, 30), (2, 20))),      # 对手不到 3 个：别人打多少、多强都不管
                              (people(5000, 5000, 500, 500), board((1, 30), (2, 20), (3, 2.9), (4, 0)))):  # 没到 3% 的路人不算对手
            conn, res = self.world(inside=inside, ranks=ranks)
            self.assertEqual(res.status, "submitted", res.detail)
            self.assertTrue(boss_rules.WARMUP[0][1] <= conn.times[0] - conn.joined <= boss_rules.WARMUP[-1][1] + boss_rules.WORLD_INTERVAL[1], conn.times[0] - conn.joined)  # 进房后先等一会儿
            self.assertGreater(conn.times[-1], conn.joined + 290 - boss_rules.REST[-1][1] - boss_rules.RISE[1] - boss_rules.PAUSE[-1][1])   # 过了目标占比也不挂机：一直打到 Boss 死
            self.assertGreater(conn.total / HP * 100, 6.5)
            rests = self.rests(conn)
            self.assertTrue(rests and all(boss_rules.REST[0][1] <= g <= boss_rules.REST[-1][1] + boss_rules.RISE[1] + boss_rules.PAUSE[-1][1] + boss_rules.WORLD_INTERVAL[1] for g in rests), rests)   # 打一阵停一阵，停的时候不上报
            top = boss_rules.PACE[1] * 1000 * MOST * PEAK * HARD
            self.assertTrue(all(1 <= h <= top + 1 for h in self.hurts(conn)), max(self.hurts(conn)))    # 速度只跟战力走
            self.assertLessEqual(conn.total, boss_rules.PACE[1] * 1000 * 1.2 * (conn.times[-1] - conn.times[0] + 3))

    def test_chases_like_someone_with_endless_revives(self):
        behind = dict(seconds=600, inside=people(500, 500, 500), ranks=board((1, 99), (2, 98), (3, 97)))  # 第 3 高的远在前面（进房血量未知，不触发退出）
        with mock.patch.object(b.Room, "pushing", lambda self, now: True):                                # 对手一直在打
            conn, res = self.world(**behind)
        gaps = [later - earlier for earlier, later in zip(conn.times, conn.times[1:])]
        self.assertLess(max(gaps), boss_rules.REST[0][1])                                                          # 追赶：倒下后看几秒，对手还在打就起来，没有三十多秒的等复活
        self.assertLessEqual(max(gaps), boss_rules.PAUSE[-1][1] + boss_rules.PEEK[1] + boss_rules.REVIVE_NOW[1] + 3 * boss_rules.WORLD_INTERVAL[1])
        conn, res = self.world(**behind)                                                                  # 榜单上对手的占比一直没涨（都没在打）
        self.assertTrue(any(g >= boss_rules.REST[0][1] for g in self.rests(conn)), self.rests(conn))               # 那我也照常躺着，不白用复活道具
        self.assertEqual(boss_rules.REVIVE_NOW[0], 2.0)                                                            # 用户定的：刚复活过两秒再打
        self.assertGreater(conn.total / (conn.times[-1] - conn.times[0]), sum(boss_rules.PACE) / 2 * 1000 * 0.65)  # 几乎一直在打
        top = boss_rules.CHASE[1] * 1000 * MOST * PEAK * HARD
        self.assertTrue(all(h <= top + 1 for h in self.hurts(conn)), max(self.hurts(conn)))               # 落后再多，速度也不超过自己战力打得出来的
        self.assertLessEqual(conn.total, boss_rules.CHASE[1] * 1000 * 1.2 * (conn.times[-1] - conn.times[0] + 3))
        self.assertLessEqual(boss_rules.CHASE[1], boss_rules.PACE[1] * 1.05)
        self.assertTrue(len(self.plans()) == 1 and "追到" in self.plans()[0], self.plans())               # 一直在追，没追上

    def test_pace_follows_power_faster_than_in_proportion(self):
        with mock.patch.object(boss_rules, "BASE_POWER", 2_000_000):
            self.assertEqual(boss_rules.scale(2_000_000), 1)
            self.assertAlmostEqual(boss_rules.scale(1_000_000), 0.5 ** boss_rules.SCALE)            # 战力低一半，每点战力打得也少
            self.assertTrue(1 < boss_rules.scale(3_000_000) < boss_rules.scale(boss_rules.CEIL_POWER))
            self.assertEqual(boss_rules.scale(9_000_000), boss_rules.scale(boss_rules.CEIL_POWER))             # 没量过的高战力：系数不再涨
        conn, _res = self.world(info=dict(self.INFO, power=500))
        top = boss_rules.PACE[1] * 500 * 0.5 ** boss_rules.SCALE * MOST * PEAK * HARD
        self.assertTrue(all(h <= top + 1 for h in self.hurts(conn)), max(self.hurts(conn)))

    def test_a_life_ends_by_the_boss_and_fades_once_the_mana_is_gone(self):
        weak, strong, four = (sorted(boss_rules.life_seconds(boss, power) for _ in range(400)) for boss, power in ((961301, 1_500_000), (961301, 2_500_000), (961401, 2_500_000)))
        self.assertGreater(strong[200], weak[200] + 15)                              # 五行：战力高的活得久
        self.assertEqual(len(boss_rules.LIFE[961401]), 1)                                     # 四象隔一阵放一次反弹技能（有人主动死、有人躲不好），战力高也死得勤：不分战力档
        self.assertLess(min(boss_rules.life_seconds(961301) for _ in range(400)), boss_rules.LIFE[961301][1][1][0][1])   # 战力读不到：按低的那档（高的那档最短也不止这么点）
        table = boss_rules.LIFE[961401][0][1]
        self.assertTrue(table[0][1] <= four[0] and four[-1] <= table[-1][1])
        self.assertTrue(all(boss_rules.LIFE_ANY[0][1] <= boss_rules.life_seconds(960101) <= boss_rules.LIFE_ANY[-1][1] for _ in range(200)))   # 没数据的祖巫用合起来的
        self.assertTrue(all(boss_rules.REST[0][1] <= boss_rules.draw(boss_rules.REST) <= boss_rules.REST[-1][1] for _ in range(200)))   # 倒下后停 30 秒出头
        self.assertTrue(boss_rules.flow(61, 84) < boss_rules.flow(50, 84) < boss_rules.flow(5, 84) < boss_rules.flow(20, 84) == boss_rules.flow(40, 84) == PEAK)   # 刚起来稍慢，接着一阵最猛，之后一截一截降
        self.assertAlmostEqual(sum(boss_rules.flow(t + 0.5, 84) for t in range(55)) / 55, 1.0, 1)   # 一条 55 秒的命平均下来是 1
        self.assertEqual((boss_rules.flow(61 + 10, 500), boss_rules.flow(61 + 45 + 10, 500)), (PEAK, PEAK))   # 活过 61 秒的再走一遍，不一直压在最低
        self.assertAlmostEqual(sum(boss_rules.flow(t + 0.5, 500) for t in range(140)) / 140, 1.0, 1)   # 一条 140 秒的命平均下来也是 1 左右（真机上原来只有 0.92）
        self.assertEqual((boss_rules.lag(10.0, None, 2.5), round(boss_rules.lag(12.8, 10.0, 2.5), 2), boss_rules.lag(10.0, 8.0, 2.5), boss_rules.lag(30.0, 10.0, 2.5)), (0.0, 0.3, 0.0, boss_rules.LAG_MOST))   # 发请求的工夫补进下一次，第一下不补
        beats = boss_rules.boss_beat.BEATS   # 在打的时候一阵高一阵低：照搬真人的节拍，每段平均是 1、至少半分钟，只有数字
        self.assertTrue({961301, 961401, 961501} <= set(beats) and all(len(parts) >= 50 for parts in beats.values()))
        self.assertTrue(all(len(part) * boss_rules.boss_beat.STEP >= 30 and abs(sum(part) / len(part) - 1) < 0.05 and max(part) <= 5 and min(part) >= 0
                            for parts in beats.values() for part in parts))
        self.assertIn(BEAT(961401), beats[961401])
        self.assertIn(BEAT(960101), [part for parts in beats.values() for part in parts])   # 没量过的祖巫：在全部里面抽
        self.assertEqual((boss_rules.world_hurt(100.0, 2.5), boss_rules.world_hurt(0.0, 2.5)), (250, 1))
        self.assertEqual((round(boss_rules.flow(84.1, 84), 2), boss_rules.flow(84 * 1.5, 84), boss_rules.flow(400, 84)), (0.8, 0.6, 0.6))   # 蓝空了就掉下去，活再久也回不来
        self.assertLessEqual(boss_rules.flow(20, 15), 0.7)                                   # 蓝很少的：猛的那一阵也打不出来
        self.assertAlmostEqual(boss_rules.beat_average((1.0, 5.0), 0.0, 1.0, 3.0), 2.0)     # 1 秒在低档、0.5 秒在高档，不能整段只取高档
        skills = boss_rules.data.slim("Skill", "consumeMP", "cd", "worldBossCD")
        at = lambda one, key: (lambda v: float(v[min(1, len(v) - 1)] or 0))(str(one.get(key)).split(";"))   # 1 级的
        cost = next(sid for sid, one in skills.items() if one.get("worldBossCD") and at(one, "consumeMP") > 0 and at(one, "worldBossCD") > 0)
        me = {"userdata": {"skill": {cost: 1, "1": 1}, "keyboard": {"skill1": int(cost), "Item1": 5}}}
        per = at(skills[cost], "consumeMP") / at(skills[cost], "worldBossCD")
        self.assertAlmostEqual(boss_rules.mana_seconds(me, {"mpMax": per * 50, "reMP": 0}), 100.0)             # 只按冷却的一半频率放：能撑两倍久
        self.assertAlmostEqual(boss_rules.mana_seconds(me, {"mpMax": per * 50, "reMP": per / 4}), 200.0)       # 算上自然回蓝
        self.assertEqual(boss_rules.mana_seconds(me, {"mpMax": per, "reMP": 0}), boss_rules.MANA_RANGE[0])
        self.assertEqual(boss_rules.mana_seconds(me, {"mpMax": per * 50, "reMP": per}), boss_rules.MANA_RANGE[1])       # 回得比耗得快
        self.assertEqual((boss_rules.mana_seconds(me, None), boss_rules.mana_seconds({"userdata": {}}, {"mpMax": 9})), (0.0, 0.0))   # 没查到面板 / 没有技能：算不出来
        room = b.Room(HP, 7, 1000, None, 5.5, 2.0, panel={"mpMax": per * 50, "reMP": 0})
        room.absorb(boss_rules.P_JOIN, [dict(me, uid=8)], 0)
        self.assertEqual(room.mana, boss_rules.MANA_USUAL)                                    # 别人的技能不关我的事
        room.absorb(boss_rules.P_JOIN, [dict(me, uid=7)], 0)
        self.assertAlmostEqual(room.mana, 100.0)

    def test_looks_up_one_roommate_panel_at_a_time(self):
        from zmxy import bosslog, protocol as p
        journal = bosslog.Journal(100.0, me={"uid": 7})
        for uid in (7, 8, 9, 10):
            journal.join(uid, {"userdata": {}}, 1e6, 100.0)
        journal.rank({"low": [{"uid": 7, "percent": "30"}, {"uid": 8, "percent": "12.5"}, {"uid": 9, "percent": "3"}, {"uid": 10, "percent": "2.99"}]}, 100.5)
        asked = []

        def request(route, body):
            asked.append((route, body["uid"]))
            if body["uid"] == 9:
                raise p.AllianceError("查不到")
            return {"code": 200, "user": {"fightPower": 2e6, "powerAttr": {"atk": 30000}, "loginIp": "x", "bag": {
                "item": {"101014003": {"count": 2}, "101004026": {"count": 5}, "1": {"count": 9}}, "warehouseItem": {"101014003": {"count": 1}, "101002026": {"count": 7}}}}}

        with mock.patch.object(b.random, "uniform", return_value=3.0):
            boss_fight.look(request, journal, 101.0)
            boss_fight.look(request, journal, 102.0)   # 还没到下一次
            boss_fight.look(request, journal, 104.0)
            boss_fight.look(request, journal, 108.0)   # 都查过了（自己不查，查不到的不重查）
            boss_fight.look(request, None, 109.0)      # 没有记录就不查
        self.assertEqual(asked, [(boss_rules.R_LOOK, 8), (boss_rules.R_LOOK, 9)])
        self.assertEqual(journal.players[8]["panel"], {"t": 1.0, "power": 2e6, "attr": {"atk": 30000}})
        self.assertEqual(journal.players[9]["panel"], {})
        held = journal.players[8]["stock"]                           # 同一次查询里数囤货：背包 + 仓库，只数那几样
        self.assertEqual((held["战力"], held["蓝瓶"], held["律令小怪材料"], held["萝卜"], len(held)), (2000000, 3, 12, 0, 1 + len(bosslog.STOCK)))
        self.assertNotIn("stock", journal.players[9])
        self.assertNotIn("panel", journal.players[7])
        self.assertNotIn("panel", journal.players[10])               # 占比不到 3% 的不查
        with mock.patch.object(bosslog, "folder", return_value=BOSS_LOG / "keep"):
            journal.save("done", "", 7)
        (file,) = (BOSS_LOG / "keep").glob("*.json")
        self.assertEqual([q["uid"] for q in json.loads(file.read_text(encoding="utf-8"))["players"]], [7, 8, 9])   # 也不存

    def test_pace_follows_my_ornament_and_weapon_spirit(self):
        gear = lambda quality, spirit: {"userdata": {"equipments": {"ornament": {"quality": quality}, "weaponSpirit": {"id": spirit}}}}
        self.assertEqual(boss_rules.gear_of(gear(7, 280001007)), 1.0)                          # 主线饰品、灵雀
        self.assertEqual(boss_rules.gear_of(gear(9, "280003006")), boss_rules.ABYSS)                    # 深渊饰品、派对精灵
        self.assertEqual(boss_rules.gear_of(gear(7, 280002004)), 1.0)                          # 红莲 4 级不算
        self.assertAlmostEqual(boss_rules.gear_of(gear(9, 280002005)), boss_rules.ABYSS * boss_rules.LOTUS)      # 深渊 + 红莲 5 级
        self.assertIsNone(boss_rules.gear_of({"userdata": {"fightPower": 5}}))                 # 没带装备：读不到
        self.assertLessEqual(boss_rules.CHASE[1] * boss_rules.ABYSS * boss_rules.LOTUS, 0.44)                    # 用户定的追赶上限：深渊 + 红莲的 200 万号不超过 0.44
        me = {"uid": 7, "userdata": dict(gear(9, 280002006)["userdata"], fightPower=1000)}
        speed = lambda conns: sum(c.total for c in conns) / sum(c.times[-1] - c.times[0] for c in conns)
        plains, strongs = [self.world()[0] for _ in range(8)], [self.world(inside=[me])[0] for _ in range(8)]
        plain, strong = plains[0], strongs[0]
        top = boss_rules.PACE[1] * 1000 * MOST * PEAK * HARD
        self.assertLessEqual(max(self.hurts(plain)), top + 1)                       # 读不到自己的装备：按最弱的算
        self.assertLessEqual(max(self.hurts(strong)), top * boss_rules.ABYSS * boss_rules.LOTUS + 1)
        self.assertGreater(speed(strongs), speed(plains) * 1.15)                      # 深渊 + 红莲：同战力下明显打得多
        laters = [self.world(late=[me])[0] for _ in range(8)]                         # 真机：自己的资料不在进房应答里，进房后才推过来——也得算上
        self.assertGreater(speed(laters), speed(plains) * 1.15)
        looked = [self.world(look={"user": me["userdata"]})[0] for _ in range(8)]      # 进房前先看一眼自己的资料：从第一下起就按装备打
        self.assertGreater(speed(looked), speed(plains) * 1.15)
        self.assertEqual((boss_rules.abyss(gear(9, 1)), boss_rules.abyss(gear(7, 1)), boss_rules.abyss({"userdata": {}})), (True, False, False))
        room = b.Room(HP, 7, 1000, None, 5.5, 2.0)
        room.absorb(boss_rules.P_JOIN, [dict(me, uid=8)], 0)
        self.assertFalse(room.dry)                                                    # 别人带深渊饰品不关我的事
        room.absorb(boss_rules.P_JOIN, [me], 0)
        self.assertTrue(room.dry)                                                     # 我带深渊饰品：蓝少，短停更勤
        self.assertAlmostEqual(boss_rules.ABYSS * boss_rules.ABYSS_STAY, 1.28, 1)                       # 整场（算上歇的）比带主线饰品的多不到三成（新表量的）

    def test_the_kill_comes_at_half_blood_then_every_ninety_seconds(self):
        self.assertEqual((boss_rules.KILL_HP, boss_rules.KILL_WARM, boss_rules.KILL_EVERY), (0.5, 30.0, 90.0))    # 半血放第一次，之后 90 秒一次（MAi 表读出来的）
        self.assertTrue({961301, 961401, 961501} <= set(boss_rules.KILL_BOSSES))               # 五行、四象、三才都有
        self.assertTrue(set(boss_rules.KILL_BOSSES) <= {i for i, _ in b.world_bosses()})

    def test_the_ninety_seconds_run_a_little_long(self):
        low, high = boss_rules.KILL_CAST                                                        # 90 秒是按游戏帧数的：实测两次隔 93 秒上下
        self.assertTrue(0 < low < high <= 6 and 92 <= boss_rules.KILL_EVERY + (low + high) / 2 <= 94)

    def test_sees_the_room_fall_together(self):
        def watch(stops: dict, until: float, idle: tuple = ()) -> b.Room:
            """每 2 秒推一次榜：stops = {uid: 打到第几秒停手}；idle 里的人 10 秒才蹭一下。"""
            room, t = b.Room(HP, 7, 1000, None, 5.5, 1.5), 0.0
            while t <= until:
                rows = [{"uid": uid, "rank": 1, "percent": round(min(t, stop) * 0.1, 2)} for uid, stop in stops.items()]
                rows += [{"uid": uid, "rank": 9, "percent": round(t // 10 * 0.01, 2)} for uid in idle]
                room.absorb(boss_rules.P_RANK, {"type": 2, "low": rows, "high": []}, t)
                t += 2.0
            return room

        self.assertIsNone(watch({1: 99, 2: 99, 3: 99}, 40).wiped(40))                 # 都在打
        self.assertIsNone(watch({1: 30, 2: 99, 3: 99}, 38).wiped(38))                 # 只倒了一个：不算
        self.assertEqual(watch({1: 30, 2: 30, 3: 99}, 38).wiped(38), (30.0, 2))       # 两个一起倒、不少于还站着的：房间刚挨了一次
        self.assertEqual(watch({1: 30, 2: 32, 3: 30, 4: 99}, 40).wiped(40), (30.0, 3))
        self.assertIsNone(watch({1: 30, 2: 30, 3: 99, 4: 99, 5: 99}, 38).wiped(38))   # 站着的比倒的多：不算
        self.assertIsNone(watch({1: 30, 2: 30, 3: 99}, 34).wiped(34))                 # 才停 4 秒：还看不出来
        self.assertIsNone(watch({1: 30, 2: 30, 3: 99}, 50).wiped(50))                 # 停了 20 秒：早过去了
        self.assertIsNone(watch({1: 99}, 38, idle=(8, 9)).wiped(38))                  # 挂着偶尔蹭一下的人不算在打，他们不涨也不算倒
        self.assertEqual(watch({1: 30, 2: 30}, 38, idle=(8, 9)).wiped(38), (30.0, 2))

    def test_tells_whether_the_rivals_that_matter_are_still_hitting(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 2.0, pct=10)
        push = lambda t, *shares: room.absorb(boss_rules.P_RANK, {"type": 2, "low": [{"uid": i + 1, "rank": 1, "percent": v} for i, v in enumerate(shares)], "high": []}, t)
        push(0, 20, 5, 1)
        push(2, 20, 5, 1)
        self.assertFalse(room.pushing(3))                  # 谁都没涨
        push(4, 20, 5.5, 1.2)
        self.assertFalse(room.pushing(5))                  # 涨的是落后我一大截的（5.5 < 10 − 2）和没到 3% 的路人：不要紧
        push(6, 20.3, 5.5, 1.2)
        self.assertTrue(room.pushing(7))                   # 排在前面的还在打
        self.assertFalse(room.pushing(6 + boss_rules.PUSHING + 1))  # 他也停了几秒了
        self.assertTrue(boss_rules.PEEK[0] >= 2 and boss_rules.PEEK[1] <= 6)
        self.assertTrue(boss_rules.along(boss_rules.KEEN, 0.5) <= 0.3 and boss_rules.along(boss_rules.KEEN, 0.05) == 1.0 and boss_rules.along(boss_rules.KEEN, 0.3) > boss_rules.along(boss_rules.KEEN, 0.5))   # 全场秒杀：半血时多半躺着，血越少越不躺
        self.assertFalse(boss_rules.pressing(10, 0.05, 200, 35, 15))   # 躺完还有 165 秒、能再涨 8 个点：躺得起
        self.assertTrue(boss_rules.pressing(10, 0.05, 120, 35, 15))    # Boss 只剩 120 秒：躺完只能再涨 4 个点，够不到 15 → 不躺
        self.assertFalse(boss_rules.pressing(10, 0.05, 0, 35, 15) or boss_rules.pressing(10, 0.05, 120, 35, 0))   # 估不出 Boss 还能活多久 / 没有可比的：不算

    def test_gets_up_as_soon_as_real_players_do(self):
        self.assertTrue(0 < boss_rules.RISE[0] < boss_rules.RISE[1] <= 3)                                # 从最后一笔伤害算起 REST + RISE 秒后出下一笔
        conn, res = self.world()
        self.assertEqual(res.status, "submitted", res.detail)
        rests = [g for g in self.rests(conn) if g >= boss_rules.REST[0][1]]
        self.assertTrue(rests and all(g <= boss_rules.REST[-1][1] + boss_rules.RISE[1] + boss_rules.PAUSE[-1][1] + 0.5 for g in rests), rests)
        with mock.patch.object(boss_rules, "LINGER_ODDS", 1.0):                                 # 有时候复活了没马上接着打：多躺几秒到一分钟
            conn, res = self.world()
        rests = [g for g in self.rests(conn) if g >= boss_rules.REST[0][1]]
        self.assertTrue(rests and all(g >= boss_rules.REST[0][1] + boss_rules.LINGER[0][1] for g in rests), rests)
        self.assertTrue(boss_rules.WARMUP[-1][1] <= 50 and any(q >= 0.8 and t <= 27 for q, t in boss_rules.WARMUP))   # 八成的时候还是 27 秒以内出手

    def test_leaves_more_room_while_the_boss_still_has_plenty_of_blood(self):
        self.assertEqual((boss_rules.along(boss_rules.LEAD, 0.0), boss_rules.along(boss_rules.LEAD, 1.0)), (boss_rules.LEAD[0][1], boss_rules.LEAD[-1][1]))
        self.assertTrue(boss_rules.along(boss_rules.LEAD, 0.1) < boss_rules.along(boss_rules.LEAD, 0.3) < boss_rules.along(boss_rules.LEAD, 0.6))        # Boss 血越多，后面变数越大，领先留得越多
        self.assertAlmostEqual(boss_rules.along(((0.0, 1.0), (1.0, 3.0)), 0.25), 1.5)
        room = b.Room(HP, 7, 1000, None, 5.5, 2.0, hp=HP * 0.8)
        room.absorb(boss_rules.P_JOIN, people(500, 500), 0)
        room.absorb(boss_rules.P_RANK, board((1, 30), (2, 20))(0), 0)
        self.assertEqual((room.third(), room.third(100), room.target()), (0.0, 0.0, 5.5))            # 对手只有两个：现在没有第 3 名
        self.assertAlmostEqual(room.third(100, ghosts=True), boss_rules.GHOST * 80)                           # Boss 还要活一阵：缺的那个当作后面会进来的人
        self.assertAlmostEqual(room.target(100), boss_rules.GHOST * 80 + 2.0)
        self.assertIsNone(room.hopeless(9.0, 100))                                                   # 退不退只看真的对手，不看还没来的人

    def test_back_to_normal_once_past_the_third_best_rival(self):
        with mock.patch.object(boss_rules, "LEAD", ((0.0, 1.5), (1.0, 1.5))), mock.patch.object(boss_rules, "SWING", (1.0, 1.0)):
            conn, res = self.world(inside=people(5000, 5000, 500), ranks=board((1, 30), (2, 20), (3, 7)))   # 前两个强得多，不去比；第 3 个打得过
        plans = self.plans()
        self.assertTrue(len(plans) == 2 and "追到估计 8.50%" in plans[0] and "正常输出" in plans[1], plans)     # 先追到第 3 高的 7% + 领先，之后正常输出
        self.assertGreater(conn.total / HP * 100, 12)                                                      # 追上以后接着打，不停在 8.5%

    def test_chases_at_once_when_stuck_behind_rivals_of_my_own_strength(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 1.5, pct=6, hp=HP * 0.5)
        room.absorb(boss_rules.P_JOIN, people(1050, 3000, 900, 5000), 0)
        seen = lambda *rows: (room.absorb(boss_rules.P_RANK, board(*rows)(HP * 0.06), 0), room.pressed())[1]
        self.assertTrue(seen((1, 9), (2, 8)))            # 对手才两个，可前面那个 1050 的战力和我差不多：不等凑够 3 个（缺的那个按后面会进来的人算，我还不稳）
        self.assertFalse(seen((1, 2.9), (2, 8)))         # 他占比还不到 3%：路人，不算
        self.assertFalse(seen((1, 5), (2, 8), (3, 9)))   # 比我弱一截的（出伤只有我的 0.84 倍）排在我前面：不算，照正常输出打一会儿就超过去了
        self.assertFalse(seen((4, 9), (2, 8), (3, 5)))   # 前面两个都强得多：不是打得过的
        self.assertFalse(seen((1, 5), (2, 9), (3, 4)))   # 战力相近的都在我后面
        self.assertFalse(seen((1, 9), (2, 3.5), (3, 4))) # 前面有战力相近的，可我现在已经比第 3 个对手多出一段领先：稳了，不用追
        room.power = None
        self.assertFalse(room.pressed())                 # 自己的战力读不到：不判断
        with mock.patch.object(boss_rules, "ABYSS_EVERY", 0.001):                              # 带深渊饰品的平时短停不断；追赶时没有这些多出来的短停
            me = {"uid": 7, "userdata": {"fightPower": 1000, "equipments": {"ornament": {"quality": 9}, "weaponSpirit": {}}}}
            near = [{"uid": i, "userdata": {**me["userdata"], "fightPower": 980}} for i in (1, 2, 3)]   # 装备一样、战力差一点：出伤相近
            conn, _res = self.world(seconds=400, inside=[me, *near], ranks=board((1, 99), (2, 98), (3, 97)))
        gaps = [later - earlier for earlier, later in zip(conn.times, conn.times[1:])]
        self.assertLess(sum(g > boss_rules.PAUSE[0][1] for g in gaps[5:]), len(gaps) / 4)
        self.assertIn("前面有出伤相近的人", self.plans()[0])

    def test_three_gears_low_never_chases_and_high_always_does(self):
        behind = dict(inside=people(500, 500, 500), ranks=board((1, 99), (2, 98), (3, 97)), seconds=400)   # 中档在这种局面下会追
        with mock.patch.object(boss_rules, "DOOMED", 1e9):                                      # 不让它中途退出，只看节奏
            low, _ = self.world(options={"mode": boss_rules.LOW}, **behind)
            self.assertIn("低档", self.plans()[0])
            self.assertTrue(all("加大输出" not in line for line in self.plans()))
            self.assertTrue(any(g >= boss_rules.REST[0][1] for g in self.rests(low)), self.rests(low))    # 倒下就老实等复活
            with mock.patch.object(b.Room, "pushing", lambda self, now: True):
                high, _ = self.world(options={"mode": boss_rules.HIGH}, seconds=400)            # 没有对手也追
            self.assertIn("高档", self.plans()[0])
            gaps = [later - earlier for earlier, later in zip(high.times, high.times[1:])]
            self.assertLess(max(gaps), boss_rules.REST[0][1])                                   # 从不等复活
            mid, _ = self.world(options={"mode": "别的"}, seconds=400)                   # 没存过 / 认不出：中档
            self.assertIn("正常输出", self.plans()[0])
        self.assertGreater(high.total, low.total * 1.02)
        runs, start = [], low.times[0]                                                  # 低档一条命不超过 max(90, 满蓝秒数) + 25 秒
        for earlier, later in zip(low.times, low.times[1:]):
            if later - earlier >= boss_rules.REST[0][1]:
                runs.append(earlier - start); start = later
        self.assertTrue(runs and max(runs) <= max(boss_rules.LOW_LIFE[0], boss_rules.MANA_USUAL) + boss_rules.LOW_LIFE[1] + boss_rules.PAUSE[-1][1], runs)
        self.assertEqual([v for v, _ in boss_rules.GEARS], [boss_rules.LOW, boss_rules.MID, boss_rules.HIGH])

    def test_pushes_left_over_from_the_last_room_are_dropped(self):
        """换房间后，上一个房间迟到的推送不能算成这个房间的（真机：一进新房间占比就是上一个房间的 4.85%）。"""
        room = b.Room(HP, 7, 1000, None, 5.5, 1.5, room_id="961501_1")
        room.absorb(boss_rules.P_SELF, {"roomId": "961501_0", "uid": "7", "hit": HP * 0.0485}, 0)
        self.assertEqual(room.pct, 0.0)
        room.absorb(boss_rules.P_SELF, {"roomId": "961501_1", "uid": "7", "hit": HP * 0.01}, 0)
        self.assertAlmostEqual(room.pct, 1.0)
        conn, res = self.world(stale=[SimpleNamespace(route=boss_rules.P_SELF, body={"hit": HP * 0.9})])   # 不带房间号的：进房前攒着的一律扔掉
        self.assertNotIn("占比 9", res.detail.replace("占比 9.", ""))   # 90% 的那一笔没算进来（自己打出来的 9 点几不算）

    def test_too_strong_is_judged_by_power_and_gear_together(self):
        """打不过 = 按战力和装备估的出伤比我高一成以上（用户 2026-10-11 定：不光比战力）。"""
        worn = {"equipments": {"ornament": {"quality": 9}, "weaponSpirit": {"id": 280002006}}}

        def strong(mine, gear, *others):
            room = b.Room(HP, 7, mine, None, 5.5, 1.5, gear=gear)
            room.absorb(boss_rules.P_JOIN, [{"uid": i + 1, "userdata": {"fightPower": pw, **(worn if geared else {})}} for i, (pw, geared) in enumerate(others)], 0)
            room.absorb(boss_rules.P_RANK, board(*((i + 1, 4) for i in range(len(others))))(0), 0)
            return len(room.strong())

        self.assertEqual(strong(2_000_000, None, (2_200_000, False), (2_050_000, False)), 1)   # 都没装备加成：战力高一成的出伤高 17%，算打不过；高 2.5% 的不算
        self.assertEqual(strong(2_000_000, boss_rules.ABYSS * boss_rules.LOTUS, (2_200_000, False), (2_600_000, False)), 0)   # 我有深渊 + 红莲：战力高三成、没装备的也打得过
        self.assertEqual(strong(2_000_000, None, (1_900_000, True)), 1)                        # 战力比我低，可他有深渊 + 红莲：打不过
        self.assertEqual(strong(None, None, (2_200_000, False)), 0)                            # 自己的战力读不到：不判断

    def test_three_stronger_rivals_at_three_percent_mean_quit(self):
        strong, busy = people(1600, 2000, 3000, 800), board((1, 4), (2, 4), (3, 4))  # 三个比我强的都打到了 3%：退出
        self.assertEqual(self.world(inside=strong, ranks=busy, rooms=self.no_new_rooms)[1].status, "skipped")

    def test_rivals_on_the_other_board_do_not_count(self):
        conn, _ = self.world(inside=people(600000, 700000, 800000, 900000))  # 金之祖巫高战线 491000：他们在高战榜，我在普通榜
        self.assertTrue(len(self.plans()) == 1 and "对手 0 人，正常输出" in self.plans()[0], self.plans())   # 不追

    def test_quits_when_three_stronger_rivals_are_scoring(self):
        inside = people(1500, 2000, 3000, 800)
        conn, res = self.world(inside=inside, ranks=board((1, 3), (2, 4), (3, 4)), rooms=self.no_new_rooms)
        self.assertEqual(res.status, "skipped", res.detail)
        self.assertEqual(conn.routes().count("entry"), 1)  # 只在开始刷新一次；换房不重复读次数
        self.assertEqual([bd["roomId"] for bd in conn.bodies("joinWorldBossCopy")], ["c", "b", "a"])  # 现有的房间只试 ROUND 个，e 不去了
        self.assertEqual(conn.routes().count("exitWorldBossCopy"), 3)
        self.assertEqual(len(conn.bodies("attack")), 0)       # 进房后还在等出手的时候榜单就推过来了：一下没打就走
        self.assertIn("进了 3 个房间都争不到前 3", res.detail)
        self.assertIn(f"等了 {b.WAIT_ROOMS // 60} 分钟也没等到", res.detail)
        polls = len(conn.bodies("getWorldBossRoomList")) - 4    # 之后一直在等新房间：隔 POLL 秒查一次，等满为止
        self.assertTrue(b.WAIT_ROOMS / b.POLL - 3 <= polls <= b.WAIT_ROOMS / b.POLL + 3, polls)
        conn, res = self.world(inside=inside, ranks=board((1, 2.9), (2, 4), (3, 4)))  # 比我强的三个里有一个还没到 3%：不退
        self.assertEqual((res.status, len(conn.bodies("joinWorldBossCopy"))), ("submitted", 1), res.detail)

    def test_waits_for_a_reborn_room_after_three_failed_ones(self):
        calm = []                                             # 新房间里那三个强的还没发力

        def rooms(n):
            if n < 8:
                return self.ROOMS
            if n < 10:                                        # a 的 Boss 被打死了，在等复活
                return [{**r, "boss": {"hp": 0, "hpMax": HP}} if r["roomId"] == "a" else r for r in self.ROOMS]
            calm.append(1)
            return [{**r, "userCnt": 1} if r["roomId"] == "a" else r for r in self.ROOMS]  # a 重开了：满血

        busy = board((1, 3), (2, 4), (3, 4))
        conn, res = self.world(inside=people(1500, 2000, 3000, 800), ranks=lambda total: (board() if calm else busy)(total), rooms=rooms)
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual([bd["roomId"] for bd in conn.bodies("joinWorldBossCopy")], ["c", "b", "a", "a"])   # 旧房间不再试（e 没去），重开的 a 再进
        self.assertTrue(len(conn.bodies("attack")) > 5)

    def test_a_room_i_left_counts_as_new_once_its_fight_has_run_out(self):
        conn, res = self.world(inside=people(1500, 2000, 3000, 800), ranks=board((1, 3), (2, 4), (3, 4)))  # 列表一直不变：c、a 一直是 ≥ 90% 血
        joins = [bd["roomId"] for bd in conn.bodies("joinWorldBossCopy")]
        self.assertEqual(joins[:3], ["c", "b", "a"])
        self.assertTrue(set(joins[3:]) == {"c", "a"} and len(joins) < 3 + 2 * b.WAIT_ROOMS / 300 + 3, joins)   # 每场 300 秒：到点以后才又算新房间，不是每次查到都进
        self.assertEqual(res.status, "skipped", res.detail)

    def test_no_room_at_the_start_waits_too(self):
        full = [{**r, "userCnt": 10} for r in self.ROOMS]
        conn, res = self.world(rooms=lambda n: full if n < 5 else self.ROOMS)   # 一开始全满；后来有了，但等的是新房间：只进血量 ≥ FRESH 的
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual([bd["roomId"] for bd in conn.bodies("joinWorldBossCopy")], ["c"])   # c 90%、2 人；a 满血但 6 人

    def test_reborn_rooms_and_the_wait_between_polls(self):
        tried = {"a": (HP * 0.4, None), "b": (HP * 0.6, 900), "c": (HP * 0.95, 900), "d": (HP * 0.3, None), "e": (HP * 0.5, 900)}
        b.reborn(self.ROOMS, tried, 500)                      # a 现在满血（比离开时高）、d 血量 0（死了）：都算重开过；b 0.7 > 0.6 也是
        self.assertEqual(sorted(tried), ["c", "e"])           # c 0.9 < 0.95、e 0.2 < 0.5：还是我离开的那一场
        b.reborn(self.ROOMS, tried, 900)                      # 那一场的结束时间过了：不管血量，都算重开
        self.assertEqual(tried, {})
        self.assertEqual((b.pick_room(self.ROOMS, {}, 10, 5.5, fresh=True) or {}).get("roomId"), "c")   # 只要血量 ≥ FRESH 的：c、a 里人少的
        self.assertEqual((b.pick_room(self.ROOMS, {"c": 0}, 10, 5.5, fresh=True) or {}).get("roomId"), "a")
        self.assertIsNone(b.pick_room(self.ROOMS, {"c": 0, "a": 0}, 10, 5.5, fresh=True))                 # f 满血但满员
        dead = [{"roomId": "x", "rebirthTime": 1_004_000}, {"roomId": "y", "rebirthTime": 1_050_000}, {"roomId": "z", "rebirthTime": 0}]
        self.assertEqual(b.nap(dead, 1000), 5)                # 最早的 4 秒后复活：卡在它之后 1 秒
        self.assertAlmostEqual(b.nap(dead, 1003.9), 1.1)      # 马上就复活：也等过它之后 1 秒
        self.assertEqual(b.nap(dead, 1004.5), b.POLL)         # 已经过了的复活时刻不算
        self.assertEqual(b.nap(dead[1:], 1000), b.POLL)       # 还早：最长 POLL 秒
        self.assertEqual(b.nap(dead, None), b.POLL)           # 读不到服务器时间
        self.assertEqual(b.nap(self.ROOMS, 1000), b.POLL)     # 没有房间报复活时间

    def test_leaves_as_soon_as_the_remaining_hp_cannot_get_me_there(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 1.5, pct=2)
        self.assertIsNone(room.hopeless())                    # 血量未知：不判断
        room.hp = HP * 0.04
        self.assertIsNone(room.hopeless())                    # 2% + 4% 过得了 5% 的门槛
        room.hp = HP * 0.03
        self.assertEqual(room.hopeless(), 5.0)                # 2% + 3% 到不了“超过 5%”
        room.hp, room.pct = HP * 0.2, 6
        room.absorb(boss_rules.P_RANK, board((1, 40), (2, 30), (3, 26.5))(HP * 0.06), 0)
        self.assertEqual(room.hopeless(), 26.5)               # 6% + 20% 超不过对手里第 3 高的 26.5%：不用等血量掉到 5%
        room.absorb(boss_rules.P_RANK, board((1, 40), (2, 30), (3, 25))(HP * 0.06), 0)
        self.assertIsNone(room.hopeless())
        self.assertIsNone(room.hopeless(19.5))                # 按我的速度到 Boss 死还能涨 19.5 个点：6 + 19.5 过得了 25
        self.assertEqual(room.hopeless(18), 25)               # 只能涨 18 个点：血还够，但我打不了那么快 → 走，不冲
        conn, res = self.world(inside=people(500, 500, 500), ranks=board((1, 95), (2, 94), (3, 93)))   # 进房血量未知时不判断：照常打
        self.assertEqual(len(conn.bodies("joinWorldBossCopy")), 1)

    def test_only_rivals_who_reached_three_percent_count(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 1.5)
        room.absorb(boss_rules.P_JOIN, people(3000, 3000, 900, 800, 700), 0)
        room.absorb(boss_rules.P_RANK, board((1, 0.5), (2, 0.5), (3, 12), (4, 8), (5, 2))(0), 0)
        self.assertEqual((len(room.scoring()), room.third(), room.target()), (2, 0.0, 5.5))   # 到 3% 的只有两个：打到门槛出头
        room.absorb(boss_rules.P_RANK, board((1, 0.5), (2, 0.5), (3, 12), (4, 8), (5, 6))(0), 0)
        self.assertEqual((len(room.scoring()), room.third(), room.target()), (3, 6, 7.5))     # 第三个也到了：比第 3 高的 6% 多一段领先
        room.absorb(boss_rules.P_RANK, board((1, 3), (2, 0.5), (3, 12), (4, 8), (5, 6))(0), 0)
        self.assertEqual((room.third(), len(room.strong())), (6, 1))                          # 第 3 高的还是 6%；超不过的只有一个到了 3%
        blind = b.Room(HP, 7, None, None, 5.5, 1.5)                                           # 自己的战力读不到：照样只保第 3，不判断强敌
        blind.absorb(boss_rules.P_RANK, board((1, 20), (2, 12), (3, 8), (4, 2))(0), 0)
        self.assertEqual((blind.target(), blind.strong()), (9.5, []))

    def test_strong_latecomers_do_not_chase_me_out_of_a_won_fight(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 0.3, hp=HP * 0.05, pct=9)
        room.absorb(boss_rules.P_JOIN, people(5000, 5000, 5000), 0)
        room.absorb(boss_rules.P_RANK, board((1, 5), (2, 4), (3, 3))(HP * 0.09), 0)
        self.assertEqual(len(room.strong()), 3)
        self.assertTrue(room.safe())
        room.hp = HP * 0.5
        self.assertFalse(room.safe())

    def test_each_room_leaves_one_line_about_everyone_in_it(self):
        shutil.rmtree(BOSS_LOG, ignore_errors=True)
        saved = lambda: [json.loads(f.read_text(encoding="utf-8")) for f in sorted(BOSS_LOG.glob("*.json"), key=lambda f: f.stat().st_mtime_ns)]
        inside = [{"uid": "1", "userdata": {"fightPower": 5000, "level": 88, "attack": 1234}}, {"uid": "7", "userdata": {"fightPower": 1000}}]  # 真机上 uid 是数字串
        self.world(inside=inside, ranks=lambda total: {"type": 2, "low": [{"uid": "1", "rank": 2, "percent": "6.50", "username": "甲"},   # 占比也是字符串
                                                                          {"uid": "7", "rank": 1, "percent": f"{total / HP * 100:.2f}"}]})
        (file,) = BOSS_LOG.glob("*.json")                          # 一场一个文件：时间_祖巫_房间_结果.json，排版成多行
        self.assertRegex(file.name, r"^\d{4}-\d\d-\d\d_\d{6}_.+_c_done\.json$")
        text = file.read_text(encoding="utf-8").splitlines()
        self.assertTrue(len(text) > 20 and max(map(len, text)) < 400, (len(text), max(map(len, text))))
        (line,) = saved()
        self.assertEqual([(w["uid"], w.get("name")) for w in line["players"]], [(1, "甲"), (7, None)])   # 进房资料和榜单上的同一个人并成一条
        self.assertEqual((line["boss"], line["room"], line["outcome"], line["hpMax"], line["me"]["power"]), (960101, "c", "done", HP, 1000))
        self.assertEqual(line["finish"], {"success": True})
        other, me = sorted(line["players"], key=lambda w: w["uid"])
        self.assertEqual((other["info"]["userdata"], other["power"], other["board"], other["track"]), (inside[0]["userdata"], 5000, "low", [[other["track"][0][0], 6.5]]))
        self.assertFalse(other["me"])
        self.assertTrue(me["me"] and len(me["track"]) > 3 and me["track"][-1][1] >= 5)   # 我的占比一路涨上去，变了才记
        self.assertEqual(me["track"], sorted(me["track"]))
        marks = [t for t, _share, _gain in me["every"]]             # 每 10 秒一笔：第 10、20、30…秒，最后不满 10 秒的补一笔
        self.assertEqual(marks[:3], [10, 20, 30])
        self.assertTrue(len(marks) >= 4 and marks == sorted(marks) and marks[-1] == line["seconds"], (marks, line["seconds"]))
        self.assertAlmostEqual(sum(gain for _t, _share, gain in me["every"]), me["every"][-1][1], places=3)   # 每段涨的加起来 = 最后的累计
        self.assertEqual([row[1:] for row in other["every"][:2]], [[6.5, 6.5], [6.5, 0.0]])                  # 别人：一直是 6.5%，后面几段没涨
        self.assertEqual((line["listed"]["roomId"], len(line["rooms"]), line["joined"]["roomId"], "message" in line["joined"]), ("c", 6, "c", False))
        self.assertAlmostEqual(sum(row[1] for row in line["sent"]), line["selfHit"][-1][1]["hit"])              # 我报的加起来 = 服务器回给我的累计
        self.assertTrue(len(line["sent"]) == len(line["selfHit"]) > 3 and all(len(row) == 2 for row in line["sent"]))   # 应答里只有 code：不多记
        self.assertTrue(len(line["plan"]) == 1 and line["plan"][0][1] == line["me"]["floor"] and "正常输出" in line["plan"][0][2], line["plan"])
        with mock.patch.object(Session, "refresh", lambda s: s.info):
            self.world(inside=people(1500, 2000, 3000, 800), ranks=board((1, 3), (2, 4), (3, 4)), rooms=self.no_new_rooms)
        self.assertEqual(sorted((ln["room"], ln["outcome"]) for ln in saved() if ln["outcome"] != "done"), [(r, "quit") for r in "abc"])  # 换了几个房间就记几个文件（同一瞬间写的，不比先后）

    def test_log_line_lists_other_players_and_boss_hp(self):
        room = b.Room(HP, 7, 1000, 2000, 5.5, 1.5)
        self.assertEqual(room.roster(), "Boss 血量未知；其他玩家：还没看到")
        room.absorb(boss_rules.P_JOIN, people(15000, 800, 30000) + [{"uid": 5, "userdata": {}}], 0)   # 3 号在高战榜
        room.absorb(boss_rules.P_HP, {"hp": HP * 0.625}, 0)
        room.absorb(boss_rules.P_RANK, {"type": 2, "low": [{"uid": 2, "rank": 1, "percent": 12.5}, {"uid": 7, "rank": 2, "percent": 6}, {"uid": 1, "rank": 3, "percent": 3}],
                               "high": [{"uid": 3, "rank": 1, "percent": 20}]}, 0)
        self.assertEqual(room.roster(), "Boss 剩 62.5%；其他玩家：0.1万 12.50%、1.5万 3.00%、别榜 3.0万 20.00%")   # 只列榜上的人；5 号还没上榜
        self.assertEqual(room.roster(most=2), "Boss 剩 62.5%；其他玩家：0.1万 12.50%、1.5万 3.00% 等 3 人")

    def test_dying_boss_and_not_in_top_three_means_leave(self):
        room = b.Room(HP, 7, 1000, None, 5.5, 1.5, hp=HP * 0.04, pct=2)
        self.assertFalse(room.lost())                     # 榜单还没推过来：不知道名次，不退
        room.absorb(boss_rules.P_RANK, board((1, 9), (2, 8), (3, 7))(HP * 0.02), 0)
        self.assertTrue(room.lost())                      # 第 4 名
        room.hp = HP * 0.06
        self.assertFalse(room.lost())                     # 血量还在 5% 以上：接着打
        room.hp = HP * 0.04
        room.absorb(boss_rules.P_RANK, board((1, 9), (2, 8), (3, 1))(HP * 0.02), 0)
        self.assertFalse(room.lost())                     # 第 3 名

    def test_room_reads_percent_only_pushes_and_leavers(self):
        room = b.Room(HP, 7, None, None, 5.5, 0.3)
        room.absorb(boss_rules.P_SELF, {"uid": 7, "percent": 3.25}, 0)
        self.assertEqual(room.pct, 3.25)
        room.absorb(boss_rules.P_JOIN, people(10, 20, 30), 0)
        room.absorb(boss_rules.P_LEAVE, {"uid": 2}, 0)
        self.assertEqual((sorted(room.powers), room.rivals()), ([1, 3], {}))   # 没上榜的人不算对手
        room.absorb(boss_rules.P_HP, {"hp": HP}, 0)
        room.absorb(boss_rules.P_HP, {"hp": HP * 0.5}, 50)
        self.assertAlmostEqual(room.eta(50), 50)
        room.absorb(boss_rules.P_HP, {"hp": HP * 0.2}, 100)          # 后来掉得快了：只看最近 60 秒（50 秒掉了 30%），不是进房以来的平均
        self.assertAlmostEqual(room.eta(100), 0.2 / (0.3 / 50))

    def test_result_says_whether_the_rank_reward_came(self):
        won = {"success": True, "hit": HP * 0.06, "hpMax": HP, "rank": 2, "rankReward": {"type": 1, "subType": "1", "count": "1"}}
        self.assertIn("占比 6.00%，普通榜第 2 名，已得排名奖励", self.world(finish=won)[1].detail)
        self.assertIn("没有排名奖励（不在前 3）", self.world(finish={"success": True, "hit": HP * 0.06, "hpMax": HP, "rank": 4})[1].detail)
        self.assertIn("Boss 未被击败", self.world(finish={"success": False})[1].detail)

    def test_catalog_comes_from_the_table(self):
        names = b.world_bosses()
        self.assertEqual(len(names), 15)
        self.assertEqual(names[0][0], 960101)

    def test_picker_hides_bosses_above_role_level(self):
        shown = lambda lv: [i for i, _ in b.world_bosses() if b.available(i, {b.PROFILE_KEY: lv} if lv is not None else {})]
        self.assertEqual(len(shown(None)), 15)          # 没读过等级：全部放行
        self.assertEqual(shown(29), [])
        self.assertEqual(len(shown(60)), 6)              # 30/36/42/48/54/60
        self.assertEqual(len(shown(144)), 15)

    def test_run_skips_boss_above_role_level(self):
        conn, res = self.world(info={"worldBoss": {"count": 3}, "p1": {"level": 40}}, options={"levels": [960801]})
        self.assertEqual(res.status, "skipped")
        self.assertEqual(conn.bodies("joinWorldBossCopy"), [])
        self.assertEqual(res.profile, {b.PROFILE_KEY: 40})


if __name__ == "__main__":
    unittest.main()
