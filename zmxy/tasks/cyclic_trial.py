"""轮回试炼（限时）：打到所选地阶目标，自动领目标内的奖励。

不是普通副本：客户端每层走 openChallenge → endChallenge，下一层从传送门直接再 openChallenge，
不退出副本。和通天河一样提交 bossHp=0，以服务器 rebirthTower.cur 恰好 +1 作为胜利确认。
cur 是从 0 数的下一关序号，所以目标值就是总层数：人 1–10、地 11–20、天 21–30。
开放时间按**挑战截止时间**判断：Activity 表的 ``underTime``（客户端界面 Tips 1176“挑战截止时间”），
不是一般活动用的 ``showUnderTime``（同一句里的“活动下架时间”，通常晚一周）。
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from .. import data, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "cyclic_trial"
R_OPEN = "scene.rebirthTowerHandler.openChallenge"
R_END = "scene.rebirthTowerHandler.endChallenge"
R_REWARD = "scene.rebirthTowerHandler.getReward"
TARGETS = ((13, "地阶三重"), (15, "地阶五重"), (20, "地阶十重"))  # 不用表里写错成"天阶"的描述
MODES = tuple((str(n), name[0] + name[2]) for n, name in TARGETS)  # 行上点按切换（同竞技场），显示“地三/地五/地十”


def _config() -> Mapping[str, Any] | None:
    try:
        return state.row("CyclicTrial", 1)
    except data.DataError:
        return None


def _dungeon_ids() -> list[int] | None:
    """30 层关卡 id；表里任何一层对不上就不把编号发给服务器。"""
    ids = [state.num(x) for x in str((_config() or {}).get("dungeonId") or "").split(";")]
    if len(ids) != 30 or not all(x and x > 0 for x in ids):
        return None
    try:
        rows = [data.row("Dungeon", mid) or {} for mid in ids]
    except data.DataError:
        return None
    for floor, (mid, row) in enumerate(zip(ids, rows), 1):
        tier = "人阶" if floor <= 10 else "地阶" if floor <= 20 else "天阶"
        if (state.num(row.get("id")) != mid or state.num(row.get("type")) != 16
                or state.num(row.get("difficult")) != 3 or tier not in str(row.get("name") or "")):
            return None
    return ids


def _times() -> tuple | None:
    """(开始, 挑战截止, 活动下架)，北京时间；读不到 → None。"""
    try:
        row = state.row("Activity", (_config() or {}).get("activityId"))
    except data.DataError:
        return None
    if not row:
        return None
    start = state._activity_time(row.get("showShelfTime") or row.get("shelfTime"))
    deadline = state._activity_time(row.get("underTime"))
    shown = state._activity_time(row.get("showUnderTime")) or deadline
    return (start, deadline, shown) if start and deadline else None


def _open(now: int) -> bool:
    """挑战截止前才算开放（截止到下架之间界面还在，但不能再挑战）。"""
    times = _times()
    return bool(times and now is not None and times[0] <= state._cn_time(now) <= times[1])


def _target(options: Mapping[str, Any]) -> int:
    allowed = dict(TARGETS)
    n = state.num(options.get("mode"))
    return n if n in allowed else TARGETS[0][0]


def _tower(value: Any) -> Mapping[str, Any] | None:
    tower = state.section(value, "rebirthTower")
    cur = state.num(tower.get("cur")) if tower else None
    return tower if cur is not None and 0 <= cur <= 30 and isinstance(tower.get("reward"), Mapping) else None


def _status(tower: Mapping[str, Any], floor_id: int) -> int | None:
    reward = tower["reward"]
    return state.num(reward.get(str(floor_id), reward.get(floor_id)))


def _claim_ready(ctx: TaskContext, tower: Mapping[str, Any], ids: list[int],
                 gains: dungeon.Gains) -> tuple[Mapping[str, Any], int, str | None]:
    """领 ids 里所有可领的奖励，返回 (最新 tower, 领了几个, 停止原因)。"""
    claimed = 0
    for floor_id in ids:
        if ctx.stopped:
            break
        if _status(tower, floor_id) != 1:
            continue
        response = ctx.request(R_REWARD, {"id": floor_id})
        gains.add(response, ctx.session.drain_pushes())
        updated = _tower(response)
        if updated is None:
            try:
                updated = _tower(ctx.refresh())
            except p.AllianceError:
                pass
        if updated is None or _status(updated, floor_id) == 1:
            return tower, claimed, f"领取 {floor_id} 奖励后服务器未确认已领取，停止且不重发"
        tower = updated
        claimed += 1
    return tower, claimed, None


def _challenge(ctx: TaskContext, dungeon_id: int, continuation: bool, gains: dungeon.Gains) -> None:
    """打一层。结算结果可能不在响应里，那时由调用方看刷新后的 cur。"""
    ctx.engage(True)
    try:
        opened = ctx.request(R_OPEN, {"rnd": random.randint(1, 2**31 - 1)})
        opened_id = state.num(state.find(opened, "dungeonId", "dungeonID", "dungeon_id", depth=7))
        if opened_id != dungeon_id:
            raise p.AllianceError(f"服务器打开的是第 {opened_id or '未知'} 层，不是预期的第 {dungeon_id} 层")
        ended = ctx.request(R_END, {"bossHp": 0})
        pushes = ctx.session.drain_pushes()
        gains.add(ended, pushes)
        results = (state.num(state.find(value, "result", depth=7)) for value in (ended, *pushes))
        result = next((n for n in results if n is not None), None)
        if result not in (None, 1):
            raise p.AllianceError(f"第 {dungeon_id} 层服务器结算结果为 {result}")
    finally:
        ctx.engage(False)
    ctx.log(f"轮回试炼 {'连续' if continuation else '首'}层 {dungeon_id} 已提交")


@task(ID, "轮回试炼", reads={R_OPEN, R_END, R_REWARD}, writes={R_OPEN, R_END, R_REWARD}, group="活动",
      modes=MODES, window=_open)
def run(ctx: TaskContext) -> TaskResult:
    if ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少服务器时间，无法确认轮回试炼是否开放，未开始挑战")
    if not _open(ctx.now):
        times = _times()
        if times and state._cn_time(ctx.now) > times[1]:
            return TaskResult(ID, SKIPPED, f"轮回试炼挑战已截止（{times[1]:%m/%d %H:%M}，活动 {times[2]:%m/%d} 下架）")
        return TaskResult(ID, SKIPPED, "轮回试炼活动未开放")
    ids, target = _dungeon_ids(), _target(ctx.options)
    if ids is None:
        return TaskResult(ID, BLOCKED, "CyclicTrial 表的 30 层关卡顺序无法确认，未开始挑战")
    tower = _tower(ctx.info)
    if tower is None:
        return TaskResult(ID, BLOCKED, "服务器未返回有效的 rebirthTower.cur/reward，未开始挑战")

    gains, fought, claimed, why = dungeon.Gains(), 0, 0, None
    while True:
        tower, got, why = _claim_ready(ctx, tower, ids[:target], gains)
        claimed += got
        cur = state.num(tower.get("cur"))
        if why or ctx.stopped or cur >= target:
            break
        try:
            _challenge(ctx, ids[cur], fought > 0, gains)
        except p.AllianceError as exc:
            why = f"第 {cur + 1} 层结算未完成：{exc}"
            break
        fought += 1
        try:
            updated = _tower(ctx.refresh())
        except p.AllianceError as exc:
            why = f"第 {cur + 1} 层已提交，但刷新轮回进度失败：{exc}"
            break
        if updated is None:
            why = f"第 {cur + 1} 层已提交，但服务器未返回有效轮回进度"
            break
        if state.num(updated.get("cur")) != cur + 1:
            why = f"第 {cur + 1} 层结算后 cur 从 {cur} 变为 {updated.get('cur')}，未按一层推进，停止且不重发"
            break
        tower = updated

    steps = (f"挑战 {fought} 层" if fought else "") + ("，" if fought and claimed else "") + (f"自动领取 {claimed} 个奖励" if claimed else "")
    unfinished = cur < target or any(_status(tower, floor_id) == 1 for floor_id in ids[:target])
    if why or (ctx.stopped and unfinished):
        if not why and not steps:
            return TaskResult(ID, SKIPPED, "已被用户停止，未挑战轮回试炼")
        return TaskResult(ID, BLOCKED, "；".join(filter(None, (steps, why or "用户停止"))) + dungeon.gain_note(gains))
    detail = f"轮回试炼完成至{dict(TARGETS)[target]}"
    if not steps:
        return TaskResult(ID, SKIPPED, detail + "（目标已完成）")
    return TaskResult(ID, SUBMITTED, f"{detail}，{steps}{dungeon.gain_note(gains)}")
