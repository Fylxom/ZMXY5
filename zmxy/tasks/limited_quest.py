"""限时任务：自动识别当前开放的版本活动任务（金秋镇煞、玄塔有灵……），接取并领取奖励。

识别规则（不写死任务名，以后换新活动自动适用）：``TaskType`` 表里 ``label == 4`` 的任务，
其 ``activityId`` 对应的 ``Activity`` 档期包含服务器时间；有前置任务（``preTaskId`` > 1）的要等前置领完。

* 接取（前置步骤，先于所有任务（排在日常任务的接取后面），这样之后打副本才计入进度）：info.quest 里没有记录或 status==0 → ``acceptQuest {qId}``；
* 领取（本任务最后执行，等前面的副本打完再刷新）：status==2 → ``commitQuest {qId}``。
* 龙宫寻宝的龙宫任务（``DragonTreasureTask`` 表）不需要接取：info.quest 里 status==2 的直接 ``commitQuest``（客户端 GameTaskMediator 也是这样领）。
任务条件本身（打噩梦副本、打九宫玄塔）由日常 / 其他任务去打，这里只汇报“还有几个没完成”。
"""

from __future__ import annotations

from collections.abc import Mapping

from .. import dungeon, protocol as p, quest, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from .rewards import dungeon_gate_open

ID = "limited_quest"
LABEL = 4  # TaskType.label：版本 / 限时活动任务


def open_quests(now: int | None) -> dict[int, str]:
    """当前档期内的限时任务 {任务 id: 名称}。"""
    out = {}
    for r in state.table_rows("TaskType"):
        qid = state.num(r.get("id"))
        if state.num(r.get("label")) == LABEL and qid is not None and state.activity_open(r.get("activityId"), now):
            out[qid] = str(r.get("name") or qid)
    return dict(sorted(out.items()))


def _can_accept(info, qid: int) -> bool:
    """同客户端 TaskManager.extractTask：角色等级够，前置关卡通关。"""
    row = state.row("TaskType", qid)
    if not row:
        return False
    role = info.get("p1") if isinstance(info, Mapping) else None
    level, required = state.num(role.get("level")) if isinstance(role, Mapping) else None, state.num(row.get("level"))
    if level is None or required is None or level < required:
        return False
    return dungeon_gate_open(info, row)


def accept(ctx: TaskContext) -> TaskResult | None:
    ctx.session.carry.pop(ID, None)
    ctx.session.carry.pop(f"{ID}:accept_failed", None)
    if ctx.now is None:
        return TaskResult(ID, BLOCKED, "没有取得服务器时间，无法判断限时任务是否开放，未接取")
    quests = open_quests(ctx.now)
    if not quests:
        return None
    status = quest.status(ctx.info)
    if status is None:
        return TaskResult(ID, BLOCKED, "服务器未返回任务列表，无法确认限时任务状态，未接取")
    got, failed = [], []
    try:
        for qid, name in quests.items():
            pre = state.num((state.row("TaskType", qid) or {}).get("preTaskId"))
            if status.get(qid, quest.FRESH) != quest.FRESH or (pre != -1 and status.get(pre) != quest.DONE):
                continue
            if not _can_accept(ctx.info, qid):
                continue
            if ctx.stopped:
                break
            try:
                quest.accept(ctx.request, qid)
            except p.AllianceError as exc:
                failed.append(f"{name}({exc})")
                continue
            got.append(name)
            ctx.log(f"已接取限时任务 {'、'.join(got)}")
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"接取限时任务：已接 {'、'.join(got) or '无'}；{exc}") from exc
    note = (f"已接取 {'、'.join(got)}" if got else "") + (f"；接取被拒绝：{'、'.join(failed)}" if failed else "")
    ctx.session.carry[ID] = note.lstrip("；")
    if failed:
        ctx.session.carry[f"{ID}:accept_failed"] = "yes"
    return None


@task(ID, "限时任务", writes={quest.R_ACCEPT, quest.R_COMMIT}, group="活动", prelude=accept, last=True, window=lambda now: bool(open_quests(now)))
def run(ctx: TaskContext) -> TaskResult:
    note = ctx.session.carry.pop(ID, "")
    accept_failed = ctx.session.carry.pop(f"{ID}:accept_failed", "") == "yes"
    ctx.log("领取限时任务奖励")
    info = ctx.refresh()  # 前面的任务会改变进度，不能用登录时的快照
    quests = open_quests(ctx.now)
    status = quest.status(info)
    if status is None:
        return TaskResult(ID, BLOCKED, "服务器未返回任务列表，无法领取限时任务奖励")
    gains, claimed, failed = dungeon.Gains(), [], []
    dragon = {qid: f"龙宫任务·{state.row('DragonTreasureTask', qid).get('name')}" for qid, st in status.items()
              if st == quest.READY and state.row("DragonTreasureTask", qid)}
    claimable = [*((q, n) for q, n in quests.items() if status.get(q) == quest.READY), *dragon.items()]
    unattempted = []
    try:
        for index, (qid, name) in enumerate(claimable):
            if ctx.stopped or ctx.winding:
                unattempted = [n for _, n in claimable[index:]]
                break
            try:
                gains.add(quest.commit(ctx.request, qid), ctx.session.drain_pushes())
            except p.AllianceError as exc:
                failed.append(f"{name}({exc})")
                continue
            claimed.append(name)
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"已领取 {'、'.join(claimed) or '无'}；{exc}") from exc
    waiting = [n for q, n in quests.items() if status.get(q) == quest.DOING]
    done = [n for q, n in quests.items() if status.get(q) == quest.DONE]
    parts = [x for x in (
        note,
        f"已领取 {'、'.join(claimed)}{dungeon.gain_note(gains)}" if claimed else "",
        f"未完成：{'、'.join(waiting)}" if waiting else "",
        f"此前已领取：{'、'.join(done)}" if done and not claimed and not waiting else "",
        f"领取被拒绝：{'、'.join(failed)}" if failed else "",
        f"已停止，未领取：{'、'.join(unattempted)}" if unattempted else "",
    ) if x]
    text = "；".join(parts) or ("没有可接取或可领取的限时任务" if quests else "当前没有开放的限时任务")
    partial = bool(unattempted and (claimed or note))
    if failed or accept_failed or partial:
        return TaskResult(ID, BLOCKED, text)
    return TaskResult(ID, SUBMITTED if claimed or note else SKIPPED, text)
