"""UID资料查询：按 UID 只读调用官方资料接口，分类整理后在程序内弹窗展示。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .. import api, protocol as p, state
from ..uid import profile as uid_profile
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, task

ID = "uid_lookup"
R_LOOK = api.R_OFFLINE_USER_INFO


def _uid_items(cfg: Mapping[str, Any]) -> list[tuple[int, str]]:
    """让任务行摘要能显示数字框里存着的 UID。"""
    uid = state.num(((cfg.get("options") or {}).get(ID) or {}).get("uid"))
    return [(uid, str(uid))] if uid is not None and uid > 0 else []


def _no_items(_: Mapping[str, Any]) -> list[tuple[str, str]]:
    return []  # RID / index 是手填的官方参数，不从当前账号推导


@task(ID, "UID资料查询", reads={R_LOOK}, group="周常", overnight=True,
      choices=Choices(
          None,
          picks=(
              Pick("uid", "对方 UID", _uid_items, number=(1, 9_999_999_999), integer=True),
              Pick("rid", "目标 RID", _no_items, text=True),
              Pick("index", "目标 index", _no_items, number=(0, 9_999_999_999), integer=True),
          ),
          note=("只读查询，填对方 UID 即可。\n"
                "RID / index 一般不填；要查对方非默认角色时两个一起填（index 不是登录界面的存档序号）。"),
      ))
def run(ctx: TaskContext) -> TaskResult:
    uid = state.num(ctx.options.get("uid"))
    if uid is None or uid <= 0:
        return TaskResult(ID, SKIPPED, "未填写对方 UID")
    rid = str(ctx.options.get("rid") or "").strip()
    raw_index = ctx.options.get("index")
    has_index = raw_index not in (None, "")
    index = state.num(raw_index) if has_index else None
    if bool(rid) != has_index:
        return TaskResult(ID, BLOCKED, "指定目标时，RID 和 index 必须同时填写")
    if has_index and (index is None or index < 0):
        return TaskResult(ID, BLOCKED, "目标 index 必须是非负整数")
    target = f"UID {uid}" + (f"（RID {rid}，index {index}）" if rid else "")
    try:
        response = api.offline_user_info(ctx.request, uid, rid=rid or None, index=index)
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"查询 {target} 失败：{exc}")
    returned = uid_profile.resolve_returned_target(response)
    payload = uid_profile.build_payload(uid, response, datetime.now(state._CN).isoformat(timespec="seconds"),
                                        rid=rid, index=index, returned_target=returned)
    note = f"；服务端实际返回 RID {returned['rid']}、index {returned['index']}" if returned else ""
    return TaskResult(ID, SUBMITTED, f"{target} 查询完成{note}；分类结果已在程序内弹窗显示",
                      popup={"title": f"UID资料查询 · {target}", "text": uid_profile.display_text(payload), "payload": payload})
