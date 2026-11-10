"""造梦迷宫（日常页）：先打推荐榜（元宝），再打热度 ≥500 且通关率 <10% 的迷宫（迷宫币）。全部离线。"""

import json
import unittest

from zmxy.core import UncertainOutcome, load_tasks
from tests.support import Fake, run

ROOMS = {"1": {"roomId": 1, "door": {"a": {"id": 2}}}, "2": {"roomId": 2, "door": [{"id": 12000}]}}
YUANBAO = {"give": [{"type": "coin", "subType": "yuanbao", "count": 5}]}
COIN = {"give": [{"type": "coin", "subType": "freeMazeMoney", "count": 10}]}


def row(uid, inn=None, out=None):
    r = {"uid": uid, "pos": 0, "name": f"m{uid}"}
    if inn is not None:
        r.update(inCnt=inn, outCnt=out)
    return r


class FreeMazeTests(unittest.TestCase):
    def conn(self, boards=None, rooms=None, end=None):
        boards = boards if boards is not None else {80: [row(9)], 90: [], 110: []}

        def handler(route, body):
            name = route.rsplit(".", 1)[1]
            if name == "getRank":
                return {"rows": boards.get(body["type"], []) if body["page"] == 1 else []}
            if name == "getMazeData":
                return {"data": {"data": json.dumps(rooms or ROOMS)}}
            if name == "endChallenge":
                return end(body) if callable(end) else (end if end is not None else YUANBAO)
        return Fake(handler)

    def challenged(self, conn):
        return [b["uid"] for b in conn.bodies("beginChallenge")]

    def test_is_a_daily_task_without_times(self):
        spec = load_tasks()["free_maze"]
        self.assertEqual((spec.group, spec.times_max), ("日常", 0))

    def test_recommend_board_first_then_coin_mazes_only(self):
        boards = {80: [row(7, 10, 9)], 90: [row(21, 600, 30), row(22, 600, 90), row(23, 499, 0), row(24)], 110: [row(25, 900, 0)]}
        conn = self.conn(boards, end=lambda b: YUANBAO if len(conn.bodies("endChallenge")) == 1 else COIN)
        r = run("free_maze", conn, {"uid": 5, "everyDaySetting": {}})
        self.assertEqual(r.status, "submitted", r.detail)
        # 推荐榜的 7 先打；其后只有热度≥500 且通关率<10% 的 21、25（22 通关率 15%，23 热度不够，24 没数据）
        self.assertEqual(self.challenged(conn), [7, 21, 25])
        self.assertEqual(conn.bodies("endChallenge")[0], {"type": "game pass", "win": True, "criticism": {"0": 0, "1": 0, "2": 0}})

    def test_skips_own_and_duplicates(self):
        conn = self.conn({80: [row(5), row(9)], 90: [row(9, 800, 0)], 110: []})
        run("free_maze", conn, {"uid": 5, "everyDaySetting": {}})
        self.assertEqual(self.challenged(conn), [9])

    def test_coin_cap_stops_coin_mazes(self):
        boards = {80: [], 90: [row(21, 600, 0), row(22, 600, 0), row(23, 600, 0)], 110: []}
        conn = self.conn(boards, end=COIN)
        run("free_maze", conn, {"uid": 5, "everyDaySetting": {"userMazeMoney": 30, "userMazeYuanbao": 200}})
        self.assertEqual(self.challenged(conn), [21, 22])  # 还差 20，每个 10

    def test_stops_after_dry_streak(self):
        conn = self.conn({80: [row(i) for i in range(10, 30)], 90: [], 110: []}, end={})
        run("free_maze", conn, {"uid": 5, "everyDaySetting": {}})
        self.assertEqual(len(self.challenged(conn)), 6)  # 连续 6 个没奖励就停，不打满 20 个

    def test_server_daily_counter_wins(self):
        boards = {80: [], 90: [row(i, 600, 0) for i in range(21, 31)], 110: []}
        conn = self.conn(boards, end={"everyDaySetting": {"userMazeMoney": 50}, **COIN})
        run("free_maze", conn, {"uid": 5, "everyDaySetting": {"userMazeYuanbao": 200}})
        self.assertEqual(self.challenged(conn), [21])  # 响应里已得 50 = 满，掉落数只有 10 也要停

    def test_caps_reached_skips_without_requests(self):
        conn = self.conn()
        r = run("free_maze", conn, {"uid": 5, "everyDaySetting": {"userMazeMoney": 50, "userMazeYuanbao": 200}})
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_missing_or_malformed_daily_counters_block_without_requests(self):
        for info in ({"uid": 5}, {"uid": 5, "everyDaySetting": {"userMazeMoney": "bad"}}):
            with self.subTest(info=info):
                conn = self.conn()
                result = run("free_maze", conn, info)
                self.assertEqual(result.status, "blocked")
                self.assertEqual(conn.sent, [])

    def test_coin_full_stops_even_if_yuanbao_left(self):
        conn = self.conn()
        r = run("free_maze", conn, {"uid": 5, "everyDaySetting": {"userMazeMoney": 50}})
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_stops_right_when_coin_reaches_50(self):
        conn = self.conn({80: [row(7), row(8)], 90: [], 110: []}, end=COIN)  # 推荐榜的迷宫这次也掉了迷宫币
        run("free_maze", conn, {"uid": 5, "everyDaySetting": {"userMazeMoney": 40}})
        self.assertEqual(self.challenged(conn), [7])

    def test_yuanbao_full_ignores_recommend_board(self):
        conn = self.conn({80: [row(7, 10, 9)], 90: [row(21, 600, 0)], 110: []}, end=COIN)
        run("free_maze", conn, {"uid": 5, "everyDaySetting": {"userMazeYuanbao": 200}})
        self.assertEqual(self.challenged(conn), [21])

    def test_unreachable_exit_skipped_and_never_begun(self):
        conn = self.conn(rooms={"1": {"roomId": 1, "door": []}, "2": {"roomId": 2, "door": [{"id": 12000}]}})
        r = run("free_maze", conn, {"uid": 5, "everyDaySetting": {}})
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("beginChallenge", conn.routes())

    def test_no_targets_or_unknown_uid(self):
        self.assertEqual(run("free_maze", self.conn({80: [], 90: [row(21, 100, 0)], 110: []}), {"uid": 5, "everyDaySetting": {}}).status, "skipped")  # 没有就不强求
        self.assertEqual(run("free_maze", self.conn(), {}).status, "blocked")

    def test_missing_out_count_never_qualifies_for_coin(self):
        for candidate in (row(21, 600), row(22, 600, -1)):
            with self.subTest(candidate=candidate):
                conn = self.conn({80: [], 90: [candidate], 110: []})
                result = run("free_maze", conn, {"uid": 5, "everyDaySetting": {}})
                self.assertEqual(result.status, "skipped")
                self.assertNotIn("beginChallenge", conn.routes())

    def test_end_timeout_not_resent(self):
        base = self.conn()

        def boom(route, body):
            if route.endswith("endChallenge"):
                raise TimeoutError("t")
            return base.handler(route, body)

        conn = Fake(boom)
        with self.assertRaises(UncertainOutcome):
            run("free_maze", conn, {"uid": 5, "everyDaySetting": {}})
        self.assertEqual(conn.routes().count("endChallenge"), 1)


if __name__ == "__main__":
    unittest.main()
