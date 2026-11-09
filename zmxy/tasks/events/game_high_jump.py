"""登高采菊（活动页）：用完每日免费次数，不发再次挑战的付费路由。

``ClimbHigh[1]`` 给活动和免费次数；每次 ``beginClimbHigh {}`` 后按客户端 ``BMProxy.gameOver`` 发
``endClimbHigh {score}``。分数在账号首次执行时从 100–120 随机取定，之后一直复用。
"""

from __future__ import annotations

import random

from ... import data, dungeon, protocol as p, state
from ...core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task
from ..limited import attempt, free_attempts

ID = "game_high_jump"
R_START = "scene.activityHandler.beginClimbHigh"
R_END = "scene.activityHandler.endClimbHigh"
CONFIG = 1
SCORE_MIN = 100
SCORE_MAX = 120
SCORE_KEY = "game_high_jump_score"


def _score(ctx: TaskContext) -> tuple[int, bool]:
    """账号固定分数；返回 (分数, 是否本次首次生成)。"""
    score = state.num(ctx.account_profile.get(SCORE_KEY))
    if score is not None and SCORE_MIN <= score <= SCORE_MAX:
        return score, False
    score = random.randint(SCORE_MIN, SCORE_MAX)
    ctx.account_profile[SCORE_KEY] = score
    return score, True


def _config():
    try:
        return state.row("ClimbHigh", CONFIG) or {}
    except data.DataError:  # 旧表没有这个活动，活动页按未开放处理
        return {}


def _open(now: int) -> bool:
    activity_id = state.num(_config().get("activityId"))
    try:
        return activity_id is not None and state.activity_open(activity_id, now)
    except data.DataError:
        return False


@task(ID, "登高采菊", writes={R_START, R_END}, group="活动", window=_open)
def run(ctx: TaskContext) -> TaskResult:
    if ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少服务器时间，无法确认活动是否开放，未发送请求")
    config = _config()
    activity_id = state.num(config.get("activityId"))
    if activity_id is None:
        return TaskResult(ID, BLOCKED, "ClimbHigh 配置没有给出活动 id，未发送请求")
    try:
        if not state.activity_open(activity_id, ctx.now):
            return TaskResult(ID, SKIPPED, "活动未开放")
    except data.DataError:
        return TaskResult(ID, BLOCKED, "Activity 数据表不可读，无法确认活动是否开放，未发送请求")
    limit, refused = free_attempts(ctx, ID, "登高采菊", state.num(config.get("freeNum")), "climbFreeNum")
    if refused:
        return refused

    score, new_score = _score(ctx)
    ctx.log(f"登高采菊 0/{limit}")
    gains, done, why = dungeon.Gains(), 0, None
    for _ in range(limit):
        if ctx.stopped:
            why = "用户停止"
            break
        try:
            # 开始请求一发出，这次免费次数就可能被占用：不退回，也不重发。
            gains.add(attempt(ctx, ID, R_START, {}), ctx.session.drain_pushes())
            gains.add(ctx.request(R_END, {"score": score}), ctx.session.drain_pushes())
        except p.AllianceError as exc:
            why = f"第 {done + 1} 次被服务器拒绝：{exc}"
            break
        done += 1
        ctx.log(f"登高采菊 {done}/{limit}")

    profile = {SCORE_KEY: score} if new_score else None
    detail = f"已结算 登高采菊×{done}"
    if why == "用户停止" and not done:
        return TaskResult(ID, SKIPPED, "用户停止，未开始挑战")
    if why:
        return TaskResult(ID, BLOCKED, f"{detail}；{why}{dungeon.gain_note(gains)}", account_profile=profile)
    return TaskResult(ID, SUBMITTED, detail + dungeon.gain_note(gains), account_profile=profile)
