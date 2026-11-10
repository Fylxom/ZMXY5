"""师徒试炼（周常页）。全部离线：两个账号的会话、房间广播、战斗服务器都用假对象。"""

import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from zmxy import login, protocol as p
from zmxy.core import Account, Session, TaskContext, UncertainOutcome, load_tasks
from zmxy.tasks import teacher_trial as tt
from tests.support import Fake

M_UID, S_UID = 77, 88
ONLINE = {"message": {"ip": "10.0.0.1", "port": 9000, "instId": 42, "password": "pw", "dungeonId": 1200103,
                      "playerInfos": [{"playerId": M_UID, "hp": 4800}, {"playerId": S_UID, "hp": 3000}]}}


class Conn(Fake):
    """主连接：请求走 handler，推送等待直接给广播。"""

    def wait_for_push(self, route, timeout=None):
        self.__dict__.setdefault("waits", []).append(route)
        return ONLINE if route == "onCreateOnlineCopy" else {}

    def notify(self, route, body):
        self.__dict__.setdefault("notes", []).append((route.rsplit(".", 1)[1], body))


class Team:
    def __init__(self, frames=4, per_push=2, fail_exit=False, drain_each=True):
        self.sent, self.frames, self.per_push, self.fail_exit = [], frames, per_push, fail_exit

    def request(self, route, body):
        name = route.rsplit(".", 1)[1]
        self.sent.append((name, body))
        if name == "battleExit" and self.fail_exit:
            raise UncertainOutcome("超时")
        return {"code": 200, "sendKeyFrameNum": self.per_push, "time": 1}

    def notify(self, route, body):
        self.sent.append((route.rsplit(".", 1)[1], body))

    def wait_push(self, route, timeout=None):
        if route == "onSendFrame" and not self.frames:
            raise p.AllianceError("等待 onSendFrame 超时")
        return {}

    def drain_pushes(self):  # 第一帧已被 wait_push 取走
        return [SimpleNamespace(route="onSendFrame") for _ in range(max(0, self.frames - 1))]


def handler(uid_used, role_field, give=True):
    state = {"used": uid_used}

    def h(route, body):
        name = route.rsplit(".", 1)[1]
        if name == "getApprenticeshipData":
            return {"masterUid": 5}
        if name == "createWaitingRoom":
            return {"roomId": "r1"}
        if name == "getMasterCopyRewardData":
            return {"give": [], role_field: state["used"]}
        if name == "getMasterCopyReward":
            state["used"] += 1
            return {"give": [{"itemId": 101045121, "count": 1}] if give else [], "everyWeekSetting": {role_field: state["used"]}}
    return h


def make(role="master", used=16, frames=4, fail_exit=False, student_used=0, give=True, same_account=False):
    spec = load_tasks()["teacher_trial" if role == "master" else "teacher_trial_student"]
    cfg = p.ClientConfig(uid=1, username="m")
    master = Session(cfg)
    master._conn = Conn(handler(used, "teacherChallengeWeekNum", give))
    master.info = {"uid": M_UID, "hpMax": 5000, "everyWeekSetting": {"teacherChallengeWeekNum": used, "studentChallengeWeekNum": 0}}
    if used is None:  # 读不到：连周设置对象都没有
        del master.info["everyWeekSetting"]
    master._link = login.Link(master._conn, cfg, [], {})
    student = Session(p.ClientConfig(uid=2, username="m" if same_account else "s"))
    student._conn = Conn(handler(student_used, "studentChallengeWeekNum", give))
    student.info = {"uid": S_UID, "hpMax": 3000, "everyWeekSetting": {"studentChallengeWeekNum": student_used}}
    student._link = login.Link(student._conn, student.config, [], {})
    for s in (master, student):
        s.bind(spec)
    teams = {"m": Team(frames, fail_exit=fail_exit), "s": Team(frames)}

    def opener(name):
        @contextlib.contextmanager
        def open_team(host, port):
            yield teams[name]
        return open_team

    master.open_team, student.open_team = opener("m"), opener("s")
    master.peers = lambda user, slot: Account("s", student.config) if user == "s" else None

    @contextlib.contextmanager
    def peer(user, slot):  # 真实的 open_peer 会重新登录；这里直接给已经准备好的徒弟会话
        if student.config.username == master.config.username:
            raise p.AllianceError("两个角色必须来自不同的登录账号")
        yield student

    master.open_peer = peer
    return spec, master, student, teams


def go(role="master", options=None, **kw):
    spec, master, student, teams = make(role, **kw)
    ctx = TaskContext(master, options if options is not None else {"student": "s|1", "times": 5})
    with mock.patch("time.sleep", lambda s: None), mock.patch("zmxy.online.client_cg", lambda now=None, fetch=None: "assets/data/cg1_test_1.swf"):  # 不联网
        return spec.run(ctx), master, student, teams


def setUpModule():  # 整个模块都不联网（有的用例直接 spec.run，不经过 go）
    patcher = mock.patch("zmxy.online.client_cg", lambda now=None, fetch=None: "assets/data/cg1_test_1.swf")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class TeacherTrialTests(unittest.TestCase):
    def test_master_reward_flow(self):
        r, master, student, teams = go("master", used=16)  # 每周 18 次，剩 2
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(master._conn.routes().count("getMasterCopyReward"), 2)
        self.assertEqual(student._conn.routes().count("getMasterCopyReward"), 0)
        self.assertEqual(master._conn.bodies("createWaitingRoom")[0], {"dungeonId": 1200103, "type": "master", "masterId": 5})
        self.assertEqual(student._conn.bodies("joinWaitingRoom")[0]["roomId"], "r1")
        self.assertEqual(student._conn.bodies("changePlayerStatus")[0]["status"], 1)
        # 师傅奖励：只有师傅这一端提交 battleExit
        exits = [(n, b) for n, b in teams["m"].sent if n == "battleExit"]
        self.assertEqual(len(exits), 2)
        self.assertFalse(any(n == "battleExit" for n, _ in teams["s"].sent))
        body = exits[0][1]
        self.assertEqual((body["battleResult"], body["frameNum"]), (5, 4 * 2))  # 4 个 onSendFrame × 2
        self.assertEqual([(x["playerId"], x["isme"]) for x in body["playerInfo"]], [(M_UID, True), (S_UID, False)])
        enter = next(b for n, b in teams["m"].sent if n == "enter")
        self.assertEqual((enter["instId"], enter["playerId"], enter["password"]), (42, M_UID, "pw"))
        self.assertEqual(enter["cg"], "assets/data/cg1_test_1.swf")  # 当前客户端版本，师徒两端同一个
        ready = [b for n, b in teams["s"].sent if n == "keyOperation" and b["objs"] and b["objs"][0].get("ready")]
        self.assertEqual(ready[0]["objs"][0], {"ready": True, "uid": 1, "teamPs": 1})  # 徒弟是座位 1
        self.assertIn("本周剩余 0/18", r.detail)
        # 不发任何攻击/移动按键：keyOperation 只有 ready 和 ping
        for name in ("m", "s"):
            for n, b in teams[name].sent:
                if n == "keyOperation":
                    self.assertTrue(b["keys"] == [84] and (b["objs"][0].get("ready") or b["objs"][0].get("ping")))

    def test_student_reward_flow_submits_both_views_and_claims_on_student(self):
        r, master, student, teams = go("student", student_used=1)  # 徒弟每周 2 次，剩 1
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(student._conn.routes().count("getMasterCopyReward"), 1)
        self.assertEqual(master._conn.routes().count("getMasterCopyReward"), 0)
        for team, me in ((teams["m"], M_UID), (teams["s"], S_UID)):
            body = next(b for n, b in team.sent if n == "battleExit")
            self.assertEqual([x["playerId"] for x in body["playerInfo"] if x["isme"]], [me])

    def test_fifteen_claims_then_tls_failure_is_blocked_without_reopening(self):
        spec, master, student, teams = make("master", used=0)
        original = master.open_team
        entered, closed, logs = [], [], []

        @contextlib.contextmanager
        def flaky(host, port):
            entered.append((host, port))
            if len(entered) == 16:
                raise p.AllianceError("TLS 握手失败 10.0.0.1:9000；SSLEOFError：EOF")
            with original(host, port) as team:
                try:
                    yield team
                finally:
                    closed.append(team)

        master.open_team = flaky
        with mock.patch("time.sleep", lambda s: None):
            result = spec.run(TaskContext(master, {"student": "s|1", "times": 18}, log=logs.append))
        self.assertEqual(result.status, "blocked")
        self.assertIn("完成 15 次，本周剩余 3/18", result.detail)
        self.assertIn("第 16 次失败", result.detail)
        self.assertIn("失败步骤：师傅战斗服TLS/Pomelo连接", result.detail)
        self.assertIn("EOF", result.detail)
        self.assertEqual(len(entered), 16)
        self.assertEqual(len(closed), 15)
        self.assertEqual(master._conn.routes().count("getMasterCopyReward"), 15)
        self.assertEqual(master._conn.routes().count("createWaitingRoom"), 16)
        self.assertIn("exitWaitingRoom", master._conn.routes())
        self.assertEqual(len(logs), 15)
        self.assertIn("主要耗时", logs[-1])
        self.assertNotIn("password", result.detail)

    def test_student_tls_failure_closes_master_connection_and_does_not_claim(self):
        spec, master, student, teams = make("master", used=0)
        closed = []

        @contextlib.contextmanager
        def master_team(*args):
            try:
                yield teams["m"]
            finally:
                closed.append(True)

        @contextlib.contextmanager
        def student_team(*args):
            raise p.AllianceError("TLS 握手超时")
            yield

        master.open_team, student.open_team = master_team, student_team
        result = spec.run(TaskContext(master, {"student": "s|1", "times": 2}))
        self.assertEqual(result.status, "blocked")
        self.assertIn("失败步骤：徒弟战斗服TLS/Pomelo连接", result.detail)
        self.assertEqual(closed, [True])
        self.assertEqual(master._conn.routes().count("createWaitingRoom"), 1)
        self.assertNotIn("getMasterCopyReward", master._conn.routes())

    def test_trace_measures_each_step_and_preserves_original_failure(self):
        now = [0.0]
        with mock.patch.object(tt.time, "monotonic", side_effect=lambda: now[0]):
            trace = tt.TrialTrace()
            def delay():
                now[0] += 7.5
            trace.call("师傅首帧", delay)
            error = UncertainOutcome("结果未知")
            with self.assertRaises(UncertainOutcome) as raised:
                trace.call("领奖", mock.Mock(side_effect=error))
            self.assertIs(raised.exception, error)
            self.assertIn("本场 7.50s", trace.note())
            self.assertIn("师傅首帧 7.50s", trace.note())
            self.assertIn("失败步骤：领奖", trace.note())

    def test_unknown_count_or_not_selected_or_used_up(self):
        r, master, *_ = go("master", used=None)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("createWaitingRoom", master._conn.routes())
        self.assertEqual(go("master", options={"times": 1})[0].status, "skipped")
        self.assertEqual(go("master", options={"student": "s|1", "times": ""})[0].status, "skipped")
        r, master, *_ = go("master", used=18)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(master._conn.sent, [])

    def test_zero_week_count_omitted_by_server_counts_as_zero(self):
        spec, master, student, teams = make("student")
        student.info = {"uid": S_UID, "hpMax": 3000, "everyWeekSetting": {"timeEnergy": {}}}  # 服务器不下发 0 值
        student._conn.handler = lambda route, body: {"give": []} if route.endswith("getMasterCopyRewardData") else {"code": 200}
        self.assertEqual(tt.read_used(student, "student"), 0)
        student.info = {"uid": S_UID}  # 连周设置对象都没有 → 仍读不到
        self.assertIsNone(tt.read_used(student, "student"))

    def test_same_login_account_is_refused(self):
        r, master, *_ = go("master", same_account=True)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("createWaitingRoom", master._conn.routes())

    def test_no_frames_means_no_battle_exit_and_room_is_left(self):
        r, master, student, teams = go("master", frames=0)
        self.assertEqual(r.status, "blocked")
        self.assertFalse(any(n == "battleExit" for t in teams.values() for n, _ in t.sent))
        self.assertIn("exitWaitingRoom", master._conn.routes())
        self.assertNotIn("getMasterCopyReward", master._conn.routes())

    def test_exit_timeout_is_uncertain_and_reward_never_claimed(self):
        spec, master, student, teams = make("master", fail_exit=True)
        with mock.patch("time.sleep", lambda s: None), self.assertRaises(UncertainOutcome) as raised:
            spec.run(TaskContext(master, {"student": "s|1", "times": 1}))
        self.assertIn("失败步骤：师傅结算", str(raised.exception))
        self.assertEqual(master._conn.routes().count("createWaitingRoom"), 1)
        self.assertNotIn("getMasterCopyReward", master._conn.routes())

    def test_a_battle_without_reward_is_replayed_once_then_stops(self):
        # 真机：第 6 场“未击败boss”，同版本再跑又成功。没奖励的那场不计次数，换一场重打；连续 2 场没有就停
        spec, master, student, teams = make("master", used=10)
        base, calls = master._conn.handler, {"n": 0}

        def flaky(route, body):
            if route.endswith("getMasterCopyRewardData"):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"error": "未击败boss或奖励次数已达上限，无法获得奖励"}
            return base(route, body)

        master._conn.handler = flaky
        with mock.patch("time.sleep", lambda s: None):
            r = spec.run(TaskContext(master, {"student": "s|1", "times": 2}))
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(master._conn.routes().count("getMasterCopyReward"), 2)  # 两次都领到了
        self.assertEqual(master._conn.routes().count("createWaitingRoom"), 3)   # 失败的那场 + 两场成功

    def test_two_battles_in_a_row_without_reward_stop_the_task(self):
        spec, master, student, teams = make("master", used=10)
        base = master._conn.handler
        master._conn.handler = lambda route, body: ({"error": "未击败boss或奖励次数已达上限，无法获得奖励"}
                                                    if route.endswith("getMasterCopyRewardData") else base(route, body))
        with mock.patch("time.sleep", lambda s: None):
            r = spec.run(TaskContext(master, {"student": "s|1", "times": 3}))
        self.assertEqual(r.status, "blocked")
        self.assertIn("连续 3 场没有奖励", r.detail)
        self.assertEqual(master._conn.routes().count("createWaitingRoom"), 3)  # 不会无限重打
        self.assertNotIn("getMasterCopyReward", master._conn.routes())

    def test_reward_without_give_list_is_not_claimed(self):
        spec, master, student, teams = make("master")
        base = master._conn.handler
        master._conn.handler = lambda route, body: {} if route.endswith("getMasterCopyRewardData") else base(route, body)
        with mock.patch("time.sleep", lambda s: None):
            r = spec.run(TaskContext(master, {"student": "s|1", "times": 1}))
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("getMasterCopyReward", master._conn.routes())

    def test_open_peer_requires_different_login(self):
        s = Session(p.ClientConfig(uid=1, username="m"))
        s.peers = lambda user, slot: Account("x", p.ClientConfig(uid=1, username="m"))
        with self.assertRaises(p.AllianceError), s.open_peer("m", 2):
            pass
        s.peers = lambda user, slot: None
        with self.assertRaises(p.AllianceError), s.open_peer("zz", 2):
            pass

    def test_ref_parsing_and_seat_helpers(self):
        self.assertEqual(tt.parse_ref("abc|3"), ("abc", 3))
        self.assertIsNone(tt.parse_ref("abc|9"))
        self.assertIsNone(tt.parse_ref(None))
        self.assertEqual((tt.week_limit("master"), tt.week_limit("student")), (18, 2))


if __name__ == "__main__":
    unittest.main()
