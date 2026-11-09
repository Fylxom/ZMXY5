"""领奖类任务共用的小步骤。

本模块不注册任务：放在这里而不是某个任务文件里，是因为任务文件互相导入会改变 ``tasks/__init__`` 的注册顺序（= 界面显示顺序）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import dungeon, state
from ..core import TaskContext


def record(ctx: TaskContext, gains: dungeon.Gains, response) -> str:
    """把这次响应的奖励记进总数，返回这一次的奖励说明（给日志）。"""
    pushes = ctx.session.drain_pushes()
    gains.add(response, pushes)
    one = dungeon.Gains()
    one.add(response, pushes)
    return dungeon.gain_note(one)


def with_idle_error(ctx: TaskContext, text: str) -> str:
    """等待时连接中断的说明：有轮询错误就接在后面。"""
    error = getattr(ctx.session, "idle_error", None)
    return text + (f"（{error}）" if error else "")


def dungeon_history(info: Any) -> Mapping[str, Any] | None:
    """副本通关记录 {关卡 id 字符串: 记录}；服务器给的可能是字典或列表，读不到 → None。"""
    history = info.get("dungeon") if isinstance(info, Mapping) else None
    if isinstance(history, list):
        return {str(r.get("id")): r for r in history if isinstance(r, Mapping)}
    return history if isinstance(history, Mapping) else None


def passed(history: Mapping[str, Any] | None, mid: Any) -> bool:
    """这一关打通过（passNum > 0）；记录读不到、这一关没有记录都算没通过。"""
    record = (history or {}).get(str(mid))
    return isinstance(record, Mapping) and (state.num(record.get("passNum")) or 0) > 0


def dungeon_gate_open(info: Any, row: Mapping[str, Any] | None) -> bool:
    """TaskType.perDungeonId 的通关条件满足了没有（-1 = 没有条件；客户端不展示没满足的任务）。"""
    required = state.num((row or {}).get("perDungeonId"))
    if required == -1:
        return True
    return required is not None and required > 0 and passed(dungeon_history(info), required)
