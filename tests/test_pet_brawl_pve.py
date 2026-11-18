"""万兽秘境：首通不吃次数、已通关的关只用免费次数、每局报赢、结算或核对不对就停。"""

import unittest
from unittest import mock

from zmxy.tasks import pet_brawl_pve as pve
from tests.support import Fake, run

LEVELS = [2200101, 2200102, 2200103, 2200104, 2200105, 2200106]
ROWS = [{"id": c, "prevId": 1 if i == 0 else LEVELS[i - 1], "layerId": 1, "name": f"关{i + 1}"} for i, c in enumerate(LEVELS)]
HELPER = {"dayNumMax": 2, "challengeTicket": 100}  # 真表 cg1_20261116_2 PetBattleHelperPve / PetBattleDungeonPve（测试快照里没有）


class Server:
    """假服务器：记着通关和今日次数；每关先赢 3 局结算。"""

    def __init__(self, copy=(), count=0, win_type=1, count_step=1, session=True):
        self.copy, self.count = {str(c): 1 for c in copy}, count
        self.win_type, self.count_step, self.session = win_type, count_step, session
        self.cur, self.wins, self.round = None, 0, 0

    def petpve(self):
        return {"petPve": {"count": self.count, "copy": dict(self.copy), "maxCopyId": 2200101}}

    def __call__(self, route, body):
        name = route.rsplit(".", 1)[1]
        if name == "getPetPVPInfo":
            assert body == {"type": 2}
            return self.petpve()
        if name == "enterPveWait":
            self.cur, self.wins, self.round = str(body["copyId"]), 0, 0
            return {"self": {}, "robot": {}, "session": 1, "time": 15}
        if name == "startPveBattle":
            self.round += 1
            return {"session": self.round} if self.session else {}
        if name == "endPveBattle":
            self.wins += body["btlResult"] == 5
            if self.wins < 3:
                return {"session": self.round + 1, "btlWin": ["me"] * self.wins}
            if self.win_type == 1:
                if self.cur in self.copy:
                    self.count += self.count_step
                self.copy[self.cur] = 1
            return {"finish": True, "winType": self.win_type,
                    "give": [{"type": "coin", "subType": "petBtlMoney", "count": 10}], **self.petpve()}
        return None


class PetBrawlPveTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("WAIT_DELAY", (0.0, 0.0)), ("FIGHT_DELAY", (0.0, 0.0)),
                            ("levels", lambda: [dict(r) for r in ROWS]), ("helper", lambda: dict(HELPER))):
            patcher = mock.patch.object(pve, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def go(self, server):
        conn = Fake(server)
        return conn, run(pve.ID, conn, {"uid": 7})

    def entered(self, conn):
        return [b["copyId"] for b in conn.bodies("enterPveWait")]

    def test_first_clears_then_free_runs_on_highest_level(self):
        server = Server()
        conn, r = self.go(server)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(self.entered(conn), LEVELS + [2200106, 2200106])
        self.assertEqual(server.count, 2)
        self.assertTrue(r.day_done)
        self.assertIn("今日免费次数剩 0", r.detail)

    def test_never_enters_a_cleared_level_without_free_runs(self):
        conn, r = self.go(Server(copy=LEVELS, count=2))
        self.assertEqual(r.status, "skipped", r.detail)
        self.assertEqual(self.entered(conn), [])
        self.assertIn("今日免费次数已用完", r.detail)

    def test_reports_win_with_hp_order_by_session(self):
        conn, _ = self.go(Server(copy=LEVELS, count=1))
        for body, session in zip(conn.bodies("endPveBattle"), (1, 2, 3)):
            self.assertEqual(body["btlResult"], 5)
            self.assertNotIn("roomId", body)
            mine, robot = body["hps"] if session % 2 else body["hps"][::-1]
            self.assertEqual(robot, 0.0)
            self.assertGreater(mine, 0.0)

    def test_finish_that_is_not_a_win_stops(self):
        conn, r = self.go(Server(win_type=0))
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(self.entered(conn), [2200101])
        self.assertIn("不是胜", r.detail)

    def test_free_run_count_mismatch_stops(self):
        conn, r = self.go(Server(copy=LEVELS, count=0, count_step=0))
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(self.entered(conn), [2200106])
        self.assertIn("没对上", r.detail)

    def test_missing_session_does_not_report(self):
        conn, r = self.go(Server(session=False))
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertFalse(conn.bodies("endPveBattle"))

    def test_missing_tables_block_before_any_request(self):
        with mock.patch.object(pve, "levels", lambda: []):
            conn, r = self.go(Server())
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(conn.sent, [])

    def test_server_rejection_stops(self):
        conn, r = self.go(lambda route, body: "未开放" if route.endswith("enterPveWait") else Server()(route, body))
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertIn("未开放", r.detail)


if __name__ == "__main__":
    unittest.main()
