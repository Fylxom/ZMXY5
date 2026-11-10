"""十绝阵（周常页）。全部离线：房间/广播/战斗服务器都用假对象。"""

import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from zmxy.core import Session, UncertainOutcome
from zmxy.tasks import ten_jue
from tests.support import Fake, run

INFO = {"uid": 77, "hpMax": 5000, "haloRewardCnt": 2}
STAGE = 1500101
ONLINE = {"message": {"ip": "10.0.0.1", "port": 9000, "instId": 42, "password": "", "dungeonId": STAGE,
                      "playerInfos": [{"playerId": 77, "hp": 4800}]}}


def setUpModule():  # 整个模块都不联网（有的用例直接 run，不经过 go）
    patcher = mock.patch("zmxy.online.client_cg", lambda now=None, fetch=None: "assets/data/cg1_test_1.swf")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeTeam:
    def __init__(self, fail_exit=False):
        self.sent, self.fail_exit = [], fail_exit

    def request(self, route, body):
        self.sent.append((route.rsplit(".", 1)[1], body))
        if route.endswith("battleExit") and self.fail_exit:
            raise UncertainOutcome("超时")
        return {"code": 200}


def server(stage=STAGE, reward=None, room_fail=False, left_after=None):
    state = {"left": 2}

    def handler(route, body):
        name = route.rsplit(".", 1)[1]
        if name == "getDungeonId":
            return {"dungeonId": stage}
        if name == "createWaitingRoom":
            return "房间已满" if room_fail else {"roomId": "r1"}
        if name == "getHaloReward":
            state["left"] -= 1
            return reward if reward is not None else {"give": [{"itemId": 101045121, "count": 1}], "haloRewardCnt": state["left"]}
    return handler


class WaitingConn(Fake):
    score = 3000

    def wait_for_push(self, route, timeout=None):
        self.__dict__.setdefault("waits", []).append(route)
        if route == "onCreateOnlineCopy":
            return ONLINE
        return {"code": 200, "score": self.score} if route == "onHaloResult" else {}

    def notify(self, route, body):
        self.sent.append((route, body))


def go(handler, team, info=INFO, score=3000, options=None):
    conn = WaitingConn(handler)
    conn.score = score

    @contextlib.contextmanager
    def open_team(self, host, port):
        team.endpoint = (host, port)
        yield team

    with mock.patch.object(Session, "open_team", open_team), mock.patch("zmxy.core.Session.refresh", lambda s: {"haloRewardCnt": 1}):
        return conn, run("ten_jue", conn, info, {"times": ten_jue.MAX_RUNS} if options is None else options)


@mock.patch("time.sleep", lambda s: None)
class TenJueTests(unittest.TestCase):
    def test_bag_full_reward_is_refought_on_resume(self):
        """打完了、领奖时背包满：这一场不计次数；清完背包继续时重新打一场才能领（不能单独补领）。"""
        full = {"on": True}
        base = server()

        def handler(route, body):
            if route.endswith("getHaloReward") and full["on"]:
                return "背包已满"
            return base(route, body)
        progress = {}

        def once(options):
            real = ten_jue.dungeon.resume_done
            with mock.patch.object(ten_jue.dungeon, "resume_done", lambda ctx: progress.update(owner=ctx) or real(ctx)):
                conn, r = go(handler, FakeTeam(), options=options)
            return conn, r, dict(progress["owner"].progress)

        conn, r, saved = once({"times": 2})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes().count("createWaitingRoom"), 1)
        self.assertEqual(saved, {"n": 0})
        full["on"] = False
        conn, r, saved = once({"times": 2, "resume_done": saved})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes().count("createWaitingRoom"), 2)  # 两场都重新开房打
        self.assertEqual(conn.routes().count("getHaloReward"), 2)
        self.assertEqual(saved, {"n": 2})

    def test_full_flow_and_result_body(self):
        team = FakeTeam()
        conn, r = go(server(), team)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(team.endpoint, ("10.0.0.1", 9000))
        self.assertEqual([n for n, _ in team.sent], ["enter", "battleExit"] * 2)  # 没有 loadComplete / keyOperation
        enter = team.sent[0][1]
        self.assertEqual((enter["instId"], enter["playerId"], enter["password"], enter["cg"]), (42, "77", "", "assets/data/cg1_test_1.swf"))
        exit_body = next(b for n, b in team.sent if n == "battleExit")
        self.assertEqual((exit_body["battleResult"], exit_body["frameNum"]), (5, 0))
        self.assertEqual(len(exit_body["data"]), 3)  # 3000 分：三个席位各 1000
        self.assertEqual({d["score"] for d in exit_body["data"]}, {1000})
        self.assertEqual(exit_body["playerInfo"][0]["playerId"], "77")
        self.assertEqual(conn.bodies("createWaitingRoom")[0], {"dungeonId": STAGE, "type": "halo"})
        self.assertEqual(conn.routes().count("getHaloReward"), 2)
        self.assertEqual(conn.routes().count("exitWaitingRoom"), 2)  # 每次打完都退房
        self.assertIn("onHaloResult", conn.waits)

    def test_times_option_limits_runs(self):
        conn, r = go(server(), FakeTeam(), options={"times": 1})
        self.assertEqual(conn.routes().count("getHaloReward"), 1)
        conn, r = go(server(), FakeTeam(), options={"times": 0})  # 留空不执行
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])
        conn, r = go(server(), FakeTeam(), options={"times": 6})
        self.assertEqual(r.status, "blocked")

    def test_score_below_tier_does_not_claim(self):
        conn, r = go(server(), FakeTeam(), score=1000)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("getHaloReward", conn.routes())
        self.assertIn("exitWaitingRoom", conn.routes())

    def test_unknown_count_zero_and_bad_stage(self):
        team = FakeTeam()
        conn, r = go(server(), team, {"uid": 77})
        self.assertEqual(r.status, "blocked")
        conn, r = go(server(), team, {**INFO, "haloRewardCnt": 0})
        self.assertEqual(r.status, "skipped")
        conn, r = go(server(stage=999), team)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("createWaitingRoom", conn.routes())
        conn, r = go(server(), team, {"haloRewardCnt": 1})  # 没有角色 uid
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_room_failure_stops(self):
        conn, r = go(server(room_fail=True), FakeTeam())
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("prepareCreateOnlineCopy", conn.routes())

    def test_exit_timeout_uncertain_never_claims_reward(self):
        conn = WaitingConn(server())
        team = FakeTeam(fail_exit=True)

        @contextlib.contextmanager
        def open_team(self, host, port):
            yield team

        with mock.patch.object(Session, "open_team", open_team):
            with self.assertRaises(UncertainOutcome):
                run("ten_jue", conn, INFO)
        self.assertNotIn("getHaloReward", conn.routes())

    def test_reward_without_give_and_counter_unchanged_is_not_success(self):
        conn, r = go(server(reward={}), FakeTeam(), {**INFO, "haloRewardCnt": 2})
        # 刷新后剩余次数 1 = 2-1，算确认成功一次；再下一次刷新仍是 1（没有再减少）→ 停止
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes().count("getHaloReward"), 2)
        conn, r = go(server(reward={}), FakeTeam(), {**INFO, "haloRewardCnt": 5})
        self.assertEqual(r.status, "blocked")  # 刷新后是 1，不是 4：本次不算成功
        self.assertEqual(conn.routes().count("getHaloReward"), 1)

    def test_endpoint_parsing_keeps_numeric_instance_and_empty_password(self):
        from zmxy import online
        self.assertEqual(online.role_stats({"uid": 7, "p1": {"hpMax": 900}}), (7, 900))  # 血量在 p1 里（客户端 StartGameCmd）
        self.assertEqual(online.role_stats({"uid": 7, "hpMax": 5}), (7, 5))
        self.assertIsNone(online.role_stats({"uid": 7, "p1": {}}))
        where = online.endpoint(ONLINE)
        self.assertEqual((where.host, where.port, where.inst_id, where.password), ("10.0.0.1", 9000, 42, ""))
        self.assertIsNone(online.endpoint({"ip": "x", "port": 0, "instId": 1}))
        self.assertIsNone(online.seat(ONLINE, 5, 100))  # 角色不在 playerInfos 里


if __name__ == "__main__":
    unittest.main()
