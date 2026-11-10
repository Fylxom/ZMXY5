"""通天河请教（周常页）。全部离线。"""

import unittest
from unittest import mock

from zmxy.core import UncertainOutcome
from zmxy.tasks import tongtian
from tests.support import Fake, run

NOW = 1_000_000_000_000  # 服务器时间（毫秒）
MIN = 60_000
INFO = {"uid": 1, "p1": {"level": 100}, "power": 1000, "everyDaySetting": {}}
OPT = {"ships": ["海舰"], "materials": [101042005, 101042006]}


def boat(uid, elapsed_min, good=1004, level=100, power=900, events=None):
    """运镖已过 elapsed_min 分钟的船（总共 15 分钟）。海舰 1004 的材料是 101042005。"""
    row = {"uid": uid, "goodType": 0, "goodId": good, "endTime": NOW + (15 - elapsed_min) * MIN, "escortTime": 15,
           "userdata": {"level": level, "fightPower": power}}
    if events is not None:
        row["robEvents"] = events
    return row


class Server:
    def __init__(self, rows, busy=(), power=1000):
        self.rows, self.busy, self.power, self.ended = rows, set(busy), power, []

    def __call__(self, route, body):
        name = route.rsplit(".", 1)[1]
        if name == "getEscortList":
            return {"list": self.rows, "serverTime": NOW}
        if name == "getPower":
            return {"p1": self.power}
        if name == "getRobEvent":
            return {"event": []}
        if name == "beginAttack":
            if body["targetId"] in self.busy:
                return "该船正在被其他人请教"
            return {"escortData": {"uid": body["targetId"]}}
        if name == "endAttack":
            self.ended.append(body)
            return {"give": [{"itemId": 101042005, "count": 1}]}


@mock.patch("time.sleep", lambda s: None)
class TongtianTests(unittest.TestCase):
    def test_consults_ready_boat_and_settles_with_escort_uid(self):
        conn = Fake(Server([boat(7, 8)]))
        r = run("tongtian_consult", conn, {**INFO, "everyDaySetting": {"tongtianRiverRobNum": 3}}, OPT)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.routes()[-3:], ["beginAttack", "createSinglePlayerCopy", "endAttack"])
        self.assertEqual(conn.bodies("endAttack"), [{"targetId": 7, "win": 1}])
        self.assertEqual(conn.bodies("createSinglePlayerCopy")[0]["mid"], 1100101)

    def test_filters_wrong_ship_material_gate_zone_and_self(self):
        rows = [boat(1, 8), boat(2, 8, good=1001), boat(3, 8, level=130), boat(4, 8, power=100), boat(5, 14.5), boat(6, 8, events=[1, 2])]
        conn = Fake(Server(rows))
        with mock.patch.object(tongtian, "MAX_WAIT_SECONDS", -1):
            r = run("tongtian_consult", conn, INFO, OPT)
        self.assertEqual(r.status, "skipped", r.detail)  # 自己(1)、木舟、等级差、战力比、天灾区、已被请教满：都不请教
        self.assertNotIn("beginAttack", conn.routes())

    def test_busy_boat_falls_through_to_next(self):
        conn = Fake(Server([boat(7, 9), boat(8, 8)], busy={7}))
        r = run("tongtian_consult", conn, {**INFO, "everyDaySetting": {"tongtianRiverRobNum": 3}}, OPT)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([b["targetId"] for b in conn.bodies("beginAttack")], [7, 8])
        self.assertEqual([b["targetId"] for b in conn.bodies("endAttack")], [8])

    def test_unknown_or_used_up_count_and_unselected(self):
        conn = Fake(Server([boat(7, 8)]))
        self.assertEqual(run("tongtian_consult", conn, {"uid": 1, "p1": {"level": 100}, "power": 1000}, OPT).status, "blocked")
        self.assertEqual(run("tongtian_consult", conn, {**INFO, "everyDaySetting": {"tongtianRiverRobNum": 4}}, OPT).status, "skipped")
        self.assertEqual(run("tongtian_consult", conn, INFO, {}).status, "skipped")
        self.assertEqual(run("tongtian_consult", conn, INFO, {"ships": ["海舰"], "materials": [101042001]}).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_end_timeout_uncertain_not_resent(self):
        base = Server([boat(7, 8)])

        def boom(route, body):
            if route.endswith("endAttack"):
                raise TimeoutError("t")
            return base(route, body)

        conn = Fake(boom)
        with self.assertRaises(UncertainOutcome):
            run("tongtian_consult", conn, {**INFO, "everyDaySetting": {"tongtianRiverRobNum": 3}}, OPT)
        self.assertEqual(conn.routes().count("endAttack"), 1)

    def test_missing_escort_uid_does_not_settle(self):
        base = Server([boat(7, 8)])
        conn = Fake(lambda route, body: {} if route.endswith("beginAttack") else base(route, body))
        r = run("tongtian_consult", conn, {**INFO, "everyDaySetting": {"tongtianRiverRobNum": 3}}, OPT)
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("endAttack", conn.routes())


if __name__ == "__main__":
    unittest.main()
