"""“选关 + 每关次数”这类任务共用的入口。这里不注册任务（导入它不影响任务顺序）。"""

from __future__ import annotations

from typing import Any, Callable

from .. import dungeon
from ..core import BLOCKED, SKIPPED, TaskContext, TaskResult


def run_levels(ctx: TaskContext, task_id: str, limit: int, level_for: Callable[[Any], dungeon.Level | None], rejected: str) -> TaskResult:
    """校验选项后交给 ``dungeon.run_counted``。level_for 把选项值换成关卡，不是本任务的关卡返回 None；
    只要有一个选项不对就一关都不打，说明是 ``选项值 + rejected``。"""
    times = dungeon.read_times(ctx, task_id, limit, dungeon.DEFAULT_TIMES)
    if isinstance(times, TaskResult):
        return times
    if not ctx.options.get("levels"):
        return TaskResult(task_id, SKIPPED, "没有选择关卡")
    if times == 0:
        return TaskResult(task_id, SKIPPED, "每关次数留空，不执行")
    levels: list[dungeon.Level] = []
    for raw in ctx.options["levels"]:
        lv = level_for(raw)
        if lv is None:
            return TaskResult(task_id, BLOCKED, f"{raw}{rejected}")
        if all(x.mid != lv.mid for x in levels):
            levels.append(lv)
    return dungeon.run_counted(ctx, task_id, levels, times)
