"""周常页任务（镇妖塔、噩梦天树王、星斗幻境、十绝阵、造梦迷宫、时光之舟、通天河、师徒试炼）。全部离线。"""

import unittest
from unittest import mock

from zmxy import protocol as p
from zmxy import login
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from tests.support import Fake, run

TOWER_IDS = [100001, 100002, 100003, 100004, 100005]


def tower_state(layer=1, index=-1, count=1, best=3, ids=TOWER_IDS):
    return {"demonTower": {"cur": {"layer": layer, "index": index, "towerIds": list(ids)}, "count": count, "bestRecord": {"layer": best}}}


class TowerServer:
    """一个最小的镇妖塔服务器：begin 第一层扣 1 次，end 推进 index。"""

    def __init__(self, count=1, layer=1, drops=True, fail=None):
        self.count, self.layer, self.index, self.drops, self.fail = count, layer, -1, drops, fail
        self.begun = False

    def __call__(self, route, body):
        name = route.rsplit(".", 1)[1]
        if self.fail == name:
            return "服务器拒绝"
        if name == "switchToLayer":
            if body["layer"]:
                self.layer = body["layer"]
            return tower_state(self.layer, self.index, self.count)
        if name == "beginChallenge":
            assert body == {"type": 0}
            if not self.begun:
                self.count, self.begun = self.count - 1, True
            return tower_state(self.layer, self.index, self.count)
        if name == "endChallenge":
            self.index += 1
            if self.index == 4:
                self.begun, self.index = False, -1
                self.index_after = 4
            return {**tower_state(self.layer, self.index if self.index >= 0 else 4, self.count),
                    **({"give": [{"itemId": 101045121, "count": 1, "dropIndex": self.index}]} if self.drops else {})}
        if name == "getReward":
            return {"give": [{"itemId": 101045121, "count": 1}]}


class DemonTowerTests(unittest.TestCase):
    OPT = {"levels": [1], "times": 1}

    def test_full_group(self):
        server = TowerServer()
        conn = Fake(server)
        r = run("demon_tower", conn, {}, self.OPT)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("5 个小层", r.detail)
        self.assertIn("×5", r.detail)  # 每层 1 个：结算的 give 和 getReward 的回执是同一份，不能算成 10
        self.assertEqual(conn.routes().count("beginChallenge"), 5)
        self.assertEqual(conn.routes().count("endChallenge"), 5)
        self.assertEqual(conn.routes().count("getReward"), 5)
        self.assertEqual(server.count, 0)
        # 小层之间不能再切层组：switchToLayer 只在开头和结尾各一次
        self.assertEqual(conn.routes().count("switchToLayer"), 2)

    def test_no_free_count_and_unselected(self):
        conn = Fake(TowerServer(count=0))
        self.assertEqual(run("demon_tower", conn, {}, self.OPT).status, "skipped")
        self.assertNotIn("beginChallenge", conn.routes())
        self.assertEqual(run("demon_tower", conn, {}, {"levels": [], "times": 1}).status, "skipped")
        self.assertEqual(run("demon_tower", conn, {}, {"levels": [999], "times": 1}).status, "blocked")

    def test_locked_layer_not_switched(self):
        conn = Fake(TowerServer())
        r = run("demon_tower", conn, {}, {"levels": [16], "times": 1})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes(), ["switchToLayer"])

    def test_unknown_count_blocks(self):
        conn = Fake(lambda route, body: {"demonTower": {"cur": {"layer": 1, "index": -1, "towerIds": TOWER_IDS}}})
        self.assertEqual(run("demon_tower", conn, {}, self.OPT).status, "blocked")
        self.assertNotIn("beginChallenge", conn.routes())

    def test_extra_count_consumed_stops(self):
        server = TowerServer(count=3)
        def handler(route, body):
            out = server(route, body)
            if route.endswith("beginChallenge") and server.index == 0:
                server.count -= 1  # 第二层又多扣一次
                out = tower_state(server.layer, server.index, server.count)
            return out
        conn = Fake(handler)
        r = run("demon_tower", conn, {}, self.OPT)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes().count("beginChallenge"), 2)

    def test_reward_without_give_stops(self):
        server = TowerServer()
        def handler(route, body):
            return {} if route.endswith("getReward") else server(route, body)
        conn = Fake(handler)
        r = run("demon_tower", conn, {}, self.OPT)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes().count("beginChallenge"), 1)  # 没有 give 发放记录，不继续下一层

    def test_end_timeout_is_uncertain_and_never_resent(self):
        server = TowerServer()
        def handler(route, body):
            if route.endswith("endChallenge"):
                raise TimeoutError("t")
            return server(route, body)
        conn = Fake(handler)
        with self.assertRaises(UncertainOutcome):
            run("demon_tower", conn, {}, self.OPT)
        self.assertEqual(conn.routes().count("endChallenge"), 1)

    def test_bag_full_mid_group_counts_as_one_group_on_resume(self):
        """背包满停在第 2 组中途：次数已扣，这一组算已打；设置 5 次，继续时只再打 3 组。"""
        server, full = TowerServer(count=5), [True]
        def handler(route, body):
            if route.endswith("getReward") and full[0] and server.count == 3 and server.index == 1:
                return "灵符背包已满"
            return server(route, body)
        ctx_progress = {}
        def go(options):
            s = Session(p.ClientConfig(uid=1, username="u"))
            conn = Fake(handler)
            s._conn, s.info = conn, {}
            s._link = login.Link(conn, s.config, [{"index": 0}], {})
            s.bind(load_tasks()["demon_tower"])
            ctx = TaskContext(s, options)
            r = load_tasks()["demon_tower"].run(ctx)
            ctx_progress.clear(); ctx_progress.update(ctx.progress)
            return r, conn
        r, _ = go({"levels": [1], "times": 5})
        self.assertEqual(r.status, "blocked")
        self.assertIn("背包已满", r.detail)
        self.assertEqual(ctx_progress, {"1": 2})
        self.assertEqual(server.count, 3)
        full[0] = False
        server.begun, server.index = False, -1  # 重新登录后服务器从新的一组开始
        r, conn = go({"levels": [1], "times": 5, "resume_done": dict(ctx_progress)})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("完成 3 组", r.detail)
        self.assertEqual(server.count, 0)
        r, conn = go({"levels": [1], "times": 5, "resume_done": {"1": 5}})
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_only_free_type_zero(self):
        conn = Fake(TowerServer())
        run("demon_tower", conn, {}, self.OPT)
        self.assertTrue(all(b == {"type": 0} for b in conn.bodies("beginChallenge")))


class NightmareTreeKingTests(unittest.TestCase):
    def conn(self, cnt=2, drop=None, boss=700601):
        state = {"cnt": cnt}

        def handler(route, body):
            name = route.rsplit(".", 1)[1]
            if name == "getDungeonWingId":
                return {"dungeonId": boss, "cnt": state["cnt"]}
            if name == "beginDungeonWing":
                return {"drop": [{"id": 1}, None, {"id": 2, "dropIndex": 7}]} if drop is None else drop
            if name == "endDungeonWing":
                state["cnt"] -= 1
        return Fake(handler)

    def test_halt_after_begin_counts_when_server_count_dropped(self):
        """开打后领宝箱失败：服务器次数已扣就算一次（断点里不再补），没扣就不算。"""
        for charged, expected in ((True, {"n": 1}), (False, {"n": 0})):
            left = {"cnt": 2}

            def handler(route, body):
                name = route.rsplit(".", 1)[1]
                if name == "getDungeonWingId":
                    return {"dungeonId": 700601, "cnt": left["cnt"]}
                if name == "beginDungeonWing":
                    left["cnt"] -= charged
                    return {"drop": [{"id": 1}]}
                if name == "getDungeonWingReward":
                    return "背包已满"
            s = Session(p.ClientConfig(uid=1, username="u"))
            conn = Fake(handler)
            s._conn, s.info = conn, {}
            s._link = login.Link(conn, s.config, [{"index": 0}], {})
            s.bind(load_tasks()["tree_king_nightmare"])
            ctx = TaskContext(s, {"times": 2})
            r = load_tasks()["tree_king_nightmare"].run(ctx)
            self.assertEqual(r.status, "blocked")
            self.assertEqual(ctx.progress, expected)
            self.assertNotIn("endDungeonWing", conn.routes())

    def test_runs_until_count_used_up(self):
        conn = self.conn()
        r = run("tree_king_nightmare", conn, {}, {"times": 2})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("×2", r.detail)
        self.assertEqual([b["index"] for b in conn.bodies("getDungeonWingReward")], [0, 7, 0, 7])
        self.assertEqual(conn.bodies("endDungeonWing")[0]["win"], 0)
        self.assertEqual(conn.routes().count("beginDungeonWing"), 2)

    def test_zero_unknown_or_foreign_boss(self):
        self.assertEqual(run("tree_king_nightmare", self.conn(cnt=0), {}).status, "skipped")
        conn = Fake(lambda route, body: {"dungeonId": 700601})  # 没有 cnt
        self.assertEqual(run("tree_king_nightmare", conn, {}).status, "blocked")
        conn = self.conn(boss=700101)  # 普通天树王不是噩梦妖王
        self.assertEqual(run("tree_king_nightmare", conn, {}).status, "blocked")
        self.assertNotIn("beginDungeonWing", conn.routes())

    def test_missing_or_empty_drop_does_not_settle(self):
        for drop in ({}, {"drop": []}, {"drop": [None]}):
            conn = self.conn(drop=drop)
            self.assertEqual(run("tree_king_nightmare", conn, {}).status, "blocked")
            self.assertNotIn("endDungeonWing", conn.routes())

    def test_claim_failure_does_not_settle_and_timeout_is_uncertain(self):
        base = self.conn()
        def reject(route, body):
            return "领取失败" if route.endswith("getDungeonWingReward") else base.handler(route, body)
        conn = Fake(reject)
        self.assertEqual(run("tree_king_nightmare", conn, {}).status, "blocked")
        self.assertNotIn("endDungeonWing", conn.routes())

        def boom(route, body):
            if route.endswith("endDungeonWing"):
                raise TimeoutError("t")
            return base.handler(route, body)
        with self.assertRaises(UncertainOutcome):
            run("tree_king_nightmare", Fake(boom), {})


if __name__ == "__main__":
    unittest.main()
