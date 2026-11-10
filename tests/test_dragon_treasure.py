"""龙宫寻宝：掷骰子、龟丞相答题、领取龙宫任务。全部离线。"""

import threading
import unittest
from unittest import mock

from zmxy import data
from zmxy.core import Session, TaskContext, UncertainOutcome
from zmxy.tasks import dragon_treasure as dt
from tests.support import Fake, run, ts

NOW = ts(2026, 10, 5)
DICE = 101048051
DAILY, WALK15, WALK35 = 9110003, 9110303, 9110304  # 本期（活动 2044）：每日登录、走 15 格、走 35 格
OLD = 9110285  # 往期（活动 1869）的走 15 格


class World:
    """假服务器：骰子、任务状态、每一掷的结果；refresh 时给出当前快照。"""

    def __init__(self, dice=0, quests=None, rolls=(), on_commit=None):
        self.dice, self.quests, self.rolls, self.on_commit = dice, dict(quests or {}), list(rolls), on_commit
        self.conn = Fake(self.handle)

    def info(self):
        return {"bag": {"item": [{"id": DICE, "count": self.dice}] if self.dice else []},
                "quest": [{"id": q, "status": s} for q, s in self.quests.items()]}

    def handle(self, route, body):
        if route == dt.R_GO:
            self.dice -= 1
            return {"step": self.rolls.pop(0) if self.rolls else [{"point": 3, "endPos": 4, "map": 1, "event": 0}]}
        if route == dt.R_GUESS:
            return {"answer": body["answer"], "give": [{"id": 101022041, "count": 5}]}
        if route.endswith("commitQuest"):
            self.quests[body["qId"]] = 3
            if self.on_commit:
                return self.on_commit(self, body["qId"])
        return None

    def go(self, now=NOW, stop=None, tasks=()):
        self.waits = []
        with mock.patch.object(Session, "refresh", lambda s: setattr(s, "info", self.info()) or s.info),                 mock.patch.object(TaskContext, "sleep", lambda ctx, seconds, background=True: self.waits.append(seconds) or True):
            return run(dt.ID, self.conn, {}, now=now, stop=stop, tasks=tasks)


class DragonTreasureTests(unittest.TestCase):
    def test_first_option_is_the_right_answer_in_every_question(self):
        # 客户端答错时显示“正确答案：answer[0]”；每题都得有 4 个不重复的选项，否则固定答 0 的前提不成立
        for r in data.table("DragonTreasureGuess").values():
            options = r["answer"].split(";")
            self.assertEqual((len(options), len(set(options))), (4, 4), r)

    def test_uses_all_dice_and_answers_turtle_quiz_with_option_zero(self):
        w = World(dice=3, rolls=[[{"point": 2, "event": 0}], [{"point": 5, "event": GUESS_STEP}], [{"point": 1, "event": 1}, {"point": 4, "event": 99}]])
        r = w.go()
        self.assertEqual(w.conn.routes(), ["goNext", "goNext", "guessChoose", "goNext"])
        # 只用骰子（type 0），不用点券指定点数（type 1 + point）；二级密码字段由 Session 按 SecondPassword 表自动带上
        self.assertEqual([(b["type"], "point" in b) for b in w.conn.bodies("goNext")], [(0, False)] * 3)
        self.assertEqual([b["answer"] for b in w.conn.bodies("guessChoose")], [0])
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("寻宝 ×3（走 12 格）", r.detail)
        self.assertIn("龟丞相答题 ×1", r.detail)

    def test_claims_daily_tasks_first_then_period_tasks_after_hunting(self):
        def on_commit(w, qid):
            if qid == DAILY:
                w.dice += 2  # 每日任务给骰子
            if qid == WALK15:
                w.quests[WALK35] = 2  # 领完一档，下一档也够了

        w = World(quests={DAILY: 2, WALK15: 1, OLD: 2}, on_commit=on_commit)
        real = w.handle

        def handle(route, body):
            out = real(route, body)
            if route == dt.R_GO and w.dice == 0:
                w.quests[WALK15] = 2
            return out
        w.conn.handler = handle
        r = w.go()
        self.assertEqual(w.conn.routes(), ["commitQuest", "goNext", "goNext", "commitQuest", "commitQuest"])
        self.assertEqual([b["qId"] for b in w.conn.bodies("commitQuest")], [DAILY, WALK15, WALK35])  # 往期任务不碰
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertIn("寻宝走15个格子", r.detail)

    def test_leaves_the_claim_before_hunting_to_limited_quest_when_it_is_selected(self):
        # 限时任务排在前面、已经领过；这里万一还有可领的也不在寻宝前领，寻宝后才领
        w = World(quests={DAILY: 2})
        r = w.go(tasks=("limited_quest", dt.ID))
        self.assertEqual((r.status, w.conn.sent), ("skipped", []))
        w = World(dice=1, quests={WALK15: 2})
        r = w.go(tasks=("limited_quest", dt.ID))
        self.assertEqual(w.conn.routes(), ["goNext", "commitQuest"])
        self.assertEqual(r.status, "submitted", r.detail)

    def test_refused_claim_does_not_cut_the_hunt_short(self):
        # 领取被拒绝只记下来：骰子照样用完，不能掷一次就停
        w = World(dice=3, quests={DAILY: 2})
        real = w.handle
        w.conn.handler = lambda route, body: "条件未达成" if route.endswith("commitQuest") else real(route, body)
        r = w.go()
        self.assertEqual(w.conn.routes().count("goNext"), 3)
        self.assertEqual(r.status, "blocked")
        self.assertIn("寻宝 ×3", r.detail)
        self.assertIn("领取被拒绝", r.detail)

    def test_runs_after_limited_quest(self):
        from zmxy.core import load_tasks
        order = list(load_tasks())
        self.assertLess(order.index("limited_quest"), order.index(dt.ID))

    def test_no_dice_and_nothing_to_claim_is_skipped(self):
        w = World(quests={WALK15: 1})
        r = w.go()
        self.assertEqual((r.status, w.conn.sent), ("skipped", []))

    def test_closed_activity_sends_nothing(self):
        w = World(dice=5)
        r = w.go(now=ts(2026, 11, 1))
        self.assertEqual((r.status, w.conn.sent), ("skipped", []))

    def test_unreadable_bag_or_quests_blocks(self):
        for info in ({"quest": []}, {"bag": {"item": []}}):
            conn = Fake()
            with mock.patch.object(Session, "refresh", lambda s, i=info: setattr(s, "info", i) or i):
                r = run(dt.ID, conn, {}, now=NOW)
            self.assertEqual((r.status, conn.sent), ("blocked", []))

    def test_response_without_steps_stops_without_retry(self):
        w = World(dice=3)
        w.conn.handler = lambda route, body: {}
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["goNext"]))

    def test_logs_every_step(self):
        w = World(dice=2, quests={DAILY: 2}, rolls=[[{"point": 2, "start": 1, "endPos": 3, "map": 1, "event": 0}],
                                                    [{"point": 5, "start": 3, "endPos": 8, "map": 1, "event": GUESS_STEP}]])
        lines = []
        real_init = TaskContext.__init__

        def init(ctx, *args, **kw):
            real_init(ctx, *args, **{**kw, "log": lines.append})

        with mock.patch.object(TaskContext, "__init__", init):
            w.go()
        text = chr(10).join(lines)
        for part in ("读取角色状态", "领取龙宫任务", "第 1 掷", "走 2 格：第 1 → 3 格", "普通格", "第 2 掷", "龟丞相答题", "答题结果：答对", "没有骰子了", "这一掷没有奖励"):
            self.assertIn(part, text)

    def test_paces_requests_and_retries_when_told_too_frequent(self):
        w = World(dice=2, rolls=[[{"point": 6, "event": 0}], [{"point": 1, "event": GUESS_STEP}]])
        real, busy = w.handle, iter([False, True, True, False, True, False])

        def handle(route, body):
            return "请勿频繁请求" if next(busy) else real(route, body)
        w.conn.handler = handle
        r = w.go()
        self.assertEqual(w.conn.routes(), ["goNext", "goNext", "goNext", "goNext", "guessChoose", "guessChoose"])
        self.assertEqual(w.waits, [dt.PACE, dt.PACE, dt.BUSY_WAIT, dt.BUSY_WAIT, dt.PACE, dt.BUSY_WAIT])
        self.assertEqual((r.status, w.dice), ("submitted", 0), r.detail)
        self.assertIn("寻宝 ×2", r.detail)

    def test_gives_up_when_still_too_frequent(self):
        w = World(dice=2)
        w.conn.handler = lambda route, body: "请勿频繁请求"
        r = w.go()
        self.assertEqual((r.status, len(w.conn.sent)), ("blocked", dt.RETRIES + 1))

    def test_server_refusal_stops_rolling(self):
        w = World(dice=3)
        w.conn.handler = lambda route, body: "道具不足"
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["goNext"]))

    def test_stops_when_server_judges_the_answer_wrong(self):
        w = World(dice=3, rolls=[[{"point": 5, "event": GUESS_STEP}]])
        real = w.handle
        w.conn.handler = lambda route, body: {"answer": 2} if route == dt.R_GUESS else real(route, body)
        r = w.go()
        self.assertEqual((r.status, w.conn.routes()), ("blocked", ["goNext", "guessChoose"]))
        self.assertIn("答错", r.detail)

    def test_quiz_is_answered_before_honouring_stop(self):
        stop = threading.Event()
        w = World(dice=3, rolls=[[{"point": 5, "event": GUESS_STEP}]])
        real = w.handle

        def handle(route, body):
            if route == dt.R_GO:
                stop.set()
            return real(route, body)
        w.conn.handler = handle
        w.go(stop=stop)
        self.assertEqual(w.conn.routes(), ["goNext", "guessChoose"])

    def test_timeout_on_roll_is_uncertain(self):
        w = World(dice=2)

        def handle(route, body):
            raise TimeoutError("超时")
        w.conn.handler = handle
        with self.assertRaises(UncertainOutcome):
            w.go()
        self.assertEqual(w.conn.routes(), ["goNext"])


GUESS_STEP = dt.GUESS

if __name__ == "__main__":
    unittest.main()
