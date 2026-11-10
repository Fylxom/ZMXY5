"""周常页的星斗幻境（三层地图）。全部离线。造梦迷宫在 test_free_maze.py。"""

import json
import unittest
from unittest import mock

from zmxy.core import UncertainOutcome, load_tasks
from tests.support import Fake, run


class MazeServer:
    """三章星斗幻境：每章两行（第 0 行三个节点都通向第 1 行唯一的 Boss 节点）。"""

    def __init__(self, strength=100, week=None, pos=None, chapter=1):
        self.chapter, self.pos, self.status, self.passed = chapter, pos, 0, False
        self.strength = strength
        self.events = {(0, 0): [201, 1], (0, 1): [101, 5], (0, 2): [301, 0], (1, 0): [203, 2]}
        self.week = week if week is not None else {"rewardMazeCnt": 0, "mazeReward": {"5270": 3}}
        self.resettable = True
        self.timing = None  # (createTime, serverTime) 毫秒；设了才在地图里带上

    def data(self):
        body = {"map": [[[0], [0], [0]], [[]]], "event": [[self.events[(0, i)] for i in range(3)], [self.events[(1, 0)]]],
                "chapter": self.chapter, "chapterPassed": self.passed, "mazeItem": [{"id": 101, "count": self.strength}]}
        if self.timing:
            body["createTime"], body["serverTime"] = self.timing
        if self.pos:
            body["pos"] = {"x": self.pos[0], "y": self.pos[1]}
            body["status"] = self.status
        return {"data": body}

    def __call__(self, route, body):
        name = route.rsplit(".", 1)[1]
        if name == "getMapData":
            return self.data()
        if name == "goto":
            x, y = body["x"], body["y"]
            assert (self.pos is None and x == 0) or (self.pos and x == self.pos[0] + 1)
            self.pos, self.status = (x, y), 0
            self.strength -= 5
            return {"event": self.events[(x, y)][0], "param": self.events[(x, y)][1],
                    "consume": [{"type": "mazeProp", "subType": 101, "count": 5}]}
        if name in ("selectOption", "finishDungeon", "chooseReward", "getShopData"):
            self.status = 1
            if name == "finishDungeon" and self.pos[0] == 1:
                self.passed = True
            return {}
        if name == "getRewardList":
            return {"rewardList": [{"id": 1}]}
        if name == "nextChapter":
            self.chapter, self.pos, self.status, self.passed = self.chapter + 1, None, 0, False
            return self.data()
        if name == "reset":
            if not self.resettable:
                return "冷却中"
            self.chapter, self.pos, self.status, self.passed = 1, None, 0, False
            return self.data()
        if name == "getFinishRewardFree":
            self.week["rewardMazeCnt"] += 1
            self.week["mazeReward"]["5270"] -= 1
            return {"give": [{"itemId": 101045121, "count": 1}]}


class StarMazeTests(unittest.TestCase):
    def run_it(self, server):
        conn = Fake(server)
        info = {"everyWeekSetting": server.week}
        with mock.patch("zmxy.core.Session.refresh", lambda s: info):
            return conn, run("star_maze", conn, info)

    def test_full_cycle_resets_and_opens_free_boxes(self):
        server = MazeServer()
        conn, r = self.run_it(server)
        self.assertEqual(r.status, "submitted", r.detail)
        names = conn.routes()
        self.assertEqual(names.count("goto"), 6)           # 3 章 × 2 步
        self.assertEqual(names.count("nextChapter"), 2)
        self.assertEqual(names.count("enterDungeon"), 6)   # 第 0 行取最小列号 = 战斗，Boss 也是战斗
        self.assertEqual(names.count("reset"), 1)
        self.assertEqual(names.count("getFinishRewardFree"), 3)
        self.assertEqual(conn.bodies("getFinishRewardFree")[0], {"type": 0, "rewardId": 5270})
        self.assertEqual(conn.bodies("finishDungeon")[0], {"win": 1, "time": 0})
        self.assertGreater(names.index("reset"), names.index("goto"))
        self.assertNotIn("getShopData", names)

    def test_strength_low_stops_without_goto_and_still_opens_boxes(self):
        server = MazeServer(strength=3)
        conn, r = self.run_it(server)
        self.assertEqual(r.status, "blocked")
        self.assertIn("体力不足", r.detail)
        self.assertNotIn("goto", conn.routes())
        self.assertEqual(conn.routes().count("getFinishRewardFree"), 3)

    def test_missing_strength_stops_without_goto(self):
        server = MazeServer()

        def handler(route, body):
            response = server(route, body)
            if route.endswith("getMapData"):
                response["data"].pop("mazeItem")
            return response

        conn = Fake(handler)
        info = {"everyWeekSetting": server.week}
        with mock.patch("zmxy.core.Session.refresh", lambda s: info):
            result = run("star_maze", conn, info)
        self.assertEqual(result.status, "blocked")
        self.assertIn("读不到星斗幻境体力", result.detail)
        self.assertNotIn("goto", conn.routes())

    def test_resumes_from_pending_node_event_first(self):
        server = MazeServer(pos=(0, 0))  # 停在战斗节点上，事件没做完
        conn, r = self.run_it(server)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes()[:4], ["getMapData", "enterDungeon", "finishDungeon", "getMapData"])
        self.assertEqual(conn.routes().count("goto"), 5)

    def test_blank_times_does_nothing(self):
        conn = Fake(MazeServer())
        r = run("star_maze", conn, {}, {"times": ""})
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_times_runs_that_many_rounds(self):
        server = MazeServer()
        conn = Fake(server)
        info = {"everyWeekSetting": server.week}
        with mock.patch("zmxy.core.Session.refresh", lambda s: info):
            r = run("star_maze", conn, info, {"times": 2})
        self.assertEqual(r.status, "submitted", r.detail)
        names = conn.routes()
        self.assertEqual(names.count("goto"), 12)   # 两轮 × 6 步
        self.assertEqual(names.count("reset"), 2)
        self.assertIn("第 2 轮三层通关", r.detail)

    def test_waits_for_reset_cooldown_before_resetting(self):
        server = MazeServer()
        server.timing = (1_000_000, 1_060_000)  # 冷却 180 秒，地图建了 60 秒 → 还要等 120 秒
        sleeps = []
        conn = Fake(server)
        info = {"everyWeekSetting": server.week}
        with mock.patch("zmxy.core.Session.refresh", lambda s: info),              mock.patch("zmxy.core.TaskContext.sleep", lambda self, sec, background=True: sleeps.append(sec) or True):
            r = run("star_maze", conn, info)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(115 < sleeps[0] <= 123, sleeps)
        self.assertEqual(conn.routes().count("reset"), 1)

    def test_no_wait_when_server_gives_no_timing(self):
        sleeps = []
        server = MazeServer()
        conn = Fake(server)
        info = {"everyWeekSetting": server.week}
        with mock.patch("zmxy.core.Session.refresh", lambda s: info),              mock.patch("zmxy.core.TaskContext.sleep", lambda self, sec, background=True: sleeps.append(sec) or True):
            run("star_maze", conn, info)
        self.assertEqual(sleeps, [])

    def test_boxes_limited_by_free_count_and_stock(self):
        conn, _ = self.run_it(MazeServer(week={"rewardMazeCnt": 2, "mazeReward": {"5270": 3}}))
        self.assertEqual(conn.routes().count("getFinishRewardFree"), 1)  # 本周只剩 1 次免费
        conn, _ = self.run_it(MazeServer(week={"rewardMazeCnt": 0, "mazeReward": {}}))
        self.assertEqual(conn.routes().count("getFinishRewardFree"), 0)

    def test_unknown_week_state_opens_nothing(self):
        conn = Fake(MazeServer())
        with mock.patch("zmxy.core.Session.refresh", lambda s: {}):
            r = run("star_maze", conn, {})
        self.assertNotIn("getFinishRewardFree", conn.routes())
        self.assertIn("读不到每周宝箱状态", r.detail)

    def test_reset_refused_is_not_fatal(self):
        server = MazeServer()
        server.resettable = False
        conn, r = self.run_it(server)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("重置未成功", r.detail)

    def test_ticket_routes_unreachable_and_goto_timeout_uncertain(self):
        spec = load_tasks()["star_maze"]
        self.assertFalse(any("Ticket" in r or "buy" in r.lower() for r in spec.reads | spec.writes))
        base = MazeServer()

        def boom(route, body):
            if route.endswith("goto"):
                raise TimeoutError("t")
            return base(route, body)

        conn = Fake(boom)
        with self.assertRaises(UncertainOutcome):
            run("star_maze", conn, {})
        self.assertEqual(conn.routes().count("goto"), 1)


if __name__ == "__main__":
    unittest.main()
