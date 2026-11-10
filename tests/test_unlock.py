"""关卡解锁判断（quota.locked）：前置关通关 + 角色等级，读不到一律当作未解锁；run_level 未解锁不创建。"""

import unittest
from unittest import mock

from zmxy import data, dungeon, protocol as p, quota
from zmxy.core import Session
from zmxy.tasks import special
from tests.support import Fake, run

YAMA2, YAMA1, YAMA10, SNAKE = 400201, 400101, 401001, 401101  # 普明宫(需36级,前置玄冥宫) / 玄冥宫 / 肃英宫 / 蛇之炼狱(需90级,前置肃英宫)


def info(level=120, passed=(), **extra):
    return {"p1": {"level": level}, "dungeon": {str(m): {"id": m, "passNum": 1} for m in passed}, **extra}


class LockedTests(unittest.TestCase):
    def test_first_level_only_needs_level(self):
        self.assertIsNone(quota.locked(info(1), YAMA1))
        self.assertIn("等级", quota.locked({"p1": {"level": 0}, "dungeon": {}}, YAMA2) or "")

    def test_previous_level_must_be_passed(self):
        self.assertIn("未通关", quota.locked(info(), YAMA2))
        self.assertIsNone(quota.locked(info(passed=[YAMA1]), YAMA2))
        zero = {"p1": {"level": 99}, "dungeon": {str(YAMA1): {"id": YAMA1, "passNum": 0}}}
        self.assertIn("未通关", quota.locked(zero, YAMA2))

    def test_level_requirement(self):
        self.assertIn("等级不足", quota.locked(info(level=89, passed=[YAMA10]), SNAKE))
        self.assertIsNone(quota.locked(info(level=90, passed=[YAMA10]), SNAKE))
        self.assertIsNone(quota.locked(info(level=1) | {"p2": {"level": 90}, "dungeon": info(passed=[YAMA10])["dungeon"]}, SNAKE))  # 取两个角色里高的

    def test_unreadable_is_locked(self):
        self.assertIn("等级", quota.locked({"dungeon": {}}, YAMA2))
        self.assertIn("通关记录", quota.locked({"p1": {"level": 99}}, YAMA2))
        self.assertIn("passNum", quota.locked({"p1": {"level": 99}, "dungeon": {str(YAMA1): {"id": YAMA1}}}, YAMA2))

    def test_invalid_level_requirement_is_locked(self):
        row = dict(data.row("Dungeon", YAMA1), needLevel="not-a-number", prevId="-1")
        with mock.patch("zmxy.quota.data.row", return_value=row):
            self.assertIn("等级", quota.locked(info(level=120), YAMA1) or "")

    def test_list_form_records(self):
        i = {"p1": {"level": 99}, "dungeon": [{"id": YAMA1, "passNum": 2}]}
        self.assertIsNone(quota.locked(i, YAMA2))


class RunLevelTests(unittest.TestCase):
    def test_locked_level_sends_nothing(self):
        class Ctx:
            info = info(passed=[])
            refreshed = 0
            def request(self, *a, **k):
                raise AssertionError("未解锁不该发包")
            def refresh(self):  # 重新进入角色再判一次（只读），还是没解锁
                self.refreshed += 1
                return self.info
            session = None
        ctx = Ctx()
        lv = special.level_of((3,), False, YAMA2)
        done, why = dungeon.run_level(ctx, lv, 1, dungeon.Gains())
        self.assertEqual(done, 0)
        self.assertIn("未解锁", why)
        self.assertEqual(ctx.refreshed, 1)


class NewAccountTests(unittest.TestCase):
    """新号全选：从第一关一路打到最后一关，后一关靠刚打通的前一关解锁，不能报未解锁。"""

    class Server:
        """记着通关记录的假服务器：结算一次就把那一关记成通关，重新进入角色时给出最新记录。"""

        def __init__(self, passed=()):
            self.passed, self.last, self.enters = set(passed), None, 0

        def info(self):
            return {"p1": {"level": 1}, "vip": {"level": 0},
                    "dungeon": {str(m): {"id": m, "passNum": 1, "dailyCount": 0} for m in self.passed}}

        def handler(self, route, body):
            if route == p.ROUTE_ENTER:
                self.enters += 1
                return {"info": self.info()}
            if route == dungeon.R_CREATE:
                self.last = body["mid"]
            elif route == dungeon.R_FINISH and body["win"]:
                table = data.table("Dungeon")
                self.passed.add(table[str(self.last)].get("finishBId") or self.last)  # Boss 房结算记在所属关卡上

    def created(self, conn, kind):
        ids = {m for m, _ in dungeon.catalog(kind)}
        return [b["mid"] for b in conn.bodies("createSinglePlayerCopy") if b["mid"] in ids]

    def test_main_all_selected_runs_first_to_last(self):
        server, mains = self.Server(), [m for m, _ in dungeon.catalog("main")]
        conn = Fake(server.handler)
        r = run("dungeon_main", conn, server.info(), {"levels": mains[::-1], "times": 1})  # 勾选顺序是倒的
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(self.created(conn, "main"), mains)
        self.assertEqual(server.enters, len(mains) - 1)  # 第一关不用前置，之后每关重新进入角色确认一次

    def test_elite_all_selected_after_main_runs_first_to_last(self):
        mains, elites = ([m for m, _ in dungeon.catalog(k)] for k in ("main", "elite"))
        server = self.Server(passed=mains)
        conn = Fake(server.handler)
        r = run("dungeon_elite", conn, server.info(), {"levels": elites[::-1], "times": 1})
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(self.created(conn, "elite"), elites)

    def test_elite_right_after_main_in_one_session_sees_the_new_passes(self):
        server = self.Server()
        conn = Fake(server.handler)
        session = Session(p.ClientConfig(uid=1, username="u"))
        self.assertEqual(run("dungeon_main", conn, server.info(), {"levels": [1, 2], "times": 1}, session=session).status, "submitted")
        r = run("dungeon_elite", conn, session.info, {"levels": [30020, 30010], "times": 1}, session=session)  # 手里还是打主线时的记录
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(self.created(conn, "elite"), [30010, 30020])

    def test_stops_at_first_level_still_locked(self):
        server = self.Server(passed=[1, 2])  # 主线只通了 2 关：精英第 3 关要主线第 3 关
        conn = Fake(server.handler)
        r = run("dungeon_elite", conn, server.info(), {"levels": [30030, 30020, 30010], "times": 1})
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertIn("昆仑宫：关卡未解锁", r.detail)
        self.assertEqual(self.created(conn, "elite"), [30010, 30020])

    def test_no_clear_does_not_unlock_the_next_level(self):
        server = self.Server()
        conn = Fake(server.handler)
        r = run("dungeon_main", conn, server.info(), {"levels": [1, 2], "times": 1, "no_clear": True})
        self.assertEqual(r.status, "blocked", r.detail)
        self.assertEqual(self.created(conn, "main"), [1])


if __name__ == "__main__":
    unittest.main()
