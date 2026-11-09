"""聚灵池聚灵：只用普通聚灵池（id=1）的 VIP 免费次数。"""

from __future__ import annotations

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "pet_elf"
ROUTE = "scene.petHandler.petElfLottery"


def free_remaining(info) -> int | None:
    row = state.row("PetElfLottery", 1)
    if not row or state.num(row.get("isOpen")) != 1:
        return None
    limit = state.by_vip(row.get("vipFreeNum"), state.vip_level(info))
    daily = state.daily(info)
    pools = daily.get("petElfLottery", {}) if daily is not None else None
    if limit is None or not isinstance(pools, dict):
        return None
    used = state.num(pools.get("1", 0))
    return None if used is None or used < 0 else max(0, limit - used)


@task(ID, "聚灵池", writes={ROUTE}, spends={ROUTE})
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未聚灵")
    left = free_remaining(ctx.info)
    if left is None:
        return TaskResult(ID, BLOCKED, "无法确认普通聚灵池免费次数，未发送请求")
    if left <= 0:
        return TaskResult(ID, SKIPPED, "今日免费聚灵次数已用完")
    try:
        resp = ctx.request(ROUTE, {"id": 1})
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"聚灵失败：{exc}")
    gains = dungeon.Gains()
    gains.add(resp, ctx.session.drain_pushes())
    return TaskResult(ID, SUBMITTED, f"已聚灵 1 次{dungeon.gain_note(gains)}")
