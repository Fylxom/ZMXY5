"""善法堂：给瑶瑶战力榜榜首点赞（每日免费次数）。每次先查榜首，不做缓存。"""

from __future__ import annotations

import json

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "shanfa_like"
ROUTE_RANK = "scene.rankHandler.getRank"
ROUTE_LIKE = "scene.userHandler.criticiseTop1Like"
RANK_TYPE, LIKE_TYPE = 8, "role_8"


def free_remaining(info) -> int | None:
    common = state.row("Common", 1)
    limit = state.num(common.get("loveGiftFreeNum")) if common else None
    used = state.used(info, "criticiseTop1")
    return None if limit is None or limit < 0 or used is None else max(0, limit - used)


def top_uid(rows) -> str | None:
    top = rows[0] if isinstance(rows, list) and rows else None
    if isinstance(top, str):  # 个别榜单行是 JSON 字符串
        try:
            top = json.loads(top)
        except ValueError:
            return None
    if not isinstance(top, dict):
        return None
    uid = str(top.get("uid") or str(top.get("id") or "").split("_", 1)[0])
    return uid if uid.isdigit() else None


@task(ID, "善法堂", reads={ROUTE_RANK}, writes={ROUTE_LIKE}, account_wide=True)
def run(ctx: TaskContext) -> TaskResult:
    left = free_remaining(ctx.info)
    if left is None:
        return TaskResult(ID, BLOCKED, "无法确认今日免费点赞次数，未发送请求")
    if left <= 0:
        return TaskResult(ID, SKIPPED, "今日免费点赞次数已用完")
    uid = top_uid(ctx.request(ROUTE_RANK, {"type": RANK_TYPE, "page": 1, "index": 0}).get("rows"))
    if uid is None:
        return TaskResult(ID, BLOCKED, "没有读到瑶瑶战力榜榜首，未点赞")
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未点赞")
    try:
        resp = ctx.request(ROUTE_LIKE, {"uid": uid, "type": LIKE_TYPE})
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"点赞榜首 UID={uid} 失败：{exc}")
    gains = dungeon.Gains()
    gains.add(resp, ctx.session.drain_pushes())
    return TaskResult(ID, SUBMITTED, f"已点赞榜首 UID={uid}{dungeon.gain_note(gains)}")
