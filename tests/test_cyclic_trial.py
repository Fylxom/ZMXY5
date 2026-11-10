"""轮回试炼地阶目标与自动领奖的离线回归。"""

import threading
import unittest
from unittest import mock

from tests.support import FakeConn, run
from zmxy.tasks import cyclic_trial as cyclic


class CyclicTrialTests(unittest.TestCase):
    ACTIVITY = {"shelfTime": "2026/10/19-23:59:59", "showShelfTime": "2026/10/19-23:59:59",
                "underTime": "2026/11/1-23:59:59", "showUnderTime": "2026/11/8-23:59:59"}

    def at(self, text):
        from datetime import datetime
        from zmxy import state
        return int(datetime.strptime(text, "%Y/%m/%d %H:%M").replace(tzinfo=state._CN).timestamp())

    def test_open_until_challenge_deadline_not_until_delisting(self):
        with mock.patch.object(cyclic.state, "row", return_value=self.ACTIVITY), \
             mock.patch.object(cyclic, "_config", return_value={"activityId": 1}):
            self.assertFalse(cyclic._open(self.at("2026/10/19 12:00")))
            self.assertTrue(cyclic._open(self.at("2026/10/25 12:00")))
            self.assertTrue(cyclic._open(self.at("2026/11/1 23:59")))
            self.assertFalse(cyclic._open(self.at("2026/11/2 00:01")))   # 挑战截止后、下架前：不再挑战
            self.assertFalse(cyclic._open(self.at("2026/11/8 12:00")))

    def test_after_deadline_says_so_and_sends_nothing(self):
        info = {"rebirthTower": {"cur": 0, "reward": {}}}
        conn = FakeConn({})
        with mock.patch.object(cyclic.state, "row", return_value=self.ACTIVITY), \
             mock.patch.object(cyclic, "_config", return_value={"activityId": 1}):
            result = run(cyclic.ID, conn, info, options={"mode": "13"}, now=self.at("2026/11/5 12:00"))
        self.assertEqual(result.status, "skipped")
        self.assertIn("挑战已截止", result.detail)
        self.assertIn("11/01 23:59", result.detail)
        self.assertEqual(conn.sent, [])

    def test_missing_deadline_is_not_open(self):
        with mock.patch.object(cyclic.state, "row", return_value={**self.ACTIVITY, "underTime": "-1"}), \
             mock.patch.object(cyclic, "_config", return_value={"activityId": 1}):
            self.assertFalse(cyclic._open(self.at("2026/10/25 12:00")))

    def test_targets_are_earth_floors_in_total_order(self):
        self.assertEqual(cyclic.MODES, (("13", "地三"), ("15", "地五"), ("20", "地十")))
        self.assertEqual(cyclic._target({}), 13)
        self.assertEqual(cyclic._target({"mode": "20"}), 20)

    def test_dungeon_table_keeps_earth_floors_in_the_middle(self):
        ids = list(range(1800101, 1800131))
        rows = {
            mid: {"id": mid, "type": 16, "difficult": 3,
                  "name": f"参水猿·{'人阶' if floor <= 10 else '地阶' if floor <= 20 else '天阶'}"
                          f"{floor}"}
            for floor, mid in enumerate(ids, 1)
        }
        with mock.patch.object(cyclic, "_config", return_value={"dungeonId": ";".join(map(str, ids))}), \
             mock.patch.object(cyclic.data, "row", side_effect=lambda table, key: rows.get(int(key))):
            found = cyclic._dungeon_ids()
        self.assertEqual(found[12:20], list(range(1800113, 1800121)))

    def test_direct_client_chain_stops_exactly_at_selected_floor(self):
        ids = list(range(1800101, 1800131))
        info = {"rebirthTower": {"cur": 0, "reward": {}}}
        opened, ended = [], []

        def respond(route, body):
            if route == cyclic.R_OPEN:
                floor = info["rebirthTower"]["cur"]
                opened.append((floor, body))
                return {"dungeonId": ids[floor], "needReport": 0}
            if route == cyclic.R_END:
                ended.append(body)
                info["rebirthTower"]["cur"] += 1
                return {"result": 1, "rebirthTower": info["rebirthTower"]}
            return {}

        conn = FakeConn({cyclic.R_OPEN: lambda body: respond(cyclic.R_OPEN, body),
                         cyclic.R_END: lambda body: respond(cyclic.R_END, body)})
        from zmxy import login, protocol as p
        from zmxy.core import Session

        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, info
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": 1_700_000_000})
        session.refresh = lambda: info
        with mock.patch.object(cyclic, "_open", return_value=True), mock.patch.object(cyclic, "_dungeon_ids", return_value=ids):
            result = run(cyclic.ID, conn, info, options={"mode": "13"}, session=session)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(len(opened), 13)
        self.assertEqual([floor for floor, _ in opened], list(range(13)))
        self.assertEqual(len(ended), 13)
        self.assertTrue(all(body == {"bossHp": 0} for body in ended))
        self.assertEqual(info["rebirthTower"]["cur"], 13)

    def test_open_response_mismatch_blocks_without_end(self):
        ids = list(range(1800101, 1800131))
        conn = FakeConn({cyclic.R_OPEN: {"dungeonId": ids[1], "needReport": 0}})
        info = {"rebirthTower": {"cur": 0, "reward": {}}}
        with mock.patch.object(cyclic, "_open", return_value=True), mock.patch.object(cyclic, "_dungeon_ids", return_value=ids):
            result = run(cyclic.ID, conn, info, options={"mode": "13"})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(conn.routes(), [cyclic.R_OPEN])

    def test_progress_jump_stops_without_trying_next_floor(self):
        ids = list(range(1800101, 1800131))
        info = {"rebirthTower": {"cur": 0, "reward": {}}}

        def respond(route, body):
            if route == cyclic.R_OPEN:
                return {"dungeonId": ids[info["rebirthTower"]["cur"]], "needReport": 0}
            info["rebirthTower"]["cur"] = 14  # 模拟服务器异常越层
            return {"result": 1}

        conn = FakeConn({cyclic.R_OPEN: lambda body: respond(cyclic.R_OPEN, body),
                         cyclic.R_END: lambda body: respond(cyclic.R_END, body)})
        from zmxy import login, protocol as p
        from zmxy.core import Session

        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, info
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": 1_700_000_000})
        session.refresh = lambda: info
        with mock.patch.object(cyclic, "_open", return_value=True), mock.patch.object(cyclic, "_dungeon_ids", return_value=ids):
            result = run(cyclic.ID, conn, info, options={"mode": "13"}, session=session)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(conn.routes(), [cyclic.R_OPEN, cyclic.R_END])

    def test_reached_target_claims_pending_rewards(self):
        reward = 1800113
        conn = FakeConn({cyclic.R_REWARD: {"rebirthTower": {"cur": 13, "reward": {str(reward): 2}}, "give": [{"itemId": 101020002, "count": 1}]}})
        info = {"rebirthTower": {"cur": 13, "reward": {str(reward): 1}}}
        with mock.patch.object(cyclic, "_open", return_value=True), mock.patch.object(cyclic, "_dungeon_ids", return_value=list(range(1800101, 1800131))):
            result = run(cyclic.ID, conn, info, options={"mode": "13"})
        self.assertEqual(result.status, "submitted")
        self.assertEqual(conn.sent, [(cyclic.R_REWARD, {"id": reward})])
        self.assertIn("自动领取 1 个奖励", result.detail)

    def climb(self, end, stop=None):
        """从第 0 层开始打；end(已打层数) 决定结算响应，返回 (结果, 连接)。"""
        ids = list(range(1800101, 1800131))
        info = {"rebirthTower": {"cur": 0, "reward": {}}}

        def finish(body):
            response = end(info["rebirthTower"]["cur"])
            info["rebirthTower"]["cur"] += 1
            return response

        conn = FakeConn({cyclic.R_OPEN: lambda body: {"dungeonId": ids[info["rebirthTower"]["cur"]]},
                         cyclic.R_END: finish})
        from zmxy import login, protocol as p
        from zmxy.core import Session

        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, info
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": 1_700_000_000})
        session.refresh = lambda: info
        with mock.patch.object(cyclic, "_open", return_value=True), mock.patch.object(cyclic, "_dungeon_ids", return_value=ids):
            return run(cyclic.ID, conn, info, options={"mode": "13"}, session=session, stop=stop), conn

    def test_stop_midway_keeps_confirmed_floors_as_blocked(self):
        stop = threading.Event()

        def end(floor):
            if floor == 1:
                stop.set()
            return {"result": 1}

        result, conn = self.climb(end, stop)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("挑战 2 层", result.detail)
        self.assertIn("用户停止", result.detail)
        self.assertEqual(conn.routes().count(cyclic.R_END), 2)

    def test_stop_before_any_progress_is_skipped(self):
        stop = threading.Event()
        stop.set()
        result, conn = self.climb(lambda floor: {"result": 1}, stop)
        self.assertEqual(result.status, "skipped", result.detail)
        self.assertEqual(conn.sent, [])

    def test_refused_settlement_keeps_confirmed_floors(self):
        result, conn = self.climb(lambda floor: {"result": 0 if floor == 2 else 1})
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("挑战 2 层", result.detail)
        self.assertIn("结算未完成", result.detail)


if __name__ == "__main__":
    unittest.main()
