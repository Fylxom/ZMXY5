"""斗神大会、灵宠乱斗（周常页）。全部离线：推送用脚本化的假连接。"""

import contextlib
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from zmxy import state, protocol as p
from zmxy.core import Session, UncertainOutcome, load_tasks, stop_after
from zmxy.tasks import arena_apply, pet_brawl, pvp_match
from tests.support import Fake, run

OPEN = int(datetime(2026, 10, 3, 12, 30, tzinfo=state._CN).timestamp())   # 周六中午：两个玩法都开放
CLOSED = int(datetime(2026, 10, 3, 16, 0, tzinfo=state._CN).timestamp())


class Scripted(Fake):
    """poll_push 按脚本逐条吐推送；脚本用完后一直返回 None。"""

    def __init__(self, handler=None, script=()):
        super().__init__(handler)
        self.script = list(script)

    def poll_push(self, routes, timeout):
        while self.script:
            route, body = self.script.pop(0)
            if route in routes:
                return SimpleNamespace(route=route, body=body)
        return None

    def notify(self, route, body):
        self.sent.append((route, body))


class FakeTeam(Fake):
    """战斗服务器（代替 core.TeamLink）：先给 onAllLoadComplete，再给几帧 onSendFrame；notify 只记录。
    settles：提交 battleExit 后服务器推 onPVPFinish。"""

    def __init__(self, settles=True):
        super().__init__()
        self.settles = settles
        self.script = [("onAllLoadComplete", {})]
        self.pushes = [SimpleNamespace(route="onSendFrame", body={})] * 3

    def poll_push(self, routes, timeout):  # 和 TeamLink.poll_push 一样返回 (路由, 消息体)
        while self.script:
            route, body = self.script.pop(0)
            if route in routes:
                return route, body
        return None

    def request(self, route, body):
        out = super().request(route, body)
        if route.endswith("battleExit") and self.settles:
            self.script.append(("onPVPFinish", {"win": 0}))
        return out

    def notify(self, route, body):
        self.sent.append((route, body))

    def drain_pushes(self):
        out, self.pushes = self.pushes, []
        return out

    def ask(self, route, body):  # 真机：ping 只回 {time}，没有 code
        self.sent.append((route, body))
        return {"time": body["time"]}


INFO = {"uid": 7, "vip": {"level": 6}, "everyDaySetting": {"pvpProtectNum": 0}, "pvpSeason": {"level": 1, "score": 500},
        "clientStore": {"r1-setKeyboard-0": '{"skill1": 1104010, "Item1": 101049005}'}}  # VIP6 上限 2，已用 0；没带二转技能


def setUpModule():  # 不联网：线上版本固定成和本地表一样（灵宠宝石周）
    from zmxy import online
    patcher = mock.patch.dict(online._current, {"cg": online.DEFAULT_CG, "live": True, "at": 1e12}, clear=True)
    patcher.start()
    from zmxy.core import Session
    other = mock.patch.object(Session, "record_id", new_callable=mock.PropertyMock, return_value="r1")
    other.start()
    unittest.addModuleCleanup(other.stop)
    unittest.addModuleCleanup(patcher.stop)
    no_download = mock.patch("zmxy.online._fetch_bytes", lambda url: None)  # 线上数据表包下载不到 → 走推算
    no_download.start()
    unittest.addModuleCleanup(no_download.stop)


def amf(value) -> bytes:
    """测试用的最小 AMF3 编码（不用引用表）。"""
    def u29(n):
        out = [n & 0x7F]
        while n := n >> 7:
            out.append(0x80 | (n & 0x7F))
        return bytes(reversed(out))

    def text(t):
        raw = t.encode()
        return u29(len(raw) << 1 | 1) + raw
    if value is None:
        return bytes([1])
    if isinstance(value, int):
        return bytes([4]) + u29(value)
    if isinstance(value, str):
        return bytes([6]) + text(value)
    if isinstance(value, bytes):
        return bytes([12]) + u29(len(value) << 1 | 1) + value
    return bytes([10, 11, 1]) + b"".join(text(k) + amf(v) for k, v in value.items()) + bytes([1])


def fz_blob(tables: dict) -> bytes:
    import lzma
    import zlib
    out = amf({"fn": len(tables)})
    for name, rows in tables.items():
        data = lzma.compress(amf(rows), lzma.FORMAT_ALONE)
        raw = zlib.compressobj(wbits=-15)
        out += amf(raw.compress(amf({"prefixName": name, "ver": 2, "data": data})) + raw.flush())
    return out


def tick():
    """让 time.monotonic 每次走 40 秒：超时分支几次循环就到。"""
    t = [0.0]

    def now():
        t[0] += 40.0
        return t[0]
    return now


class PvpMatchTests(unittest.TestCase):
    def test_automatic_tasks_do_not_declare_a_run_count_field(self):
        for task_id in ("pvp_match", "pet_brawl"):
            spec = load_tasks()[task_id]
            self.assertEqual(spec.times_max, 0)
            self.assertTrue(spec.long_running)

    def test_robot_is_settled_as_a_win_without_fighting(self):
        conn, stop = self.one_robot(lambda route, body: {"robot": {"entityId": "e9", "name": "机器人"}, "self": {}} if route.endswith("matchStart") else {"addScore": 100})
        r = run("pvp_match", conn, INFO, now=OPEN, stop=stop)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("robotPVPFinish"), [{"win": 1, "name": "机器人", "entityId": "e9"}])
        self.assertNotIn("battleExit", conn.routes())

    ROBOT = staticmethod(lambda route, body: {"robot": {"entityId": "e9", "name": "r"}, "addScore": 100} if route.endswith("matchStart") else ({"addScore": 100} if route.endswith("robotPVPFinish") else None))

    @staticmethod
    def info(score=500, used=0, vip=6, **more):
        info = {"uid": 7, "vip": {"level": vip}, "pvpSeason": {"level": 1, "score": score}, "clientStore": {}, **more}
        if used is not None:
            info["everyDaySetting"] = {"pvpProtectNum": used}
        return info

    @staticmethod
    def one_robot(handler):
        """打完一场人机就收手（“打完这场再停”）：次数框取消后，不收手会一直赢下去。"""
        stop = threading.Event()

        def answer(route, body):
            if route.endswith("robotPVPFinish"):
                stop_after(stop)
            return handler(route, body)
        return Scripted(answer), stop

    def blocked_by_skills(self, store):
        conn = Scripted(self.ROBOT)
        r = run("pvp_match", conn, {**INFO, "clientStore": store}, {"times": 1}, now=OPEN)
        self.assertEqual(conn.bodies("matchStart"), [], r.detail)
        return r

    def test_second_job_skill_on_the_keys_means_no_match(self):
        r = self.blocked_by_skills({"r1-setKeyboard-0": '{"skill1": 1104010, "skill2": "1104090"}'})   # 风之守护
        self.assertEqual(r.status, "blocked")
        self.assertIn("二转技能（风之守护）", r.detail)
        r = self.blocked_by_skills({"r1-setKeyboard-0": {"skill3": 1105120}})                          # 值已经是对象也认
        self.assertIn("狂战巨碑", r.detail)

    def test_pvp_key_set_wins_over_the_everyday_one(self):
        r = self.blocked_by_skills({"r1-setKeyboard-0": '{"skill1": 1104010}', "r1-setKeyboard_ds-0": '{"skill1": 1104100}'})
        self.assertIn("风魔眼", r.detail)
        store = {"r1-setKeyboard-0": '{"skill1": 1104100}', "r1-setKeyboard_ds-0": '{"skill1": 1104010}'}  # 平时带二转，斗神设置没带
        self.assertEqual(self.robot_run(store).status, "submitted")
        store["r1-setKeyboard_ds-0"] = "{}"                                                            # 斗神设置是空的 → 看平时的
        self.assertEqual(self.blocked_by_skills(store).status, "blocked")

    def test_other_saves_of_the_same_account_are_ignored(self):
        store = {"r1-setKeyboard-0": '{"skill1": 1104010}', "r2-setKeyboard-0": '{"skill1": 1104090}', "r2-setKeyboard_ds-0": '{"skill1": 1104090}'}
        self.assertEqual(self.robot_run(store).status, "submitted")

    def test_only_the_fighting_role_is_checked(self):
        store = {"r1-setKeyboard-0": '{"skill1": 1104010}', "r1-setKeyboard-1": '{"skill1": 1104090}'}
        self.assertEqual(self.robot_run(store).status, "submitted")                                    # 2 号带二转，但出战的是 1 号
        store["r1-setNotJoin"] = '{"pNum": 1}'                                                         # 1 号不出战 → 看 2 号
        self.assertEqual(self.blocked_by_skills(store).status, "blocked")
        store["r1-setNotJoin"] = '{"pNum": 2}'
        self.assertEqual(self.robot_run(store).status, "submitted")

    def test_unreadable_key_settings_mean_no_match(self):
        info = {k: v for k, v in INFO.items() if k != "clientStore"}
        conn = Scripted(self.ROBOT)
        r = run("pvp_match", conn, info, {"times": 1}, now=OPEN)
        self.assertEqual((r.status, conn.bodies("matchStart")), ("blocked", []))
        self.assertEqual(self.blocked_by_skills({"r1-setKeyboard-0": "not json"}).status, "blocked")
        self.assertEqual(self.robot_run({}).status, "submitted")                                       # 从没存过按键设置 = 默认技能

    def robot_run(self, store):
        conn, stop = self.one_robot(self.ROBOT)
        return run("pvp_match", conn, {**INFO, "clientStore": store}, now=OPEN, stop=stop)

    def test_no_protection_left_means_no_match(self):
        conn = Scripted(self.ROBOT)
        r = run("pvp_match", conn, self.info(used=2), {"times": 3}, now=OPEN)  # VIP6 上限 2，今日已用 2
        self.assertEqual(r.status, "blocked")
        self.assertIn("保护次数已用完", r.detail)
        self.assertEqual(conn.bodies("matchStart"), [])

    def test_stops_when_protection_runs_out_between_matches(self):
        # 真人局我们一定输：每场后已用 +1。上限 2、已用 0 → 打两场 → 用完停
        with mock.patch.object(pvp_match, "fight_human", lambda ctx, uid, season=None: (pvp_match.absorb(season, {}, True), "真人")[1]):
            conn = Scripted(self.ROBOT_HUMAN)
            r = run("pvp_match", conn, self.info(), {"times": 1}, now=OPEN)  # 旧次数不再截断，仍打完两次保护
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(len(conn.bodies("matchStart")), 2)
        self.assertIn("保护次数已用完", r.detail)

    ROBOT_HUMAN = staticmethod(lambda route, body: {"queue": 1} if route.endswith("matchStart") else None)

    def test_robot_win_adds_score_and_keeps_protection(self):
        conn = Scripted(self.ROBOT)
        r = run("pvp_match", conn, self.info(score=13900), {"times": 3}, now=OPEN)
        # 13900 → 14000（第一场 +100）就够了上限：第二场前拒绝
        self.assertEqual(len(conn.bodies("matchStart")), 1)
        self.assertIn("匹配不到人机", r.detail)

    def test_missing_season_or_protection_blocks_instead_of_guessing(self):
        for info in ({"uid": 7}, self.info(used=None), self.info(vip=None)):
            conn = Scripted(self.ROBOT)
            r = run("pvp_match", conn, info, {"times": 1}, now=OPEN)
            self.assertEqual(r.status, "blocked")
            self.assertEqual(conn.bodies("matchStart"), [])

    def test_score_with_no_robots_is_refused(self):
        conn = Scripted(self.ROBOT)
        r = run("pvp_match", conn, self.info(score=14000), {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked")
        self.assertIn("匹配不到人机", r.detail)
        self.assertEqual(conn.bodies("matchStart"), [])
        r = run("pvp_match", Scripted(self.ROBOT), self.info(score=13999), {"times": 1}, now=OPEN)  # 差 1 分还有人机
        self.assertEqual(r.status, "submitted", r.detail)

    def test_server_season_in_the_reply_wins_over_local_guess(self):
        season = {"score": 500, "protectUsed": 0}
        pvp_match.absorb(season, {"pvpSeason": {"score": 450}}, True)
        self.assertEqual(season, {"score": 450, "protectUsed": 1})

    @staticmethod
    def online_cg(built, live=True):
        from zmxy import online
        return mock.patch.dict(online._current, {"cg": f"assets/data/cg1_{built}_1.swf", "live": live}, clear=True)

    @staticmethod
    def at(m, d, h=12):
        return int(datetime(2026, m, d, h, tzinfo=state._CN).timestamp())

    def test_table_says_which_growth_week_its_version_is(self):
        # cg1_20260928 的表：翅膀 9/14、装备 9/21、幻兽 9/28 都已下架，灵宠宝石到 10/8 → 第 3 周
        self.assertEqual(state.growth_table_week(), (3, datetime(2026, 10, 8, 23, 59, 59)))
        with mock.patch("zmxy.state.table_rows", lambda name: []):
            self.assertIsNone(state.growth_table_week())

    def test_growth_week_advances_one_per_newer_online_version(self):
        from zmxy import online
        cases = [("20260928", 3),   # 线上 == 本地表：灵宠宝石
                 ("20261007", 4),   # 节假日顺延、周三构建的下一版：翅膀光环
                 ("20261005", 4), ("20261003", 4),  # 构建日期不一定是周一
                 ("20261012", 1), ("20261019", 2), ("20261026", 3), ("20261102", 4),
                 ("20260921", 2)]   # 线上比本地表旧一版
        for built, week in cases:
            with self.online_cg(built):
                self.assertEqual(online.growth_week(self.at(10, 20)), week, built)
        with self.online_cg("20260928"):                 # 过了表里的结束时间但线上没更新：没更新就没换
            self.assertEqual(online.growth_week(self.at(10, 9)), 3)

    @staticmethod
    def activity(ends):
        return {str(i): {"id": i, "name": name + "养成", "underTime": end}
                for i, (name, end) in enumerate(zip(state.GROWTH_WEEKS, ends), 1)}

    def test_fz_package_round_trip(self):
        from zmxy import fz
        blob = fz_blob({"Other": {"1": {"a": 5}}, "Activity": self.activity(["2026/10/19-23:59:59"] * 4)})
        self.assertEqual([k for k, _ in fz.tables(blob)], ["Other", "Activity"])
        self.assertEqual(fz.table(blob, "Other"), {"1": {"a": 5}})
        self.assertIsNone(fz.table(blob, "Missing"))
        with self.assertRaises(fz.FzError):
            fz.table(b"CWS" + bytes([14, 37, 113, 0, 0]), "Activity")           # 没带 Referer 拿到的占位 swf

    def test_growth_week_is_read_from_the_online_versions_own_table(self):
        from zmxy import online
        # 线上已是 10-07 构建的新版，它自己的表写着：装备宝石到 10/19 最晚 → 第 1 周（按构建日期推算会得出第 4 周）
        blob = fz_blob({"Activity": self.activity(["2026/10/19-23:59:59", "2026/9/28-23:59:59", "2026/10/8-23:59:59", "2026/9/14-23:59:59"])})
        asked = []

        def fetch_bytes(url):
            asked.append(url)
            return blob
        with self.online_cg("20261007"), mock.patch.dict(online._weeks, clear=True):
            self.assertEqual(online.growth_week(self.at(10, 9), fetch_bytes=fetch_bytes), 1)
            self.assertEqual(online.growth_week(self.at(10, 9), fetch_bytes=fetch_bytes), 1)
            self.assertEqual(asked, [online.RES_BASE + "cg1_20261007_1.swf"])   # 同一版本只下载一次
        with self.online_cg("20261007"), mock.patch.dict(online._weeks, clear=True):
            self.assertEqual(online.growth_week(self.at(10, 9), fetch_bytes=lambda url: b"garbage"), 4)  # 解不开 → 退回推算
        with self.online_cg("20260928"), mock.patch.dict(online._weeks, clear=True):
            self.assertEqual(online.growth_week(self.at(10, 4), fetch_bytes=lambda url: 1 / 0), 3)       # 和本地表同版：不下载

    def test_growth_week_without_the_online_version(self):
        from zmxy import online
        with self.online_cg("20260928", live=False):
            self.assertEqual(online.growth_week(self.at(10, 4)), 3)      # 还在表里那一周的期内：用表里的
            self.assertIsNone(online.growth_week(self.at(10, 9)))        # 过期了又读不到线上版本：不知道
            self.assertIsNone(online.growth_week(None))

    def test_interface_reads_the_week_without_waiting_for_a_reload(self):
        """界面线程问养成周（known_growth_week）：不联网，后台正在重读（占着锁）时也立刻回答。"""
        import threading
        from zmxy import online
        with self.online_cg("20260928"):
            held, release = threading.Event(), threading.Event()

            def reload():
                with online._reading:
                    held.set()
                    release.wait(5)
            worker = threading.Thread(target=reload)
            worker.start()
            self.assertTrue(held.wait(5))
            try:
                self.assertEqual(online.known_growth_week(self.at(10, 4)), 3)
            finally:
                release.set()
                worker.join()
        with mock.patch.dict(online._current, clear=True):
            self.assertIsNone(online.known_growth_week(self.at(10, 4)))               # 这次运行还没读过
        with mock.patch.dict(online._current, {"cg": "assets/data/cg1_20260928_1.swf", "live": True, "at": 0}, clear=True):
            self.assertIsNone(online.known_growth_week(self.at(10, 4)))               # 读过但过期了
        with self.online_cg("20261007"), mock.patch.dict(online._weeks, clear=True):
            self.assertIsNone(online.known_growth_week(self.at(10, 9)))               # 线上是新版，它的表还没解过：要下载，不在界面线程里做
            online._weeks["assets/data/cg1_20261007_1.swf"] = (4, datetime(2026, 10, 12, 23, 59, 59))
            self.assertEqual(online.known_growth_week(self.at(10, 9)), 4)

    def test_notices_when_the_game_has_moved_on_to_a_newer_version(self):
        from zmxy import online
        for built, live, want in (("20261007", True, "cg1_20261007_1"),   # 程序开着跨过了游戏更新
                                  ("20260928", True, None),                # 同一版
                                  ("20260921", True, None),                # 线上比本地旧：不算
                                  ("20261007", False, None)):              # 没读到线上版本
            with self.online_cg(built, live):
                self.assertEqual(online.newer_online(), want, built)
        with mock.patch.dict(online._current, clear=True):
            self.assertIsNone(online.newer_online())

    def test_cap_is_vip_plus_the_bonus_only_in_the_wing_week(self):
        ctx = SimpleNamespace(info=self.info(vip=4), now=self.at(10, 4), log=lambda m: None)
        with self.online_cg("20260928"):
            self.assertEqual(pvp_match.protect_cap(ctx), 1)       # 灵宠宝石周（用户确认这周不加）
        ctx.now = self.at(10, 9)
        with self.online_cg("20261007"):
            self.assertEqual(pvp_match.protect_cap(ctx), 2)       # 翅膀光环周：+1
        with self.online_cg("20260928", live=False):
            self.assertEqual(pvp_match.protect_cap(ctx), 1)       # 判断不了：不加
        with self.online_cg("20261012"):
            self.assertEqual(pvp_match.protect_cap(ctx), 1)       # 装备宝石周
        ctx.info = self.info(vip=0)
        with self.online_cg("20261007"):
            self.assertEqual(pvp_match.protect_cap(ctx), 1)       # VIP0：0 + 1

    def test_no_robot_threshold_comes_from_the_grade_table(self):
        self.assertEqual(pvp_match.no_robot_score(), 14000)

    def test_robot_without_entity_id_is_not_settled(self):
        conn = Scripted(lambda route, body: {"robot": {"name": "x"}} if route.endswith("matchStart") else None)
        r = run("pvp_match", conn, INFO, {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.bodies("robotPVPFinish"), [])

    def test_outside_open_hours_does_nothing(self):
        conn = Scripted()
        r = run("pvp_match", conn, INFO, {"times": 1}, now=CLOSED)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_legacy_times_are_ignored_and_all_remaining_protection_is_used(self):
        for options in ({}, {"times": ""}, {"times": 0}, {"times": 1}, {"times": "bad"}):
            with self.subTest(options=options):
                conn = Scripted(self.ROBOT_HUMAN)
                with mock.patch.object(pvp_match, "fight_human", lambda ctx, uid, season=None: (pvp_match.absorb(season, {}, True), "真人")[1]):
                    r = run("pvp_match", conn, INFO, options, now=OPEN)
                self.assertEqual(r.status, "submitted", r.detail)
                self.assertEqual(len(conn.bodies("matchStart")), 2)
                self.assertIn("保护次数已用完", r.detail)

    def test_robot_matches_do_not_use_up_the_automatic_protection_target(self):
        expected = ["robot"] * 21 + ["human", "human"]  # 人机不耗保护，可以超过原来的 20 场上限
        kinds = iter(expected)
        seen = []

        def match(ctx, uid, season):
            kind = next(kinds)
            seen.append(kind)
            pvp_match.absorb(season, {"addScore": 100} if kind == "robot" else {}, kind == "human")
            return kind

        with mock.patch.object(pvp_match, "one_match", match):
            r = run("pvp_match", Scripted(), INFO, now=OPEN)
        self.assertEqual(seen, expected)
        self.assertIn("完成 23 场", r.detail)
        self.assertIn("保护次数已用完", r.detail)

    def test_auto_respects_stop_after_current_match(self):
        for finish_current in (False, True):
            with self.subTest(finish_current=finish_current):
                stop = threading.Event()

                def match(ctx, uid, season):
                    pvp_match.absorb(season, {}, True)
                    stop_after(stop) if finish_current else stop.set()
                    return "真人"

                with mock.patch.object(pvp_match, "one_match", side_effect=match) as match_call:
                    r = run("pvp_match", Scripted(), INFO, now=OPEN, stop=stop)
                self.assertEqual(match_call.call_count, 1)
                self.assertIn("停止", r.detail)

    def test_unchanged_settlement_does_not_match_forever(self):
        with mock.patch.object(pvp_match, "one_match", return_value="人机，已结算") as match:
            r = run("pvp_match", Scripted(), INFO, now=OPEN)
        self.assertEqual(match.call_count, 1)
        self.assertIn("积分和保护次数都没有变化", r.detail)

    CREATE = {"ip": "10.0.0.1", "port": 9000, "instId": 5, "password": "", "dungeonId": 1,
              "playerInfos": [{"playerId": 7, "hp": 100}, {"playerId": 8, "hp": 100}]}

    def human(self, tail=(), frames=None, head=(), team_tail=(), settles=True, **consts):
        conn = Scripted(script=[*head, ("onPlayerJoinWaitingRoom", {"room": {"roomId": "r1"}}),
                                ("onPrepareCreateOnlineCopy", {"dungeonId": 1, "roomId": "r1"}),
                                ("onCreateOnlineCopy", self.CREATE), *tail])
        team = FakeTeam(settles)
        team.script += team_tail
        if frames is not None:
            team.pushes = [SimpleNamespace(route="onSendFrame", body=b) for b in frames]

        @contextlib.contextmanager
        def open_team(self, host, port):
            yield team

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(Session, "open_team", open_team))
            stack.enter_context(mock.patch("zmxy.online.client_cg", lambda now=None, fetch=None: "assets/data/cg1_test_1.swf"))  # 不联网
            for name, value in ({"FINISH_WAIT": 1.0} | consts).items():
                stack.enter_context(mock.patch(f"zmxy.tasks.pvp_match.{name}", value))
            r = run("pvp_match", conn, {**INFO, "everyDaySetting": {"pvpProtectNum": 1}}, now=OPEN)  # 只剩一次保护，测这一场
        return conn, team, r

    def test_server_finish_means_no_battle_exit(self):
        conn, team, r = self.human(team_tail=[("onPVPFinish", {"win": 0})])  # 服务器已结算：再提交会被踢
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(team.routes(), ["enter", "loadComplete", "keyOperation"])

    def test_finish_while_loading_means_no_ready_and_no_battle_exit(self):
        conn, team, r = self.human([("onPVPFinish", {"win": 0})])  # 结算推送走主连接，加载阶段就到了
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(team.routes(), ["enter", "loadComplete"])

    def test_no_settlement_after_battle_exit_is_uncertain(self):
        with self.assertRaises(UncertainOutcome):
            self.human(settles=False, TIME_LIMIT_FRAMES=2, FINISH_WAIT=0.0)

    def test_leave_push_of_the_previous_room_is_ignored(self):
        # 真机：上一局结算后才推到“对手离开旧等待房间”，正好落在下一次匹配的排队里
        stale = ("onPlayerLeaveWaitingRoom", {"exitPlayerId": "8", "room": {"roomId": "r0"}})
        conn, team, r = self.human([("onPVPFinish", {"win": 0})], head=[stale])
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertNotIn("matchCancel", conn.routes())

    def test_opponent_leaving_the_current_room_stops_the_match(self):
        conn = Scripted(script=[("onPlayerJoinWaitingRoom", {"room": {"roomId": "r1"}}),
                                ("onPlayerLeaveWaitingRoom", {"exitPlayerId": "8", "room": {"roomId": "r1"}})])
        with mock.patch("zmxy.online.client_cg", lambda now=None, fetch=None: "assets/data/cg1_test_1.swf"):
            r = run("pvp_match", conn, INFO, {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked")
        self.assertIn("matchCancel", conn.routes())

    def test_enter_uses_the_current_client_cg(self):
        conn, team, r = self.human(TIME_LIMIT_FRAMES=2)
        self.assertEqual(team.bodies("enter")[0]["cg"], "assets/data/cg1_test_1.swf")

    def test_client_cg_picks_the_newest_published_resource_list(self):
        from zmxy import online
        xml = lambda v: f"<item><name>config</name><url>assets/data/cg1_{v}.swf</url><type>fz</type></item>"
        seen = []

        def fetch(url):
            seen.append(url)  # 2026-10-04 是周日：最近的周一是 09-28
            return xml("20260928_1") if "gameresources_20260928_1.xml" in url else (xml("20260921_1") if "20260921_1" in url else None)

        with mock.patch.dict(online._current, clear=True):
            self.assertEqual(online.client_cg(OPEN, fetch), "assets/data/cg1_20260928_1.swf")
            self.assertEqual(online.client_cg(OPEN, lambda u: 1 / 0), "assets/data/cg1_20260928_1.swf")  # 同一进程只查一次
        self.assertTrue(seen[0].endswith("gameresources_20261001_3.xml") or "2026100" in seen[0] or "2026093" in seen[0])
        self.assertLess(len(seen), 25)  # 新的在前：不会把几周的候选全试一遍

    def test_client_cg_falls_back_when_nothing_is_published(self):
        from zmxy import online
        with mock.patch.dict(online._current, clear=True):
            self.assertEqual(online.client_cg(OPEN, lambda url: None), online.DEFAULT_CG)
        with mock.patch.dict(online._current, clear=True):  # 内容不对（不是 cg1_日期.swf）也不信
            self.assertEqual(online.client_cg(OPEN, lambda url: "<name>config</name><url>evil.swf</url>"), online.DEFAULT_CG)

    def test_replay_cg_is_checked_after_a_human_match(self):
        from zmxy import online
        conn, team, r = self.human([("onPVPFinish", {"reportId": "pvpX"})])
        self.assertEqual(conn.bodies("getReport"), [{"reportId": "pvpX"}])  # 只读：看服务器存的 cg
        self.assertIn("cg1_", online.DEFAULT_CG)  # 由数据表目录名推出，不再写死

    def test_human_idles_until_time_is_up_then_reports_a_loss(self):
        conn, team, r = self.human(TIME_LIMIT_FRAMES=2)  # 3 条帧推送 > 上限 2 → “时间到”
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([x for x in conn.sent if x[0].endswith("loadingProgress")][0][1], {"roomId": "r1", "percent": 100, "version": 1})
        self.assertEqual(team.routes(), ["enter", "loadComplete", "keyOperation", "battleExit"])  # 和别的在线副本一样先报加载、准备
        self.assertEqual(team.bodies("keyOperation")[0]["objs"], [{"ready": True, "uid": 0, "teamPs": 0}])
        exit_body = team.bodies("battleExit")[0]
        self.assertEqual(exit_body["frameNum"], 3)  # 本端收到的帧数 × 每帧逻辑帧数（缺省 1），不是 0
        self.assertEqual(exit_body["battleResult"], 4)
        self.assertEqual([(x["playerId"], x["dead"], x["exit"]) for x in exit_body["playerInfo"]], [(7, False, False), (8, False, False)])
        self.assertIn("exitWaitingRoom", conn.routes())

    def test_opponent_finishing_is_reported_as_being_killed(self):
        # 真机：对手打死我方并结算后，帧里出现对手 connected=false；报胜会变成“战斗异常”，要报输
        frames = [{"serFrameNum": 41, "frames": [{"currFrame": 40, "status": [{"connected": False, "battlePos": 1}]}]}]
        conn, team, r = self.human(frames=frames)
        self.assertEqual(r.status, "submitted", r.detail)
        exit_body = team.bodies("battleExit")[0]
        self.assertEqual((exit_body["battleResult"], exit_body["frameNum"]), (4, 40))
        self.assertEqual([(x["playerId"], x["hp"], x["dead"], x["exit"]) for x in exit_body["playerInfo"]],
                         [(7, 0, True, False), (8, 100, False, False)])

    def test_battle_server_is_pinged_like_the_client(self):
        conn, team, r = self.human(TIME_LIMIT_FRAMES=2, BATTLE_PING_GAP=0.0)
        self.assertIn("ping", team.routes())
        self.assertEqual(team.bodies("keyOperation")[1]["objs"][0]["ping"], True)

    def test_queue_timeout_cancels_the_match(self):
        conn = Scripted()
        with mock.patch("time.monotonic", tick()):
            r = run("pvp_match", conn, INFO, {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked")
        self.assertIn("matchCancel", conn.routes())


class PetBrawlTests(unittest.TestCase):
    CAP = pet_brawl.cap_of()

    def info(self, got=0):
        return {"uid": 7, "everyDaySetting": {"addPetPvpM": got}}

    def test_cap_comes_from_the_table(self):
        self.assertEqual(self.CAP, 20)

    def test_already_at_cap_skips(self):
        conn = Scripted()
        r = run("pet_brawl", conn, self.info(self.CAP), now=OPEN)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_outside_open_hours_does_nothing(self):
        conn = Scripted()
        r = run("pet_brawl", conn, self.info(), now=CLOSED)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_afk_match_reports_ready_and_loading_then_stops_at_cap(self):
        conn = Scripted(script=[
            ("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
            ("onPetPvpBtlChangeState", {"btlStage": 0}),
            ("onPetPvpBtlChangeState", {"btlStage": 1}),
            ("onPetPvpBtlChangeState", {"btlStage": 2, "session": 1, "players": [{"uid": "9"}, {"uid": "7"}]}),
            ("onPetPVPFinish", {"addPetBtlM": 20, "everyDaySetting": {"addPetPvpM": self.CAP}})])
        with mock.patch("zmxy.tasks.pet_brawl.LOAD_DELAY", 0.0), mock.patch("zmxy.tasks.pet_brawl.FIGHT_DELAY", 0.0):
            r = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("matchStart"), [{}])
        self.assertEqual(conn.bodies("loadingEnd")[0], {"roomId": "0$1"})
        self.assertEqual(conn.bodies("changePlayerStatus"), [{"roomId": "0$1", "status": 4}])  # 准备好只报一次
        self.assertEqual(conn.bodies("setBtlResult"), [{"roomId": "0$1", "btlResult": 4, "hps": [1.0, 0.0]}])  # 每局按输上报，hps 按玩家顺序
        order = [r.rsplit(".", 1)[-1] for r in conn.routes()]
        self.assertLess(order.index("changePlayerStatus"), order.index("loadingEnd"))
        self.assertEqual(conn.routes().count("matchStart"), 1)  # 到上限就不再开
        self.assertIn(f"{self.CAP}/{self.CAP}", r.detail)

    def test_legacy_times_are_ignored_and_coins_are_filled(self):
        for options in ({}, {"times": ""}, {"times": 0}, {"times": 1}, {"times": "bad"}):
            with self.subTest(options=options):
                with mock.patch.object(pet_brawl, "one_match", side_effect=[("本场 +5", 15), ("本场 +5", self.CAP)]) as match:
                    r = run("pet_brawl", Scripted(), self.info(10), options, now=OPEN)
                self.assertEqual(r.status, "submitted", r.detail)
                self.assertEqual(match.call_count, 2)
                self.assertIn("今日乱斗币 20/20", r.detail)

    def test_auto_can_take_more_than_the_old_ten_match_limit(self):
        with mock.patch.object(pet_brawl, "one_match", side_effect=[("本场 +1", total) for total in range(1, self.CAP + 1)]) as match:
            r = run("pet_brawl", Scripted(), self.info(), now=OPEN)
        self.assertEqual(match.call_count, 20)
        self.assertIn("打了 20 场，今日乱斗币 20/20", r.detail)

    def test_auto_stops_if_a_completed_match_gives_no_coins(self):
        with mock.patch.object(pet_brawl, "one_match", return_value=("本场 +0", 5)) as match:
            r = run("pet_brawl", Scripted(), self.info(5), now=OPEN)
        self.assertEqual(match.call_count, 1)
        self.assertIn("没有增加乱斗币", r.detail)
        self.assertEqual(r.status, "blocked")

    def test_escape_finish_with_reward_refreshes_and_continues_to_cap(self):
        finish = {"winType": 1, "reportId": "offline-report", "addPetBtlM": 10,
                  "error": "对方已逃跑", "everyDaySetting": {"addPetPvpM": 0}}
        conn = Scripted(script=[("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
                                ("onPetPVPFinish", finish), *self.WON])
        with mock.patch.object(Session, "refresh", return_value=self.info(10)) as refresh:
            result = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertIn("今日乱斗币 20/20", result.detail)
        self.assertIn("打了 2 场", result.detail)
        refresh.assert_called_once()
        self.assertEqual(len(conn.bodies("matchStart")), 2)

    def test_escape_finish_stops_at_cap_or_when_refresh_is_unknown_or_unchanged(self):
        finish = {"winType": 1, "reportId": "offline-report", "addPetBtlM": 10, "error": "对方已逃跑"}
        for snapshot, status in ((self.info(20), "submitted"), ({}, "blocked"), (self.info(), "blocked")):
            with self.subTest(snapshot=snapshot):
                conn = Scripted(script=[("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
                                        ("onPetPVPFinish", finish)])
                with mock.patch.object(Session, "refresh", return_value=snapshot):
                    result = run("pet_brawl", conn, self.info(), now=OPEN)
                self.assertEqual(result.status, status, result.detail)
                self.assertEqual(len(conn.bodies("matchStart")), 1)

    def test_escape_finish_refresh_failure_does_not_restart(self):
        conn = Scripted(script=[("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
                                ("onPetPVPFinish", {"winType": 1, "reportId": "offline-report", "addPetBtlM": 10,
                                                    "error": "对方已逃跑"})])
        with mock.patch.object(Session, "refresh", side_effect=p.AllianceError("offline disconnect")):
            result = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(conn.bodies("matchStart")), 1)

    def test_other_or_incomplete_finish_errors_still_block(self):
        base = {"winType": 1, "reportId": "offline-report", "addPetBtlM": 10, "error": "对方已逃跑"}
        for changes in ({"error": "战斗异常"}, {"code": 500}, {"reportId": ""}, {"winType": 0},
                        {"addPetBtlM": None}, {"addPetBtlM": -1}):
            with self.subTest(changes=changes):
                conn = Scripted(script=[("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
                                        ("onPetPVPFinish", {**base, **changes})])
                with mock.patch.object(Session, "refresh") as refresh:
                    result = run("pet_brawl", conn, self.info(), now=OPEN)
                self.assertEqual(result.status, "blocked", result.detail)
                self.assertEqual(len(conn.bodies("matchStart")), 1)
                refresh.assert_not_called()

    def test_finish_before_current_room_is_not_counted_as_new_match(self):
        conn = Scripted(script=[("onPetPVPFinish", {"winType": 0, "addPetBtlM": 0,
                         "reportId": "old_match", "everyDaySetting": {"addPetPvpM": 0}})])
        result = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("尚未进入本场房间", result.detail)
        self.assertNotIn("打了 1 场", result.detail)
        self.assertEqual(len(conn.bodies("matchStart")), 1)
        self.assertEqual(len(conn.bodies("matchCancel")), 1)
        self.assertFalse(conn.bodies("setBtlResult"))

    def test_finish_after_match_result_but_before_room_is_also_unowned(self):
        conn = Scripted(script=[("onPVPMatchResult", {"code": 200, "targetUid": "9"}),
                                ("onPetPVPFinish", {"addPetBtlM": 0})])
        result = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(result.status, "blocked")
        self.assertIn("尚未进入本场房间", result.detail)
        self.assertEqual(len(conn.bodies("matchStart")), 1)

    def kicked_run(self, polls, relogins):
        conn = Scripted()
        with (mock.patch.object(conn, "poll_push", side_effect=polls),
              mock.patch.object(pet_brawl, "LOAD_DELAY", 3600.0), mock.patch.object(pet_brawl, "KICK_WAIT", 0.0),
              mock.patch.object(Session, "refresh") as refresh,
              mock.patch.object(Session, "relogin", side_effect=relogins) as relogin):
            lines = []
            result = run("pet_brawl", conn, self.info(), now=OPEN, log=lines.append)
        refresh.assert_not_called()
        return conn, result, relogin, lines

    def test_kick_before_room_logs_back_in_and_continues(self):
        """被踢（真机 11-01 匹配后、10-30 / 11-08 第 2 局摆阵后）：不取消匹配，等一会儿重新登录，重读乱斗币接着打。"""
        error = p.AllianceError("服务器踢出了当前会话（服务器未提供踢出原因）")
        conn, result, relogin, lines = self.kicked_run([error], [self.info(self.CAP)])
        self.assertEqual(relogin.call_count, 1)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertIn(f"{self.CAP}/{self.CAP}", result.detail)
        self.assertTrue(any("匹配中断" in line and "重新登录" in line for line in lines))
        self.assertEqual(conn.sent, [(pet_brawl.R_INFO, {}), (pet_brawl.R_START, {})])  # 断线后不发取消

    def test_kick_after_room_retries_at_most_twice(self):
        prepare = SimpleNamespace(route=pet_brawl.PREPARE, body={"roomId": "room1", "dungeonId": 2000101})
        error = p.AllianceError("服务器踢出了当前会话（服务器未提供踢出原因）")
        conn, result, relogin, _lines = self.kicked_run([prepare, error] * 3, [self.info(5)] * 2)
        self.assertEqual(relogin.call_count, pet_brawl.KICK_RETRY_MAX)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("对局中断", result.detail)
        self.assertIn("房间room1", result.detail)
        self.assertEqual(len(conn.bodies("matchStart")), 3)

    def test_relogin_failure_stops(self):
        error = p.AllianceError("服务器踢出了当前会话（服务器未提供踢出原因）")
        conn, result, relogin, _lines = self.kicked_run([error], p.AllianceError("连接游戏服务器失败"))
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("重新登录失败", result.detail)

    def test_disconnect_after_second_round_swap_logs_confirmed_step_without_retry(self):
        script = [("onPrepareCreateOnlineCopy", {"roomId": "room1", "dungeonId": 2000101}),
                  ("onPetPvpBtlChangeState", {"btlStage": 0, "session": 2,
                   "players": self.players([1065, 610460, 1258083], [1], [0, 2])})]
        conn = Scripted(script=script)
        poll = conn.poll_push

        def receive(routes, timeout):
            if not conn.script:
                raise p.AllianceError("服务器提前关闭了 TLS 连接")
            return poll(routes, timeout)

        lines = []
        with mock.patch.object(conn, "poll_push", side_effect=receive):
            result = run("pet_brawl", conn, self.info(), now=OPEN, log=lines.append)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("第2局换宠已响应", result.detail)
        self.assertIn("阶段0", result.detail)
        self.assertTrue(any("换宠已响应" in line for line in lines))
        self.assertEqual(conn.bodies("setPetPvpWaitPet"), [{"roomId": "room1", "index": 0}])
        self.assertEqual(len(conn.bodies("matchStart")), 1)

    def test_players_reordering_in_second_round_does_not_reverse_loss_hps(self):
        conn = Scripted(script=[
            ("onPrepareCreateOnlineCopy", {"roomId": "r", "dungeonId": 2000101}),
            ("onPetPvpBtlChangeState", {"btlStage": 2, "session": 1, "players": [{"uid": "7"}, {"uid": "9"}]}),
            ("onPetPvpBtlChangeState", {"btlStage": 0, "session": 2, "players": [{"uid": "9"}, {"uid": "7"}]}),
            ("onPetPvpBtlChangeState", {"btlStage": 2, "session": 2, "players": [{"uid": "9"}, {"uid": "7"}]}),
            ("onPetPVPFinish", {"addPetBtlM": 20, "everyDaySetting": {"addPetPvpM": 20}})])
        with mock.patch.object(pet_brawl, "FIGHT_DELAY", 0):
            result = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual([b["hps"] for b in conn.bodies("setBtlResult")], [[0.0, 1.0], [1.0, 0.0]])

    def test_auto_respects_stop_after_current_match(self):
        for finish_current in (False, True):
            with self.subTest(finish_current=finish_current):
                stop = threading.Event()

                def match(ctx, uid):
                    stop_after(stop) if finish_current else stop.set()
                    return "本场 +5", 5

                with mock.patch.object(pet_brawl, "one_match", side_effect=match) as match_call:
                    r = run("pet_brawl", Scripted(), self.info(), now=OPEN, stop=stop)
                self.assertEqual(match_call.call_count, 1)
                self.assertIn("停止", r.detail)

    def test_cap_reached_before_times_are_used_stops(self):
        conn = Scripted(script=[
            ("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
            ("onPetPvpBtlChangeState", {"btlStage": 0}),
            ("onPetPVPFinish", {"addPetBtlM": 20, "everyDaySetting": {"addPetPvpM": self.CAP}})])
        with mock.patch("zmxy.tasks.pet_brawl.LOAD_DELAY", 0.0):
            r = run("pet_brawl", conn, self.info(), {"times": 3}, now=OPEN)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 1)  # 满了就不再开第二场
        self.assertIn("今日乱斗币 20/20", r.detail)
        self.assertNotIn("剩余", r.detail)
        self.assertNotIn("次数不足", r.detail)  # 提示里不写“次数不足”（那是口头说法）

    @staticmethod
    def players(powers, on, cards):
        return [{"uid": "9", "btlPets": [{"fightPower": 1}] * 3},
                {"uid": "7", "btlPets": [{"fightPower": w} for w in powers], "onBtl": on, "btlCards": cards}]

    def test_choose_lowest_power_pet(self):
        pick, _ = pet_brawl.choose_pet(self.players([300, 100, 200], [0], [1, 2]), "7")
        self.assertEqual(pick, 1)  # 手牌里战力最低的换上去

    def test_lowest_already_on_field_is_kept(self):
        self.assertIsNone(pet_brawl.choose_pet(self.players([100, 300, 200], [0], [1, 2]), "7")[0])

    def test_equal_power_is_left_to_the_system(self):
        pick, why = pet_brawl.choose_pet(self.players([100, 100, 100], [0], [1, 2]), "7")
        self.assertIsNone(pick)
        self.assertIn("相同", why)

    def test_unreadable_power_is_left_to_the_system(self):
        players = self.players([100, 200, 300], [0], [1, 2])
        del players[1]["btlPets"][1]["fightPower"]
        self.assertIsNone(pet_brawl.choose_pet(players, "7")[0])

    def test_lowest_pet_swap_is_sent_during_placement(self):
        conn = Scripted(script=[
            ("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
            ("onPetPvpBtlChangeState", {"btlStage": 0, "session": 1, "players": self.players([300, 100, 200], [0], [1, 2])}),
            ("onPetPVPFinish", {"addPetBtlM": 20, "everyDaySetting": {"addPetPvpM": self.CAP}})])
        with mock.patch("zmxy.tasks.pet_brawl.LOAD_DELAY", 0.0):
            run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN)
        self.assertEqual(conn.bodies("setPetPvpWaitPet"), [{"roomId": "0$1", "index": 1}])

    def test_every_log_line_shows_todays_coins(self):
        conn = Scripted(script=[
            ("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
            ("onPetPvpBtlChangeState", {"btlStage": 0}),
            ("onPetPVPFinish", {"addPetBtlM": 2, "everyDaySetting": {"addPetPvpM": 5}}),
            ("onPVPMatchResult", {"code": 500, "error": "停"})])
        lines = []
        with mock.patch("zmxy.tasks.pet_brawl.LOAD_DELAY", 0.0):
            run("pet_brawl", conn, self.info(3), {"times": 2}, now=OPEN, log=lines.append)
        self.assertTrue(lines)
        self.assertTrue(all(x.startswith(("乱斗币 3/20｜", "乱斗币 5/20｜")) for x in lines), lines)
        self.assertTrue(lines[0].startswith("乱斗币 3/20｜"))
        self.assertTrue(lines[-1].startswith("乱斗币 5/20｜"))  # 打完一场后跟着更新

    FLED = [("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$1"}),
            ("onPetPvpBtlChangeState", {"btlStage": 1})]  # 加载阶段报 loadingEnd 时被拒绝“战斗已完成”
    WON = [("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$2"}),
           ("onPetPVPFinish", {"addPetBtlM": 20, "everyDaySetting": {"addPetPvpM": 20}})]

    def fled_run(self, script, times, after=0):
        """真机 2026-10-09：对手中途退出 → loadingEnd 被拒绝“战斗已完成”，没有结算推送；重读角色得到 after 个乱斗币。"""
        conn = Scripted(lambda route, body: "战斗已完成" if route.endswith("loadingEnd") and body["roomId"] == "0$1" else None, script)
        with mock.patch("zmxy.tasks.pet_brawl.LOAD_DELAY", 0.0), mock.patch.object(Session, "refresh", lambda s: self.info(after)):
            return conn, run("pet_brawl", conn, self.info(), {"times": times}, now=OPEN)

    def test_opponent_fleeing_without_coins_is_replayed(self):
        conn, r = self.fled_run(self.FLED + self.WON, 1)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 2)  # 跑掉的那场不算次数，重新开一局
        self.assertIn("打了 1 场，今日乱斗币 20/20", r.detail)
        self.assertIn("1 场对手中途退出", r.detail)

    def test_fled_match_that_paid_coins_counts(self):
        conn, r = self.fled_run(self.FLED, 1, after=self.CAP)
        self.assertEqual(conn.routes().count("matchStart"), 1)
        self.assertIn("打了 1 场，今日乱斗币 20/20", r.detail)
        self.assertNotIn("重开", r.detail)

    def test_replays_are_limited(self):
        conn, r = self.fled_run(self.FLED * (pet_brawl.REPLAY_MAX + 2), 1)
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), pet_brawl.REPLAY_MAX + 1)
        self.assertIn("不再重开", r.detail)

    def test_match_timeout_cancels(self):
        conn = Scripted()
        with mock.patch("time.monotonic", tick()):
            r = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes().count("matchStart"), 3)
        self.assertEqual(conn.routes().count("matchCancel"), 3)
        self.assertIn("已重试匹配 2 次，不再重试", r.detail)

    TIMEOUT = [("onPVPMatchResult", {"code": 500, "error": "匹配超时,请稍后重试"})]

    def test_server_match_timeout_retries_without_using_a_run(self):
        for failures in (1, 2):
            with self.subTest(failures=failures):
                conn = Scripted(script=self.TIMEOUT * failures + self.WON)
                lines = []
                r = run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN, log=lines.append)
                self.assertEqual(r.status, "submitted", r.detail)
                self.assertEqual(conn.routes().count("matchStart"), failures + 1)
                self.assertNotIn("matchCancel", conn.routes())  # 服务器已明确结束排队
                self.assertIn("打了 1 场，今日乱斗币 20/20", r.detail)
                self.assertEqual(sum("次重试" in line for line in lines), failures)
                self.assertTrue(all(line.startswith(("乱斗币 0/20｜", "乱斗币 20/20｜")) for line in lines))

    def test_server_match_timeout_stops_after_two_retries(self):
        conn = Scripted(script=self.TIMEOUT * 4)
        r = run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 3)
        self.assertEqual(conn.script, self.TIMEOUT)  # 不会开始第 4 次匹配
        self.assertIn("已重试匹配 2 次，不再重试", r.detail)
        self.assertNotIn("setBtlResult", conn.routes())

    def test_match_retry_limit_resets_after_a_completed_match(self):
        first = self.WON[:1] + [("onPetPVPFinish", {"addPetBtlM": 5, "everyDaySetting": {"addPetPvpM": 5}})]
        second = [("onPrepareCreateOnlineCopy", {"dungeonId": 2000101, "roomId": "0$3"}),
                  ("onPetPVPFinish", {"addPetBtlM": 15, "everyDaySetting": {"addPetPvpM": self.CAP}})]
        conn = Scripted(script=self.TIMEOUT * 2 + first + self.TIMEOUT * 2 + second)
        r = run("pet_brawl", conn, self.info(), {"times": 2}, now=OPEN)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 6)
        self.assertIn("打了 2 场，今日乱斗币 20/20", r.detail)

    def test_local_match_timeout_cancels_before_retrying(self):
        conn = Scripted()

        def handler(route, body):
            if route == pet_brawl.R_START and conn.routes().count("matchStart") == 2:
                conn.script.extend(self.WON)

        conn.handler = handler
        with mock.patch("time.monotonic", tick()):
            r = run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes()[:4], ["getPetPVPInfo", "matchStart", "matchCancel", "matchStart"])
        self.assertIn("打了 1 场", r.detail)

    def test_local_match_timeout_does_not_retry_if_cancel_fails(self):
        conn = Scripted(lambda route, body: "取消失败" if route == pet_brawl.R_CANCEL else None)
        with mock.patch("time.monotonic", tick()):
            r = run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 1)
        self.assertIn("取消匹配失败", r.detail)

    def test_stop_during_match_timeout_prevents_retry(self):
        for finish_current in (False, True):
            with self.subTest(finish_current=finish_current):
                stop = threading.Event()
                conn = Scripted(script=self.TIMEOUT + self.WON)

                def log(message):
                    if "收到 onPVPMatchResult" in message:
                        stop_after(stop) if finish_current else stop.set()

                r = run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN, stop=stop, log=log)
                self.assertEqual(r.status, "blocked", r.detail)
                self.assertEqual(conn.routes().count("matchStart"), 1)
                self.assertIn("停止", r.detail)

    def test_match_timeout_after_entering_a_room_does_not_retry(self):
        conn = Scripted(script=self.WON[:1] + self.TIMEOUT + self.WON)
        r = run("pet_brawl", conn, self.info(), {"times": 1}, now=OPEN)
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 1)
        self.assertIn("匹配失败", r.detail)

    def test_rejected_match_result_stops(self):
        conn = Scripted(script=[("onPVPMatchResult", {"code": 500, "error": "宠物不足"})])
        r = run("pet_brawl", conn, self.info(), now=OPEN)
        self.assertEqual(r.status, "blocked")
        self.assertIn("宠物不足", r.detail)
        self.assertEqual(conn.routes().count("matchStart"), 1)


class ArenaApplyTests(unittest.TestCase):
    class Context:
        def __init__(self, now, info):
            self.now, self.info, self.sent = now, info, []

        def request(self, route, body):
            self.sent.append((route, body))
            return {"code": 200}

    def test_apply_window_rejects_invalid_calendar_values_and_handles_week_wrap(self):
        for value in ("0-12:00:00", "8-12:00:00", "5-24:00:00", "5-12:60:00", "5-12:00:60"):
            self.assertIsNone(arena_apply._offset(value), value)
        monday = int(datetime(2026, 10, 5, 0, 30, tzinfo=state._CN).timestamp())
        self.assertTrue(arena_apply.in_apply_window(
            {"applyStartTime": "7-22:00:00", "applyEndTime": "1-01:00:00"}, monday))

    def test_invalid_weekly_registration_count_blocks_without_writing(self):
        ctx = self.Context(OPEN, {"everyWeekSetting": {"applyForArenaTime": "bad"}})
        with mock.patch.object(arena_apply.state, "row", return_value={"applyStartTime": "5-16:00:00", "applyEndTime": "7-22:00:00"}):
            result = arena_apply.apply(ctx, "arena_apply", "竞技场", 1, arena_apply.R_ARENA, "applyForArenaTime")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(ctx.sent, [])

    def test_registered_weekly_arena_is_not_submitted_again(self):
        ctx = self.Context(OPEN, {"everyWeekSetting": {"applyForArenaTime": 1}})
        with mock.patch.object(arena_apply.state, "row", return_value={"applyStartTime": "5-16:00:00", "applyEndTime": "7-22:00:00"}):
            result = arena_apply.apply(ctx, "arena_apply", "竞技场", 1, arena_apply.R_ARENA, "applyForArenaTime")
        self.assertEqual(result.status, "skipped")
        self.assertEqual(ctx.sent, [])

    def test_missing_weekly_setting_blocks_registration(self):
        ctx = self.Context(OPEN, {})
        with mock.patch.object(arena_apply.state, "row", return_value={"applyStartTime": "5-16:00:00", "applyEndTime": "7-22:00:00"}):
            result = arena_apply.apply(ctx, "arena_apply", "竞技场", 1, arena_apply.R_ARENA, "applyForArenaTime")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(ctx.sent, [])


if __name__ == "__main__":
    unittest.main()
