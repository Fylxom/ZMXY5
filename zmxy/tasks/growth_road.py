"""成长之路：领取已完成的成长任务，再领取日/周活跃度奖励。

规则取自 Liveness / LivenessReward 表：
* 成长任务：info.quest 里 status==2（完成未领）的任务 → commitQuest {qId}
* 活跃度奖励：livenessType 0 看 everyDaySetting、1 看 everyWeekSetting 的 activePoint，
  达到 activePoint 且 livenessReward 里没有该 id → getReward {rewardId}
活跃度以领完任务后重新进入角色取得的服务器数据为准，不自己累加。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import data, dungeon, protocol as p, quest, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "growth_road"
ROUTE_REWARD = "scene.livenessHandler.getReward"

_SETTINGS = {0: "everyDaySetting", 1: "everyWeekSetting"}


def _ready_quests(info: Mapping[str, Any]) -> list[int] | None:
    """可领取的成长任务 id（按表里的 order）；服务器没给 quest → None。"""
    status = quest.status(info)
    if status is None:
        return None
    table = data.table("Liveness")
    done = {qid for qid, st in status.items() if st == quest.READY}
    rows = sorted((r for k, r in table.items() if state.num(k) in done), key=lambda r: (state.num(r.get("order")) or 0, state.num(r["id"])))
    return [state.num(r["id"]) for r in rows]


def _claimable_rewards(info: Mapping[str, Any]) -> list[int] | None:
    """已达标且未领取的活跃度奖励 id；读不到活跃度/领取记录 → None（不猜）。"""
    settings = {}
    for kind, key in _SETTINGS.items():
        s = state.find(info, key)
        if not isinstance(s, Mapping) or state.num(s.get("activePoint")) is None:
            return None
        settings[kind] = s
    out = []
    for k, row in data.table("LivenessReward").items():
        s = settings.get(state.num(row.get("livenessType")))
        if s is None:
            continue
        got = s.get("livenessReward") or {}
        claimed = {state.num(x) for x in got} if isinstance(got, (Mapping, list, tuple, set)) else set()
        rid = state.num(k)
        if rid not in claimed and state.num(s["activePoint"]) >= (state.num(row.get("activePoint")) or 10**9):
            out.append(rid)
    return sorted(out)


@task(ID, "成长之路", writes={quest.R_COMMIT, ROUTE_REWARD}, last=True)
def run(ctx: TaskContext) -> TaskResult:
    got_quests = got_rewards = 0
    gains = dungeon.Gains()

    def done(why: str) -> TaskResult:
        return TaskResult(ID, BLOCKED,
                          f"已领取成长任务 {got_quests} 项、活跃奖励 {got_rewards} 项；{why}{dungeon.gain_note(gains)}")

    def stopped() -> TaskResult:
        status = SUBMITTED if got_quests or got_rewards else SKIPPED
        return TaskResult(ID, status,
                          f"已停止；已领取成长任务 {got_quests} 项、活跃奖励 {got_rewards} 项{dungeon.gain_note(gains)}")

    if ctx.stopped:
        return stopped()
    info = ctx.refresh()  # 前面的任务会改变进度，不能用登录时的快照
    quests = _ready_quests(info)
    if quests is None:
        return TaskResult(ID, BLOCKED, "服务器没有返回成长任务状态，未领取")
    for qid in quests:
        if ctx.stopped:
            return stopped()
        try:
            gains.add(quest.commit(ctx.request, qid), ctx.session.drain_pushes())
        except p.AllianceError as exc:
            return done(f"任务 {qid} 领取失败：{exc}")
        got_quests += 1
    if got_quests:  # 活跃度以服务器为准
        if ctx.stopped:
            return stopped()
        try:
            info = ctx.refresh()
        except p.AllianceError as exc:
            return done(f"{exc}，未领取活跃奖励")
    if ctx.stopped:
        return stopped()
    rewards = _claimable_rewards(info)
    if rewards is None:
        return done("服务器没有返回活跃度或领取记录，未领取活跃奖励")
    for rid in rewards:
        if ctx.stopped:
            return stopped()
        try:
            gains.add(ctx.request(ROUTE_REWARD, {"rewardId": rid}), ctx.session.drain_pushes())
        except p.AllianceError as exc:
            return done(f"奖励 {rid} 领取失败：{exc}")
        got_rewards += 1
    if not got_quests and not got_rewards:
        return TaskResult(ID, SKIPPED, "当前没有可领取的成长任务或活跃奖励")
    return TaskResult(ID, SUBMITTED, f"成长任务 {got_quests} 项，活跃奖励 {got_rewards} 项{dungeon.gain_note(gains)}")
