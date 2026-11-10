"""限时任务：自动识别开放的版本活动任务，接取并领取。离线。"""

import unittest
from unittest import mock

from zmxy import login, protocol as p
from zmxy.core import Session, TaskContext, load_tasks
from tests.support import Fake, ts

ACCEPT, COMMIT = "scene.questHandler.acceptQuest", "scene.questHandler.commitQuest"
YUAN = 2014  # 玄塔有灵（活动 30，到 2026-10-7）
ZHEN = 2015  # 金秋镇煞
EXPIRED = 2013  # 中秋梦魇（活动 21，已结束）


def make(conn, info, now=ts(2026, 10, 1)):
    s = Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s.info = conn, info
    s._link = login.Link(conn, s.config, [{"index": 0}], {"time": now})
    spec = load_tasks()["limited_quest"]
    s.bind(spec)
    return s, spec, TaskContext(s, {})


def quests(**status):
    return {"quest": [{"id": int(k[1:]), "status": v} for k, v in status.items()]}


def player(level=30, passed=(3, 30090)):  # 够得上玄塔有灵 / 金秋镇煞的等级和前置关卡（TaskManager.extractTask）
    return {"p1": {"level": level}, "dungeon": {str(mid): {"id": mid, "passNum": 1} for mid in passed}}


class LimitedQuestTests(unittest.TestCase):
    def test_accepts_open_activity_quests_only(self):
        conn = Fake()
        s, spec, ctx = make(conn, quests(q1=3) | player())
        self.assertIsNone(spec.prelude(ctx))
        self.assertEqual(sorted(b["qId"] for b in conn.bodies("acceptQuest")), [YUAN, ZHEN])  # 已过期的 2013 不接
        self.assertIn("金秋镇煞", s.carry["limited_quest"])

    def test_level_and_required_dungeon_gate_accepting(self):
        for you, want in ((player(level=20), [YUAN]),            # 金秋镇煞要 24 级
                          (player(passed=(3,)), [YUAN]),         # 金秋镇煞要先通关 30090
                          (player(passed=()), []),
                          ({"dungeon": {}}, [])):                # 读不到等级不接
            with self.subTest(you=you):
                conn = Fake()
                _, spec, ctx = make(conn, quests(q1=3) | you)
                spec.prelude(ctx)
                self.assertEqual([b["qId"] for b in conn.bodies("acceptQuest")], want)

    def test_does_not_reaccept_started_quests(self):
        conn = Fake()
        _, spec, ctx = make(conn, quests(**{f"q{YUAN}": 1, f"q{ZHEN}": 2}))
        spec.prelude(ctx)
        self.assertEqual(conn.sent, [])

    def test_claims_finished_and_reports_unfinished(self):
        conn = Fake()
        s, spec, ctx = make(conn, {})
        with mock.patch.object(Session, "refresh", lambda self: setattr(self, "info", quests(**{f"q{YUAN}": 2, f"q{ZHEN}": 1})) or self.info):
            r = spec.run(ctx)
        self.assertEqual([b["qId"] for b in conn.bodies("commitQuest")], [YUAN])
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("玄塔有灵", r.detail)
        self.assertIn("未完成：金秋镇煞", r.detail)

    def test_no_open_activity_is_skipped(self):
        conn = Fake()
        s, spec, ctx = make(conn, quests(q1=3), now=ts(2026, 11, 1))
        self.assertIsNone(spec.prelude(ctx))
        with mock.patch.object(Session, "refresh", lambda self: self.info):
            r = spec.run(ctx)
        self.assertEqual((r.status, conn.sent), ("skipped", []))

    def test_claims_dragon_palace_tasks_without_accepting(self):
        from zmxy import data
        dragon = int(next(iter(data.table("DragonTreasureTask"))))
        conn = Fake()
        s, spec, ctx = make(conn, {}, now=ts(2026, 11, 1))  # 限时活动已结束，龙宫任务仍可领
        with mock.patch.object(Session, "refresh", lambda self: setattr(self, "info", quests(**{f"q{dragon}": 2})) or self.info):
            spec.prelude(ctx)
            r = spec.run(ctx)
        self.assertEqual(conn.bodies("acceptQuest"), [])
        self.assertEqual([b["qId"] for b in conn.bodies("commitQuest")], [dragon])
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("龙宫任务", r.detail)

    def test_new_activity_is_picked_up_without_code_changes(self):
        from zmxy import data
        rows = dict(data.table("TaskType"))
        acts = dict(data.table("Activity"))
        rows["9001"] = {**rows[str(ZHEN)], "id": 9001, "name": "新活动任务", "activityId": 999, "label": 4}
        acts["999"] = {**acts["30"], "id": 999, "showShelfTime": "2026/9/30-00:00:00", "showUnderTime": "2026/12/1-00:00:00"}
        real = data.table
        with mock.patch("zmxy.data.table", lambda n: rows if n == "TaskType" else acts if n == "Activity" else real(n)):
            conn = Fake()
            _, spec, ctx = make(conn, quests(q1=3) | player())
            spec.prelude(ctx)
        self.assertIn(9001, [b["qId"] for b in conn.bodies("acceptQuest")])


if __name__ == "__main__":
    unittest.main()
