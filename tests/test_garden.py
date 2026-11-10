"""联盟药园（定时页）：采摘、铲除、浇灌。全部离线。"""

import copy
import unittest
from unittest import mock

from tests.support import Fake, run
from zmxy import guard
from zmxy.core import TaskContext, UncertainOutcome, load_tasks

NOW = 1_760_000_000_000  # 药园里的时间都是毫秒
HOUR = 3_600_000
ME, PAL, SHIELDED = 100, 200, 300
PLANT = 180000001  # 经验瑶草，每棵最多浇 5 次


def soil(finish=NOW - 1, pick=0, water=0, plant=PLANT):
    return {"plantId": plant, "finishTime": finish, "needTime": 20 * HOUR, "pickNum": pick, "wateringNum": water, "fertilizeNum": 0}


def info(used=0, league=True):
    out = {"everyDaySetting": {"wateringFreeNum": used}}
    if league:
        out["league"] = {"leagueId": "L1", "leagueDevote": {}}
    return out


class World:
    """假服务器：自己和盟友的药园，照客户端的规则改状态。"""

    def __init__(self, mine=None, pals=None, used=0, shield=()):
        self.gardens = {ME: mine or {}, **(pals or {})}
        self.used, self.shield, self.refuse, self.frozen = used, set(shield), {}, set()

    def garden(self, uid):
        return {"uid": uid, "serverTime": NOW, "protectEndTime": NOW + HOUR if uid in self.shield else 0,
                "soil": copy.deepcopy(self.gardens[uid])}

    def __call__(self, route, body):
        name = route.rsplit(".", 1)[1]
        if name in self.refuse:
            return self.refuse[name]
        if name == "getGardenInfo":
            return {"garden": self.garden(ME)}
        if name == "getLeagueInfo":
            return {"league": {"users": {str(u): {"uid": u} for u in self.gardens}}}
        if name == "getGardenSimpleInfo":
            return {"data": [{"uid": u, "plant": [s["plantId"] for s in g.values()],
                              "times": [[s["finishTime"], s["needTime"]] for s in g.values()]} for u, g in self.gardens.items() if u in body["uids"]]}
        if name == "getFriendGardenInfo":
            return {"garden": self.garden(body["uid"])}
        plot = self.gardens[body["targetId"]]
        key = str(body["soilIndex"])
        if name not in self.frozen:
            if name == "pick":
                plot[key]["pickNum"] = 1
            elif name == "uproot":
                del plot[key]
            elif name == "water":
                plot[key]["wateringNum"] += 1
                plot[key]["finishTime"] -= 2 * HOUR
                self.used += 1
        out = {"garden": self.garden(body["targetId"])}
        if name == "pick":
            out["give"] = [{"type": "item", "id": 101707001, "count": 1}]
        if name == "water":
            out["everyDaySetting"] = {"wateringFreeNum": self.used}
        return out


@mock.patch.object(TaskContext, "sleep", lambda ctx, seconds, background=True: True)
class GardenTests(unittest.TestCase):
    def go(self, world, information=None, **kw):
        conn = Fake(world)
        return run("garden", conn, information if information is not None else info(world.used), **kw), conn

    def test_picks_then_uproots_ripe_and_leaves_growing(self):
        world = World({"0": soil(), "1": soil(finish=NOW + HOUR), "2": soil(pick=1)})
        r, conn = self.go(world)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(conn.bodies("pick"), [{"plantId": PLANT, "soilIndex": 0, "ticket": 0, "targetId": ME}])
        self.assertEqual([b["soilIndex"] for b in conn.bodies("uproot")], [0, 2])
        self.assertEqual(list(world.gardens[ME]), ["1"])  # 没成熟的不铲
        self.assertIn("采摘 经验瑶草", r.detail)
        self.assertIn("铲除 经验瑶草、经验瑶草", r.detail)

    def test_waters_growing_allies_until_free_count_runs_out(self):
        world = World(pals={PAL: {"0": soil(finish=NOW + 30 * HOUR, water=3), "1": soil(finish=NOW + 30 * HOUR)}}, used=1)
        r, conn = self.go(world)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual([(b["targetId"], b["soilIndex"]) for b in conn.bodies("water")], [(PAL, 0), (PAL, 0), (PAL, 1), (PAL, 1)])  # 第一棵浇到上限 5，剩下的给第二棵
        self.assertEqual(world.used, 5)
        self.assertEqual(conn.bodies("getGardenSimpleInfo"), [{"uids": [PAL]}])  # 不含自己
        self.assertIn("浇灌 4 次", r.detail)

    def test_water_skips_ripe_shielded_and_own_garden(self):
        world = World({"0": soil(finish=NOW + HOUR)}, {PAL: {"0": soil()}, SHIELDED: {"0": soil(finish=NOW + 9 * HOUR)}}, shield={SHIELDED})
        r, conn = self.go(world)
        self.assertEqual((r.status, conn.bodies("water")), ("skipped", []), r.detail)
        self.assertEqual(conn.bodies("getFriendGardenInfo"), [{"uid": SHIELDED}])  # 植物都成熟了的盟友不用进去看

    def test_water_stops_once_plant_ripens(self):
        world = World(pals={PAL: {"0": soil(finish=NOW + 3 * HOUR)}})
        r, conn = self.go(world)
        self.assertEqual(len(conn.bodies("water")), 2, r.detail)  # 每次缩短 2 小时，两次就熟了

    def test_no_water_left_or_unreadable_sends_no_water(self):
        pals = {PAL: {"0": soil(finish=NOW + 30 * HOUR)}}
        for information, text in ((info(5), "已用完"), ({"league": {"leagueId": "L1"}}, "读不到今日仙露次数")):
            r, conn = self.go(World(pals=copy.deepcopy(pals)), information)
            self.assertEqual(conn.routes(), ["getGardenInfo"], r.detail)
            self.assertIn(text, r.detail)

    def test_same_connection_does_not_reuse_water(self):
        from zmxy import protocol as p
        from zmxy.core import Session

        session = Session(p.ClientConfig(uid=1, username="u"))
        world = World(pals={PAL: {str(i): soil(finish=NOW + 30 * HOUR) for i in range(4)}}, used=2)
        stale = info(2)  # 第二次执行拿到的还是登录时的快照
        for _ in range(2):
            run("garden", Fake(world), stale, session=session)
        self.assertEqual(world.used, 5)

    def test_no_league_or_no_garden_data_sends_nothing_more(self):
        r, conn = self.go(World(), info(league=False))
        self.assertEqual((r.status, conn.routes()), ("skipped", []), r.detail)
        world = World({"0": soil()})
        world.refuse["getGardenInfo"] = {}
        r, conn = self.go(world)
        self.assertEqual((r.status, conn.routes()), ("blocked", ["getGardenInfo"]), r.detail)

    def test_pick_not_confirmed_stops_before_uproot_and_water(self):
        world = World({"0": soil(), "1": soil(pick=1)}, {PAL: {"0": soil(finish=NOW + 30 * HOUR)}})
        world.frozen.add("pick")
        r, conn = self.go(world)
        self.assertEqual((r.status, conn.routes()), ("blocked", ["getGardenInfo", "pick"]), r.detail)

    def test_pick_refused_stops(self):
        world = World({"0": soil(), "1": soil(pick=1)})
        world.refuse["pick"] = "背包已满"
        r, conn = self.go(world)
        self.assertEqual((r.status, conn.routes()), ("blocked", ["getGardenInfo", "pick"]), r.detail)

    def test_uproot_not_confirmed_stops(self):
        world = World({"0": soil(pick=1), "1": soil(pick=1)})
        world.frozen.add("uproot")
        r, conn = self.go(world)
        self.assertEqual((r.status, conn.routes()), ("blocked", ["getGardenInfo", "uproot"]), r.detail)

    def test_water_not_confirmed_stops_without_resend(self):
        world = World(pals={PAL: {"0": soil(finish=NOW + 30 * HOUR)}})
        world.frozen.add("water")
        r, conn = self.go(world)
        self.assertEqual((r.status, len(conn.bodies("water"))), ("blocked", 1), r.detail)

    def test_water_refusals_give_up_after_three(self):
        world = World(pals={u: {"0": soil(finish=NOW + 30 * HOUR)} for u in (200, 201, 202, 203, 204)})
        world.refuse["water"] = "不能浇灌"
        r, conn = self.go(world)
        self.assertEqual((r.status, len(conn.bodies("water"))), ("skipped", 3), r.detail)

    def test_write_timeout_is_uncertain(self):
        from zmxy import protocol as p

        world = World({"0": soil()})

        def handler(route, body):
            if route.endswith(".pick"):
                raise p.AllianceError("timeout")
            return world(route, body)

        with self.assertRaises(UncertainOutcome):
            run("garden", Fake(handler), info())

    def test_routes_pass_guard_and_ticket_route_is_not_declared(self):
        spec = load_tasks()["garden"]
        self.assertEqual(spec.group, "周常")
        self.assertFalse(any("Ticket" in r for r in spec.reads | spec.writes))
        self.assertIsNone(guard.check("scene.gardenHandler.water", {"plantId": PLANT, "soilIndex": 0, "ticket": 0, "targetId": PAL}))
        self.assertIsNotNone(guard.check("scene.gardenHandler.water", {"plantId": PLANT, "soilIndex": 0, "ticket": 3, "targetId": PAL}))


if __name__ == "__main__":
    unittest.main()
