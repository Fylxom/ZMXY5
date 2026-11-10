"""玄女战记：有能量打最新关卡、领取战记任务和星级奖励。全部离线。"""

import threading
import unittest
from unittest import mock

from zmxy import state
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from zmxy.tasks import chapter_role as cr
from tests.support import Fake, run, ts

NOW = ts(2026, 10, 6)
L1, L2, L3, L4 = 1600101, 1600102, 1600103, 1600104  # 需要等级 1 / 2 / 4 / 7
TASK1 = 8901066  # 击败第 1 关的首领
STARS6 = 8903181  # 总星数达到 6
OTHER = 9110003  # 龙宫寻宝的任务
ALL = [int(r["id"]) for r in state.table_rows("ChapterRoleReward")]  # 星级奖励全部领过
DONE = {int(r["id"]): 3 for r in state.table_rows("ChapterRoleTask")}  # 战记任务全部领过


class World:
    """假服务器：战记能量、关卡记录、任务状态；refresh 时给出当前快照。"""

    def __init__(self, energy=0, passed=(), level=120, quests=None, got=(), owned=(1,), crystals=None):
        self.crystals = crystals  # None = 进角信息里没有背包
        self.energy, self.level, self.quests = energy, level, dict(quests or {})
        self.dungeon = {str(m): {"star": 3, "starInfo": 7, "passNum": 1} for m in passed}
        self.got, self.owned, self.current = {str(i): 1 for i in got}, {str(i): 1 for i in owned}, None
        self.conn = Fake(self.handle)

    def chapter(self):
        return {"energy": self.energy, "role": {"id": 209000001, "level": self.level}, "dungeon": dict(self.dungeon),
                "starReward": dict(self.got), "levelType": dict(self.owned)}

    def info(self):
        bag = {} if self.crystals is None else {"bag": {"item": [{"id": cr.CRYSTAL, "count": self.crystals}] if self.crystals else []}}
        return {**bag, "chapter": self.chapter(), "quest": [{"id": q, "status": s} for q, s in self.quests.items()]}

    def handle(self, route, body):
        if route == cr.R_BEGIN:
            self.current = body["dungeonId"]
        elif route == cr.R_END:
            self.energy -= 20
            self.dungeon[str(self.current)] = {"star": body["star"], "starInfo": body["starInfo"], "passNum": 1}
            return {"chapter": self.chapter(), "give": [{"type": "coin", "subType": "chapterRoleEXP", "count": 10}]}
        elif route == cr.R_USE:
            self.crystals -= body["count"]
            self.energy += 100
            return {"give": [{"type": "coin", "subType": "chapterEnergy", "count": 100}]}
        elif route == cr.R_STAR:
            self.got[str(body["id"])] = 1
            return {"chapter": self.chapter(), "give": [{"id": 101048126, "count": 10}]}
        elif route.endswith("commitQuest"):
            self.quests[body["qId"]] = 3
        return None

    def go(self, now=NOW, stop=None):
        self.waits = []
        with mock.patch.object(Session, "refresh", lambda s: setattr(s, "info", self.info()) or s.info), \
                mock.patch.object(TaskContext, "sleep", lambda ctx, seconds, background=True: self.waits.append(seconds) or True):
            return run(cr.ID, self.conn, {}, now=now, stop=stop)


class ChapterRoleTests(unittest.TestCase):
    def test_registered_on_limited_page(self):
        spec = load_tasks()[cr.ID]
        self.assertEqual((spec.title, spec.group), ("玄女战记", "活动"))

    def test_fights_the_newest_level_until_energy_runs_out(self):
        w = World(energy=65, passed=[L1])
        r = w.go()
        self.assertEqual(w.conn.routes()[:6], ["beginChapterDungeon", "endChapterDungeon"] * 3)
        self.assertEqual(set(w.conn.routes()[6:]), {"getStarReward"})  # 12 星：顺带领星级奖励
        self.assertEqual([b["dungeonId"] for b in w.conn.bodies("beginChapterDungeon")], [L2, L3, L4])  # 每打过一关就往后推
        # 第 1 章的关卡设了通关 / 限时 / 血量三个条件：3 星，starInfo = 1|2|4
        self.assertEqual(w.conn.bodies("endChapterDungeon"), [{"win": 1, "star": 3, "starInfo": 7}] * 3)
        self.assertEqual((r.status, w.energy, w.waits), ("submitted", 5, [cr.PACE] * 3), r.detail)

    def test_new_player_starts_from_the_first_level(self):
        w = World(energy=20, level=1)
        w.go()
        self.assertEqual([b["dungeonId"] for b in w.conn.bodies("beginChapterDungeon")], [L1])

    def test_repeats_the_last_playable_level_when_role_level_is_too_low(self):
        w = World(energy=40, passed=[L1, L2], level=3)  # 第 3 关要 4 级
        w.go()
        self.assertEqual([b["dungeonId"] for b in w.conn.bodies("beginChapterDungeon")], [L2, L2])

    def test_finishing_the_last_level_and_claiming_everything_ends_the_activity(self):
        last = cr.levels()[-1]
        w = World(energy=100, passed=cr.levels()[:-1], got=ALL, crystals=3, quests={**DONE, TASK1: 1})
        real = w.handle

        def handle(route, body):
            out = real(route, body)
            if route == cr.R_END:
                w.quests[TASK1] = 2  # 打完最后一关，最后一个任务也完成了
            return out
        w.conn.handler = handle
        r = w.go()
        self.assertEqual(w.conn.routes(), ["beginChapterDungeon", "endChapterDungeon", "commitQuest"])  # 剩下的能量和水晶都不再用
        self.assertEqual([b["dungeonId"] for b in w.conn.bodies("beginChapterDungeon")], [last])
        self.assertEqual((r.status, r.profile, w.crystals), ("submitted", {cr.END: True}, 3), r.detail)
        self.assertIn("玄女战记已结束", r.detail)

    def test_cleared_but_tasks_not_all_claimed_is_not_ended(self):
        for quests in ({**DONE, TASK1: 1}, {k: v for k, v in DONE.items() if k != TASK1}):  # 有任务没完成 / 服务器没列出来
            w = World(energy=100, passed=cr.levels(), got=ALL, crystals=3, quests=quests)
            r = w.go()
            self.assertEqual((r.status, w.conn.sent, r.profile), ("skipped", [], {cr.END: False}))  # 打完了就不再挑战
            self.assertIn("还有 1 个战记任务没领完", r.detail)
        # 领取被拒绝：还是没领完
        w = World(energy=0, passed=cr.levels(), got=ALL, quests={**DONE, TASK1: 2})
        w.conn.handler = lambda route, body: "条件未达成"
        r = w.go()
        self.assertEqual((r.status, r.profile), ("blocked", {cr.END: False}))
        # 还有能领的星级奖励没领到：也不算结束
        self.assertFalse(cr.chapter_end(World(passed=cr.levels(), quests=DONE).info()))

    def test_ended_save_sends_nothing(self):
        w = World(energy=100, passed=cr.levels(), got=ALL, crystals=3, quests=DONE)
        r = w.go()
        self.assertEqual((r.status, w.conn.sent, r.profile), ("skipped", [], {cr.END: True}))
        self.assertIn("玄女战记已结束", r.detail)

    def test_marks_not_ended_and_leaves_the_mark_alone_when_unreadable(self):
        self.assertEqual(World(energy=20, passed=[L1], got=ALL, quests=DONE).go().profile, {cr.END: False})
        self.assertIsNone(cr.chapter_end({"quest": []}))
        self.assertIsNone(cr.chapter_end({"chapter": {"energy": 5}, "quest": []}))
        self.assertIsNone(cr.chapter_end(World(passed=cr.levels(), got=ALL).info() | {"quest": None}))

    def test_claims_tasks_and_star_rewards_after_fighting(self):
        w = World(energy=20, passed=[L1], quests={TASK1: 2, STARS6: 1, OTHER: 2}, got=[1001], owned=[1])
        real = w.handle

        def handle(route, body):
            out = real(route, body)
            if route == cr.R_END:
                w.quests[STARS6] = 2  # 打完这一关星数够了
            return out
        w.conn.handler = handle
        r = w.go()
        self.assertEqual(w.conn.routes()[:2], ["beginChapterDungeon", "endChapterDungeon"])
        self.assertEqual([b["qId"] for b in w.conn.bodies("commitQuest")], [TASK1, STARS6])  # 别的活动的任务不碰
        # 共 6 星：只领免费档里够星数、没领过的；至尊档没开通不领
        ids = [b["id"] for b in w.conn.bodies("getStarReward")]
        want = sorted(int(r["id"]) for r in state.table_rows("ChapterRoleReward")
                      if r["levelType"] == 1 and r["stars"] <= 6 and int(r["id"]) != 1001)
        self.assertTrue(want)
        self.assertEqual(ids, want)
        self.assertEqual(r.status, "submitted", r.detail)

    def test_claims_without_energy(self):
        w = World(energy=10, quests={TASK1: 2})
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("submitted", ["commitQuest"]), r.detail)

    def test_uses_crystals_when_energy_runs_out(self):
        w = World(energy=30, passed=cr.levels()[:10], got=ALL, crystals=2)
        r = w.go()
        fight = ["beginChapterDungeon", "endChapterDungeon"]
        # 30 → 打 1 次剩 10 → 水晶 110 → 打 5 次剩 10 → 水晶 110 → 打 5 次剩 10 → 没有水晶了
        self.assertEqual(w.conn.routes(), fight + ["useItem"] + fight * 5 + ["useItem"] + fight * 5)
        # 使用道具在 SecondPassword 表里，二级密码字段由 Session 自动带上
        self.assertEqual([(b["index"], b["count"], b["id"]) for b in w.conn.bodies("useItem")], [(0, 1, str(cr.CRYSTAL))] * 2)
        self.assertEqual((r.status, w.energy, w.crystals), ("submitted", 10, 0), r.detail)
        self.assertIn("使用战记水晶 ×2", r.detail)

    def test_keeps_crystals_while_energy_is_enough(self):
        w = World(energy=20, passed=[L1], got=ALL, crystals=0)
        w.go()
        self.assertNotIn("useItem", w.conn.routes())
        w = World(energy=10, passed=[L1], got=ALL)  # 读不到背包：不用
        r = w.go()
        self.assertEqual((r.status, w.conn.sent), ("skipped", []))

    def test_stops_when_crystal_adds_no_energy(self):
        w = World(energy=0, crystals=3)
        w.conn.handler = lambda route, body: None
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["useItem"]))

    def test_refused_crystal_stops(self):
        w = World(energy=0, crystals=3)
        w.conn.handler = lambda route, body: "道具不存在"
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["useItem"]))

    def test_nothing_to_do_is_skipped(self):
        w = World(energy=19)
        r = w.go()
        self.assertEqual((r.status, w.conn.sent), ("skipped", []))
        self.assertIn("战记能量不足", r.detail)

    def test_closed_activity_sends_nothing(self):
        w = World(energy=100)
        r = w.go(now=ts(2026, 11, 1))
        self.assertEqual((r.status, w.conn.sent), ("skipped", []))

    def test_unreadable_energy_or_records_blocks_the_fight(self):
        for broken in ("energy", "dungeon", "role"):
            w = World(energy=100)
            real = w.chapter
            w.chapter = lambda real=real, broken=broken: {k: v for k, v in real().items() if k != broken}
            r = w.go()
            self.assertEqual((r.status, w.conn.sent), ("blocked", []), broken)
        conn = Fake()
        with mock.patch.object(Session, "refresh", lambda s: setattr(s, "info", {"quest": []}) or s.info):
            r = run(cr.ID, conn, {}, now=NOW)
        self.assertEqual((r.status, conn.sent), ("blocked", []))

    def test_refused_begin_stops(self):
        w = World(energy=100)
        w.conn.handler = lambda route, body: "等级不足"
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["beginChapterDungeon"]))

    def test_stops_when_energy_does_not_drop(self):
        w = World(energy=100)
        w.conn.handler = lambda route, body: {"chapter": w.chapter()} if route == cr.R_END else None
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["beginChapterDungeon", "endChapterDungeon"]))

    def test_started_fight_is_settled_before_honouring_stop(self):
        stop = threading.Event()
        w = World(energy=100)
        real = w.handle

        def handle(route, body):
            if route == cr.R_BEGIN:
                stop.set()
            return real(route, body)
        w.conn.handler = handle
        w.go(stop=stop)
        self.assertEqual(w.conn.routes(), ["beginChapterDungeon", "endChapterDungeon"])

    def test_timeout_on_settle_is_uncertain(self):
        w = World(energy=100)

        def handle(route, body):
            if route == cr.R_END:
                raise TimeoutError("超时")
        w.conn.handler = handle
        with self.assertRaises(UncertainOutcome):
            w.go()
        self.assertEqual(w.conn.routes(), ["beginChapterDungeon", "endChapterDungeon"])


if __name__ == "__main__":
    unittest.main()
