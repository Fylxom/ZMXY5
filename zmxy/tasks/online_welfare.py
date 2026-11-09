"""在线福利（活动页常驻）：其他任务都结束后，领双倍掉宝率之后解锁的在线奖励。

OnlineReward 按 nextTaskId 顺序解锁：领一档刷新一次，遇到未就绪的不越级提交。
领了一部分但后续未就绪或被停止时返回 blocked，保留已确认收益。
"""

from __future__ import annotations

from collections.abc import Mapping

from .. import dungeon, protocol as p, quest, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from .double_drop import ONLINE_ACTIVITY, ONLINE_CLAIMED, ONLINE_NAME, online_quest_id, weekdays

ID = "online_welfare"


class _Halt(Exception):
    """刷新失败：停在这里，已确认的写进说明。"""


def reward_chain() -> list[int]:
    """首档双倍掉宝之后的奖励（通常是第 2–5 档和累计奖）。"""
    rows = {qid: row for row in state.table_rows("OnlineReward")
            if state.num(row.get("activityId")) == ONLINE_ACTIVITY and (qid := state.num(row.get("id"))) is not None}
    first = online_quest_id()
    if first not in rows:
        return []
    chain, current = [], first
    while (next_id := state.num(rows[current].get("nextTaskId"))) in rows and next_id != first and next_id not in chain:
        chain.append(next_id)
        current = next_id
    return chain


def reward_name(qid: int) -> str:
    row = state.row("OnlineReward", qid) or {}
    name, desc = row.get("name") or qid, row.get("describe")
    return f"{name}（{desc}）" if desc else str(name)


def server_progress(info, qid: int, target_id) -> int | None:
    """服务器任务记录里的目标进度；记录缺失或重复不猜。"""
    records = state.find(info, "quest")
    records = list(records.values()) if isinstance(records, Mapping) else records
    if not isinstance(records, list):
        return None
    mine = [r for r in records if isinstance(r, Mapping) and state.num(r.get("id", r.get("qId"))) == qid]
    progress = mine[0].get("progress") if len(mine) == 1 else None
    if not isinstance(progress, Mapping):
        return None
    return state.num(progress.get(str(target_id), progress.get(state.num(target_id))))


def claim_ready(info, qid: int, statuses: Mapping[int, int], order: list[int]) -> bool:
    """客户端会本地改状态，所以 status=1 时还要看服务器进度，或前面各档是否都已领。"""
    status = statuses.get(qid)
    if status != quest.DOING:
        return status == quest.READY
    row = state.row("OnlineReward", qid) or {}
    target = state.num(row.get("targetNum"))
    if target == 0 and qid == order[-1]:  # 客户端 currAndTotal：前五档都领了才开放累计奖
        return (state.num(row.get("preTaskId")) == order[-2]
                and all(statuses.get(previous) == quest.DONE for previous in order[:-1]))
    if target is None or target <= 0:
        return False
    amount = server_progress(info, qid, row.get("targetId"))
    return amount is not None and amount >= target


def _closed(now: int | None) -> TaskResult | None:
    if now is None:
        return TaskResult(ID, BLOCKED, "缺少服务器时间，无法确认在线福利开放日，未领取")
    if not state.activity_open(ONLINE_ACTIVITY, now):
        return TaskResult(ID, SKIPPED, "在线福利活动当前未开放")
    days = weekdays(state.row("Activity", ONLINE_ACTIVITY))
    if not days:
        return TaskResult(ID, BLOCKED, "Activity 230 没有有效的 weekDay 配置，无法确认开放日，未领取")
    return None if state.week_index(now) in days else TaskResult(ID, SKIPPED, "今天不是在线福利开放日")


class _Run:
    """一次领取：先补首档，再沿奖励链一档档领，每领一档重读状态。"""

    def __init__(self, ctx: TaskContext, first: int, chain: list[int], info, statuses: Mapping[int, int]) -> None:
        self.ctx, self.first, self.chain, self.info, self.statuses = ctx, first, chain, info, statuses
        self.gains, self.got, self.done = dungeon.Gains(), [], []

    def reload(self, claimed: str) -> None:
        """领完一档后服务器才开放下一档：重读 info 和状态。"""
        try:
            self.info = self.ctx.refresh()
        except p.AllianceError as exc:
            raise _Halt(f"已领取 {claimed}；刷新后续奖励状态失败：{exc}") from exc
        self.statuses = quest.status(self.info)
        if self.statuses is None:
            raise _Halt(f"已领取 {claimed}；服务器未返回后续在线福利状态")

    def claim(self, qid: int, name: str, rest: list[str]) -> str | None:
        """领一档，返回服务器拒绝的原因；"已领取"按此前领过算。"""
        try:
            self.gains.add(quest.commit(self.ctx.request, qid), self.ctx.session.drain_pushes())
        except UncertainOutcome as exc:
            parts = [f"已确认领取 {'、'.join(self.got) or '无'}", f"此前已领取：{'、'.join(self.done)}" if self.done else "",
                     f"{name}结果未知，未重发", f"尚未处理：{'、'.join(rest)}" if rest else ""]
            raise UncertainOutcome(f"在线福利：{'；'.join(filter(None, parts))}；{exc}") from exc
        except p.AllianceError as exc:
            if "已领取" not in str(exc):
                return str(exc)
            self.done.append(name)
        else:
            self.got.append(name)
        return None

    def claim_first(self) -> TaskResult | None:
        """首档还没领时先补领；后面领不了时返回结果。"""
        first_status = self.statuses.get(self.first)
        in_session = self.ctx.session.carry.pop(ONLINE_CLAIMED, "") == "yes"  # 双倍掉宝任务本次刚领过，快照可能还没变
        if first_status == quest.DONE or in_session:
            return None
        if first_status is None:
            return TaskResult(ID, BLOCKED, "服务器未返回首档双倍掉宝率任务状态，无法确认后续在线奖励，未领取")
        if first_status not in (quest.DOING, quest.READY):
            return TaskResult(ID, SKIPPED, "首档双倍掉宝率尚未达到领取状态，后续在线福利暂不可领取")
        if refused := self.claim(self.first, ONLINE_NAME, [reward_name(qid) for qid in self.chain]):
            return TaskResult(ID, BLOCKED, f"首档双倍掉宝率领取被拒绝：{refused}；后续奖励未提交")
        self.reload("、".join(self.got) or "首档已领过")
        return None

    def claim_chain(self) -> TaskResult:
        chain = self.chain
        waiting, failed, unready, stopped = [], "", "", False
        for index, qid in enumerate(chain):
            rest = [reward_name(x) for x in chain[index:]]
            if self.ctx.stopped:
                waiting, stopped = rest, True
                break
            status = self.statuses.get(qid)
            if status == quest.DONE:
                self.done.append(rest[0])
                continue
            if not claim_ready(self.info, qid, self.statuses, [self.first, *chain]):
                row = state.row("OnlineReward", qid) or {}
                amount = server_progress(self.info, qid, row.get("targetId"))
                unready = f"未就绪记录：qId={qid}，status={status}，progress={amount}，target={state.num(row.get('targetNum'))}"
                waiting = rest
                break
            if refused := self.claim(qid, rest[0], rest[1:]):
                failed = f"{rest[0]}({refused})"
                break
            if index + 1 < len(chain):
                self.reload("、".join(self.got) or "无")
        parts = [x for x in (
            f"已领取 {'、'.join(self.got)}{dungeon.gain_note(self.gains)}" if self.got else "",
            f"此前已领取：{'、'.join(self.done)}" if self.done and not waiting else "",
            (f"用户停止，尚未领取：{'、'.join(waiting)}" if stopped else
             f"尚未确认可领取（状态、进度或前置奖励不足）：{'、'.join(waiting)}") if waiting else "",
            f"领取被拒绝：{failed}" if failed else "",
            unready,
        ) if x]
        if failed or (waiting and self.got):
            return TaskResult(ID, BLOCKED, "；".join(parts))
        return TaskResult(ID, SUBMITTED if self.got else SKIPPED, "；".join(parts) or "在线福利后续奖励已领取")


@task(ID, "在线福利", writes={quest.R_COMMIT}, group="活动", terminal=True)
def run(ctx: TaskContext) -> TaskResult:
    if closed := _closed(ctx.now):
        return closed
    chain, first = reward_chain(), online_quest_id()
    if first is None or not chain:
        return TaskResult(ID, BLOCKED, "OnlineReward 表没有完整的在线奖励链，未领取")
    try:
        info = ctx.refresh()  # 前面的任务都结束了，读这段时间累积的奖励状态
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"刷新在线福利状态失败，未领取：{exc}")
    statuses = quest.status(info)
    if statuses is None:
        return TaskResult(ID, BLOCKED, "服务器未返回 info.quest，无法确认在线福利状态，未领取")
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已被用户停止，未领取在线福利")
    job = _Run(ctx, first, chain, info, statuses)
    try:
        return job.claim_first() or job.claim_chain()
    except _Halt as exc:
        return TaskResult(ID, BLOCKED, str(exc))
