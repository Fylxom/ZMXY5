"""竞技场 / 斗宠大会报名（定时页）：下届报名，每周一次。

报名时间读 ``ChallengeApply``（1 竞技场 / 2 斗宠大会）的 ``applyStartTime``~``applyEndTime``（“星期-时:分:秒”，周一=1…周日=7，北京时间；
现在是周五 16:00 到周日 22:00），不在时段内不发请求。已报名 = ``everyWeekSetting.applyForArenaTime|applyForPetArenaTime`` > 0（客户端同样判断）。
请求 ``scene.arenaHandler|petArenaHandler.applyFor {index:0}``（客户端 ArenaApplyMediator：斗宠固定 0；竞技场有第二角色时弹“选择角色”，``index`` 0 = 1P / 1 = 2P，
界面“选模式”里选 1P / 2P，默认 1P，没有 2P 的存档选 2P 由服务器拒绝）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from .. import protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

_CN = timezone(timedelta(hours=8))
R_ARENA = "scene.arenaHandler.applyFor"
R_PET = "scene.petArenaHandler.applyFor"
P1, P2 = "1p", "2p"


def _offset(text) -> int | None:
    """“5-16:00:00” → 距周一 00:00 的秒数。"""
    match = re.fullmatch(r"([1-7])-(\d{1,2}):(\d{2}):(\d{2})", str(text or "").strip())
    if not match:
        return None
    day, hour, minute, second = map(int, match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return (day - 1) * 86400 + hour * 3600 + minute * 60 + second


def in_apply_window(row, now: int | None) -> bool | None:
    """现在是否在报名时段内；读不到表或时间 → None。"""
    start, end = _offset((row or {}).get("applyStartTime")), _offset((row or {}).get("applyEndTime"))
    if now is None or start is None or end is None:
        return None
    here = datetime.fromtimestamp(now, _CN)
    point = here.weekday() * 86400 + here.hour * 3600 + here.minute * 60 + here.second
    return start <= point <= end if start <= end else point >= start or point <= end


def apply(ctx: TaskContext, tid: str, title: str, row_id: int, route: str, key: str, index: int = 0) -> TaskResult:
    window = in_apply_window(state.row("ChallengeApply", row_id), ctx.now)
    if window is None:
        return TaskResult(tid, BLOCKED, f"读不到{title}报名时间（ChallengeApply），未报名")
    if not window:
        return TaskResult(tid, SKIPPED, f"不在{title}报名时间（周五 16:00 到周日 22:00）")
    setting = state.section(ctx.info, "everyWeekSetting")
    if setting is None:
        return TaskResult(tid, BLOCKED, f"读不到本周{title}报名状态（everyWeekSetting），未报名")
    used = state.num(setting.get(key, 0))
    if used is None or used < 0:
        return TaskResult(tid, BLOCKED, f"无法确认{title}本周报名状态，未报名")
    if used > 0:
        return TaskResult(tid, SKIPPED, f"{title}已报名")
    if ctx.stopped or ctx.winding:
        return TaskResult(tid, SKIPPED, "用户停止，未报名")
    try:
        ctx.request(route, {"index": index})
    except p.AllianceError as exc:
        return TaskResult(tid, BLOCKED, f"报名失败：{exc}")
    return TaskResult(tid, SUBMITTED, f"已报名{title}")


@task("arena_apply", "竞技场报名", writes={R_ARENA}, group="周常", modes=((P1, "1P"), (P2, "2P")), mode_default=P1)
def arena_apply(ctx: TaskContext) -> TaskResult:
    index = 1 if ctx.options.get("mode") == P2 else 0
    return apply(ctx, "arena_apply", "竞技场", 1, R_ARENA, "applyForArenaTime", index)


@task("pet_arena_apply", "斗宠大会报名", writes={R_PET}, group="周常")
def pet_arena_apply(ctx: TaskContext) -> TaskResult:
    return apply(ctx, "pet_arena_apply", "斗宠大会", 2, R_PET, "applyForPetArenaTime")
