"""UID资料查询：按 UID 只读调用官方资料接口，分类整理后在程序内弹窗展示。"""

from __future__ import annotations

from datetime import datetime
from .. import api, protocol as p, state
from ..uid import sheet
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "uid_lookup"
R_LOOK = api.R_OFFLINE_USER_INFO


SIDES = (("1p", "1P"), ("2p", "2P"))  # 查这个角色存档的 1P 还是 2P：entityId 的 _c0 / _c1，也就是请求里的 index
NOTE = ("不填角色编号：查对方现在用的角色。\n"
        "要查对方别的存档：填角色编号，再选 1P/2P。角色编号 = 对方账号里第几个创建的角色，从 0 数起；"
        "删掉的角色也占一个号，所以和存档在第几格无关。\n"
        "不知道编号就先不填查一次，结果里会写对方现在这个角色的编号。")


def target_name(role: int, index: int) -> str:
    return f"角色编号 {role} · {SIDES[index][1] if index < len(SIDES) else f'第 {index + 1} 位'}"


def parse_rid(uid: int, rid: str) -> int | None:
    """“UID_r编号”里的编号；不是这个 UID 的或格式不对 = None。"""
    head, marker, tail = str(rid).partition("_r")
    return int(tail) if marker and head == str(uid) and tail.isdigit() else None


def returned_name(uid: int, returned) -> str:
    role = parse_rid(uid, returned["rid"])
    return target_name(role, returned["index"]) if role is not None else f"{returned['rid']}，index {returned['index']}"


@task(ID, "UID资料查询", reads={R_LOOK}, group="周常", overnight=True)
def run(ctx: TaskContext) -> TaskResult:
    """选项（周常页底部 UID 查询卡片，各存档各存）：uid、role（角色编号，留空 = 对方现在的角色）、side（1p / 2p）。"""
    uid = state.num(ctx.options.get("uid"))
    if uid is None or uid <= 0:
        return TaskResult(ID, SKIPPED, "未填写对方 UID")
    raw = str(ctx.options.get("role") if ctx.options.get("role") is not None else "").strip()
    rid = index = None
    if raw:
        role = state.num(raw)
        if role is None or role < 0 or not raw.isdigit():
            return TaskResult(ID, BLOCKED, "角色编号必须是 0 或正整数")
        rid, index = f"{uid}_r{role}", 1 if ctx.options.get("side") == "2p" else 0
    target = f"UID {uid}" + (f"（{target_name(role, index)}）" if rid else "")
    try:
        response = api.offline_user_info(ctx.request, uid, rid=rid, index=index)
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"查询 {target} 失败：{exc}")
    returned = sheet.resolve_returned_target(response)
    note = ""
    if returned:
        got = returned_name(uid, returned)
        if rid and (returned["rid"], returned["index"]) != (rid, index):
            note = f"没有这个角色，服务器返回的是 {got}"
        elif not rid:
            note = f"对方现在的角色是 {got}"
    query = "，".join(x for x in (target_name(role, index) if rid else "", note) if x)
    data = sheet.build(uid, response, datetime.now(state._CN).isoformat(timespec="seconds"), query=query)
    return TaskResult(ID, SUBMITTED, f"{target} 查询完成" + (f"；{note}" if note else "") + "；分类结果已在程序内弹窗显示",
                      popup={"title": f"UID资料查询 · {target}", "text": data.text(), "sheet": data})
