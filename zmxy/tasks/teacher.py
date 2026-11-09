"""师徒授课、师徒学艺：各用一次每日免费次数。次数上限取自 Teacher 表，已用次数取自 everyDaySetting。"""

from __future__ import annotations

from .. import dungeon, api, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ROUTE_INFO = api.R_APPRENTICESHIP_DATA
ROUTE_TEACH = "scene.apprenticeshipHandler.giveLessons"
ROUTE_LEARN = "scene.apprenticeshipHandler.getBuffer"


def _free_left(ctx: TaskContext, limit_key: str, used_key: str) -> int | None:
    """今日剩余免费次数；表或今日状态读不到 → None。"""
    teacher = state.row("Teacher", 1)
    limit = state.num(teacher.get(limit_key)) if teacher else None
    used = state.used(ctx.info, used_key)
    return None if limit is None or used is None else max(0, limit - used)


def learn_id(master: dict) -> int | None:
    """免费学艺的档位：按各徒弟好感度之和，取 TeacherLearn 里 likingAll 不小于它的第一档。"""
    apprentices = master.get("apprentice")
    if not isinstance(apprentices, dict):
        return None
    liking = [state.num(a.get("liking")) for a in apprentices.values() if isinstance(a, dict)]
    if any(v is None for v in liking):
        return None
    total = sum(liking)
    for row in sorted(state.table_rows("TeacherLearn"), key=lambda r: state.num(r.get("id")) or 0):
        top, rid = state.num(row.get("likingAll")), state.num(row.get("id"))
        if top is not None and rid is not None and total <= top:
            return rid
    return None


def _relation(ctx: TaskContext, tid: str):
    """师徒关系响应；读不到返回阻止结果。"""
    try:
        return api.apprenticeship_data(ctx.request)
    except p.AllianceError as exc:
        return TaskResult(tid, BLOCKED, f"读取师徒关系失败：{exc}")


def _send(ctx: TaskContext, tid: str, route: str, body: dict, done: str) -> TaskResult:
    gains = dungeon.Gains()
    gains.add(ctx.request(route, body), ctx.session.drain_pushes())
    return TaskResult(tid, SUBMITTED, done + dungeon.gain_note(gains))


@task("teacher_teach", "师徒授课", reads={ROUTE_INFO}, writes={ROUTE_TEACH}, account_wide=True)
def teach(ctx: TaskContext) -> TaskResult:
    tid = "teacher_teach"
    resp = _relation(ctx, tid)
    if isinstance(resp, TaskResult):
        return resp
    left = _free_left(ctx, "teachingDayNum", "giveLessonCnt")
    if left is None:
        return TaskResult(tid, BLOCKED, "无法确认今日授课次数，未授课")
    mine = state.section(resp, "my")
    if not isinstance(mine, dict) or not mine.get("userdata"):
        return TaskResult(tid, SKIPPED, "当前角色没有可授课的徒弟")
    if left <= 0:
        return TaskResult(tid, SKIPPED, "今日授课次数已用完")
    if ctx.stopped:
        return TaskResult(tid, SKIPPED, "已停止，未授课")
    return _send(ctx, tid, ROUTE_TEACH, {}, "授课完成")


@task("teacher_learn", "师徒学艺", reads={ROUTE_INFO}, writes={ROUTE_LEARN}, account_wide=True)
def learn(ctx: TaskContext) -> TaskResult:
    tid = "teacher_learn"
    resp = _relation(ctx, tid)
    if isinstance(resp, TaskResult):
        return resp
    master = state.section(resp, "master")
    if master is None:
        return TaskResult(tid, SKIPPED, "当前角色没有师父，跳过学艺")
    left = _free_left(ctx, "learnNum_free", "personFreeLearnNum")
    lid = learn_id(master)
    if left is None or lid is None:
        return TaskResult(tid, BLOCKED, "无法确认今日免费学艺次数或学艺档位，未学艺")
    if left <= 0:
        return TaskResult(tid, SKIPPED, "今日免费学艺次数已用完")
    if ctx.stopped:
        return TaskResult(tid, SKIPPED, "已停止，未学艺")
    return _send(ctx, tid, ROUTE_LEARN, {"type": 0, "id": lid}, "免费学艺完成")
