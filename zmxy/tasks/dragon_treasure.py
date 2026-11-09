"""龙宫寻宝：用完寻宝骰子，遇到龟丞相答题，最后领取龙宫任务（每日 + 每期“走的格子数”）。

对照客户端 ``modules/game45``（Game45Proxy / GameControlView / GameAnswerView / ShowWinLoseCmd）：

* 配置：``DragonTreasureHelper`` 第 1 行给活动 id（Activity 档期）和骰子的物品 id（``treasureItemId``）。
* 掷骰子：``goNext {type:0}`` 用 1 个骰子（``type:1`` 是花点券指定点数，不碰）。响应 ``step`` 是这一掷走的每一段
  ``{point, endPos, map, event, give}``：event 1 好运气（再走一段，已在同一个响应里）、99 换地图、**2 龟丞相答题**。
* 答题：``guessChoose {answer: 选项序号}``。题目是客户端自己随机挑的，请求里不带题号，服务器只看序号；
  ``DragonTreasureGuess`` 每题的第 1 个选项就是正确答案，所以固定答 0：界面上四个选项的位置是打乱的，但 ItemAnswer 只是
  交换四个按钮的 y 坐标，第 i 个按钮的文字永远是 ``answer[i]``、点它发出去的就是 i；答错时客户端显示“正确答案：answer[0]”。
  响应的 ``answer`` 为 0 才算答对（ShowWinLoseCmd）：答完发现不是 0 就停止寻宝，不继续答错。
  掷到答题格后必须先答完才检查“停止”，不把题留在那里。
* 任务：``DragonTreasureTask`` 里本期活动的任务不用接取，info.quest 里 status==2 的 ``commitQuest``（同 GameTaskMediator）。
  骰子来自每日任务，所以寻宝前先领一遍——本次也勾了“限时任务”时这一步归它（它排在本任务前面，已经领过），这里不重复做；
  寻宝后再领（每期任务一档接一档，领到没有为止）。

* 节奏：服务器限制寻宝请求的频率（真机 2026-10-05：连着掷第 2 次被拒“请勿频繁请求”；客户端每一掷之间有骰子和走格子的动画）。
  每个寻宝 / 答题请求前等 ``PACE`` 秒；仍被拒“频繁”（= 没执行）就多等一会儿再发，最多 ``RETRIES`` 次。

骰子数和任务状态每一轮都重新进角色读，不自己推算。
"""

from __future__ import annotations

from itertools import count

from .. import dungeon, protocol as p, quest, state
from . import limited_quest
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from .rewards import record, with_idle_error

ID = "dragon_treasure"
R_GO = "scene.dragonTreasureHandler.goNext"
R_GUESS = "scene.dragonTreasureHandler.guessChoose"
GUESS = 2  # step.event：龟丞相答题
RIGHT = 0  # 正确答案的选项序号
PACE, BUSY_WAIT, RETRIES = 1.0, 5.0, 3  # 寻宝请求之间的间隔（秒）、被拒“频繁”后的等待、最多再试几次
BUSY = "频繁"
EVENTS = {0: "普通格", 1: "好运气（再走一段）", 2: "龟丞相答题", 3: "特殊格", 99: "换地图"}  # step.event；3 的含义客户端没处理，按“特殊格”记
MAX_ROUNDS = 12  # 领任务 / 寻宝的轮数上限（每期任务 6 档 + 中途又得到骰子），只防死循环


def helper() -> dict:
    return state.row("DragonTreasureHelper", 1) or {}


def is_open(now: int | None) -> bool:
    return state.activity_open(helper().get("activityId"), now)


def ready_tasks(info) -> dict[int, str] | None:
    """本期活动里可领取的龙宫任务 {任务 id: 说明}；服务器没给任务列表 → None。"""
    status = quest.status(info)
    if status is None:
        return None
    aid = state.num(helper().get("activityId"))
    return {int(r["id"]): str(r.get("describe") or r.get("name") or r["id"]) for r in state.table_rows("DragonTreasureTask")
            if state.num(r.get("activityId")) == aid and status.get(state.num(r.get("id"))) == quest.READY}


def send(ctx: TaskContext, route: str, body: dict, *, finish_current: bool = False) -> dict:
    """按节奏发一个寻宝请求；明确拒绝“请勿频繁请求”时等一会儿再发。
    finish_current 只用于已经触发的龟丞相题：停止后仍要答完，不能把角色留在答题格。"""
    for attempt in count():
        wait = BUSY_WAIT if attempt else PACE
        if attempt:
            ctx.log(f"  等 {wait:g} 秒后重发（第 {attempt}/{RETRIES} 次重试）")
        if not ctx.sleep(wait) and not (finish_current and (ctx.stopped or ctx.winding)):
            if ctx.stopped or ctx.winding:
                raise dungeon.Stop("已被用户停止，未发送请求")
            raise dungeon.Stop(with_idle_error(ctx, "等待请求间隔时连接中断，未发送"))
        if not finish_current and (ctx.stopped or ctx.winding):
            raise dungeon.Stop("已被用户停止，未发送请求")
        try:
            return ctx.request(route, body)
        except p.AllianceError as exc:
            if BUSY not in str(exc) or attempt == RETRIES:
                raise
            ctx.log(f"  服务器拒绝：{exc}（没有执行，可以重发）")


class _Run:
    """龙宫寻宝的一次执行：领任务 → 掷完这一轮的骰子，一轮轮来，每轮重新读骰子数和任务状态。"""

    def __init__(self, ctx: TaskContext, dice_id: int) -> None:
        self.ctx, self.dice_id = ctx, dice_id
        self.gains, self.claimed, self.failed = dungeon.Gains(), [], set()
        self.refused: list[str] = []  # 领取被拒绝的任务：只记下来，不影响接着寻宝
        self.rolls = self.cells = self.quizzes = 0
        self.stop = ""

    @property
    def halted(self) -> bool:
        return self.ctx.stopped or self.ctx.winding

    def summary(self) -> str:
        parts = [x for x in (
            f"寻宝 ×{self.rolls}（走 {self.cells} 格）" if self.rolls else "",
            f"龟丞相答题 ×{self.quizzes}" if self.quizzes else "",
            f"已领取任务：{'、'.join(self.claimed)}" if self.claimed else "",
        ) if x]
        return "；".join(parts) + dungeon.gain_note(self.gains)

    def claim_tasks(self, ready: dict[int, str]) -> bool:
        """领可领的龙宫任务；返回这一轮领到了没有。"""
        ctx, got = self.ctx, False
        for qid, name in ready.items():
            if self.halted:
                self.stop = "已被用户停止，未继续领取龙宫任务"
                break
            if qid in self.failed:
                continue
            if not self.rolls and limited_quest.ID in ctx.tasks:  # 寻宝前的领取：限时任务做过了
                ctx.log(f"跳过领取「{name}」：本次已勾选限时任务，由它领取")
                continue
            ctx.log(f"领取龙宫任务「{name}」")
            try:
                note = record(ctx, self.gains, quest.commit(ctx.request, qid))
            except p.AllianceError as exc:
                self.failed.add(qid)
                self.refused.append(f"{name}({exc})")
                ctx.log(f"  领取被拒绝：{self.refused[-1]}")
                continue
            ctx.log(f"  已领取{note}")
            self.claimed.append(name)
            got = True
        return got

    def log_steps(self, steps: list) -> None:
        for s in steps:
            if not isinstance(s, dict):
                continue
            point, event = state.num(s.get("point")), state.num(s.get("event"))
            self.cells += max(0, point or 0)
            self.ctx.log(f"  走 {point} 格：第 {s.get('start', '?')} → {s.get('endPos', '?')} 格，地图 {s.get('map', '?')}，{EVENTS.get(event, f'事件 {event}')}")

    def answer_quizzes(self, steps: list) -> None:
        ctx = self.ctx
        for s in steps:
            if not isinstance(s, dict) or state.num(s.get("event")) != GUESS:
                continue
            ctx.log("  遇到龟丞相，答第 1 个选项（题库第 1 项是正确答案）")
            try:
                result = send(ctx, R_GUESS, {"answer": RIGHT}, finish_current=True)
            except p.AllianceError as exc:
                self.stop = f"龟丞相答题被拒绝（{exc}），这道题没有答上，请登录游戏核对"
                return
            note = record(ctx, self.gains, result)
            self.quizzes += 1
            verdict = state.num(result.get("answer"))
            ctx.log(f"  答题结果：{'答对' if verdict == RIGHT else '服务器返回 answer=' + str(result.get('answer'))}{note}")
            if verdict is None:
                self.stop = "龟丞相答题响应缺少有效 answer，结果无法确认，已停止寻宝"
            elif verdict != RIGHT:  # 客户端：只有明确 answer==0 才是答对
                self.stop = f"龟丞相判定答错（服务器返回 answer={result.get('answer')}），和客户端脚本推出的规则不一致，已停止寻宝"

    def roll_all(self, dice: int) -> None:
        ctx = self.ctx
        for n in range(1, dice + 1):
            if self.halted:
                ctx.log("已被用户停止")
                return
            ctx.log(f"第 {self.rolls + 1} 掷（本轮 {n}/{dice}）")
            try:
                response = send(ctx, R_GO, {"type": 0})
            except p.AllianceError as exc:
                self.stop = f"寻宝被拒绝（{exc}）"
                return
            steps = response.get("step")
            note = record(ctx, self.gains, response)
            if not isinstance(steps, list):
                self.stop = "寻宝请求已响应，但没有返回 step 行动列表；结果不确定，未重试"
                return
            self.rolls += 1
            self.log_steps(steps)
            ctx.log(f"  这一掷{note or '没有奖励'}")
            self.answer_quizzes(steps)
            if self.stop:
                return

    def rounds(self) -> None:
        ctx = self.ctx
        for _ in range(MAX_ROUNDS):
            if self.halted:
                self.stop = "已被用户停止"
                return
            ctx.log("读取角色状态（骰子数、龙宫任务）")
            info = ctx.refresh()  # 前面的任务会完成龙宫每日任务，寻宝会推进每期任务
            ready = ready_tasks(info)
            dice = state.bag_count(info, self.dice_id)
            if ready is None or dice is None:
                ctx.log("读不到骰子或龙宫任务")
                self.stop = "服务器未返回任务列表或背包，无法确认龙宫任务和骰子数量"
                return
            ctx.log(f"寻宝骰子 ×{dice}；可领取的龙宫任务：{'、'.join(ready.values()) or '无'}")
            if self.claim_tasks(ready):
                continue  # 领到的可能有骰子、可能解锁下一档：重新读
            if dice <= 0:
                ctx.log("没有骰子了，寻宝结束")
                return
            if self.halted:
                return
            self.roll_all(dice)
            if self.stop or self.halted:
                return

    def result(self) -> TaskResult:
        halted, did = self.halted, self.rolls or self.claimed
        text = "；".join(x for x in (self.summary(), self.stop, "已被用户停止" if halted else "",
                                      f"领取被拒绝：{'、'.join(self.refused)}" if self.refused else "") if x)
        if self.refused:
            return TaskResult(ID, BLOCKED, text)
        if self.stop:
            if halted or self.stop.startswith("已被用户停止"):
                return TaskResult(ID, SUBMITTED if did else SKIPPED, text)
            return TaskResult(ID, BLOCKED, text)
        if did:
            return TaskResult(ID, SUBMITTED, text)
        if halted:
            return TaskResult(ID, SKIPPED, text or "已被用户停止")
        return TaskResult(ID, SKIPPED, "没有寻宝骰子，也没有可领取的龙宫任务")


@task(ID, "龙宫寻宝", writes={R_GO, R_GUESS, quest.R_COMMIT}, group="活动", last=True, window=is_open)
def run(ctx: TaskContext) -> TaskResult:
    dice_id = state.num(helper().get("treasureItemId"))
    if dice_id is None or ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少活动配置或服务器时间，未发送请求")
    if not is_open(ctx.now):
        return TaskResult(ID, SKIPPED, "活动未开放")
    job = _Run(ctx, dice_id)
    try:
        job.rounds()
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"{job.summary() or '尚未寻宝'}；{exc}") from exc
    except dungeon.Stop as exc:
        job.stop = job.stop or str(exc)
    except p.AllianceError as exc:
        job.stop = job.stop or f"刷新角色状态失败：{exc}"
    return job.result()
