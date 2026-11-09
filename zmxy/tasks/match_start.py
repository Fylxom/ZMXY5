"""斗神大会、灵宠乱斗共用的“开始匹配”：服务器因为逃跑次数拒绝匹配时，不停下也不原地等，
交出“过几分钟再来”的占位结果（TaskResult.retry_in），执行器先做这个账号后面的任务，到时间再重跑这个任务
（重跑时照常检查开放时间、次数；同一个任务最多等 runner.LATER_MAX 轮）。

真机提示（2026-11-07 灵宠乱斗）：“你已逃跑累计达到3次，请5分钟后再继续匹配”。其他拒绝原样停下，由各任务自己处理。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .. import dungeon, protocol as p
from ..core import SKIPPED, TaskContext, TaskResult

ESCAPE = re.compile(r"逃跑.{0,12}?(\d+)\s*次.{0,12}?(\d+)\s*分钟")
EXTRA_SECONDS = 20  # 多等一点，免得刚好差几秒又被拒


class Banned(Exception):
    """逃跑禁赛：seconds 秒后再匹配。故意不继承 dungeon.Stop，任务里按停止处理的地方接不住它，一直传到任务入口。"""

    def __init__(self, message: str, seconds: int) -> None:
        super().__init__(message)
        self.seconds = seconds

    def result(self, tid: str, done: str = "") -> TaskResult:
        wait = f"{self.seconds // 60} 分 {self.seconds % 60} 秒" if self.seconds % 60 else f"{self.seconds // 60} 分钟"
        detail = f"{self}；{wait}后再匹配，先做后面的任务"
        return TaskResult(tid, SKIPPED, f"{done}；{detail}" if done else detail, pending=True, retry_in=self.seconds)


def escape_wait(message: str) -> int | None:
    """拒绝说明是逃跑禁赛 → 要等几秒；不是 = None。"""
    m = ESCAPE.search(message)
    return int(m[2]) * 60 + EXTRA_SECONDS if m and 0 < int(m[2]) <= 60 else None


def start(ctx: TaskContext, route: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    """发开始匹配。逃跑禁赛 → Banned；其他拒绝 → dungeon.Stop。"""
    try:
        return ctx.request(route, body)
    except p.AllianceError as exc:
        seconds = escape_wait(str(exc))
        if seconds is None:
            raise dungeon.Stop(f"开始匹配失败：{exc}") from exc
        raise Banned(str(exc), seconds) from exc
