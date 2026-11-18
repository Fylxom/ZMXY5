"""指定对手 / 洞府的“预览”（2026-11-14 用户要求）：按填的 UID + 角色编号只读查对方，核对填得对不对。

不是任务（不进任务表）：界面“选择”对话框里点“预览”时，后台登录当前存档，用 ``SPEC`` 绑定会话（只声明下面几个只读路由）。
* 资料（``getOfflineUserInfo {uid, rid, index:0}``）：名字、角色、等级、战力；角色编号不存在时服务器会返回别的角色，照实写出来。
  没填角色编号：只按 UID 查（对方现在用的角色），编号取返回的 ``user.entityId``，对话框自动填进输入框，再接着查下面的。
* 竞技场 / 斗宠：查详情（同挑战前那一步），查得到 = 已报名，写出战角色战力 / 宠物战力和战功；查不到写服务器原话。
  竞技场的战力是报名时的快照，可能和资料里的不一样。
* 藏宝洞：``getPublishLandform {rid}`` 列出发布的洞府和星级，标出执行时会打的那个（星级最高）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .. import api, data, protocol as p, state
from ..taskdef import TaskResult, TaskSpec
from . import arena, treasure_cave, uid_lookup
from ..uid import sheet

Request = Callable[..., Mapping[str, Any]]

SPEC = TaskSpec("target_preview", "预览对手", run=lambda ctx: TaskResult("target_preview", "skipped", ""),
                reads=frozenset({api.R_OFFLINE_USER_INFO, arena.ARENA.details, arena.PET_ARENA.details, treasure_cave.R_PUBLISHED}))


def _n(value) -> str:
    num = state.num(value)
    return "?" if num is None else f"{int(num):,}"


def role_text(char_id) -> str:
    """charId → “小白龙（神·雷霆）”：前面是排行榜页签那种基础角色名（同 roleType、roleGrade=0），括号里是这个形态。"""
    try:
        row = state.row("Role", state.num(char_id))
        rows = state.table("Role").values() if row else ()
    except data.DataError:  # 读不到角色表：只写编号，不影响预览别的内容
        row, rows = None, ()
    if not row:
        return f"角色 {char_id}" if char_id not in (None, "") else "角色未知"
    name = str(row.get("name") or char_id)
    base = next((str(r.get("name")) for r in rows
                 if isinstance(r, Mapping) and r.get("roleType") == row.get("roleType") and r.get("roleGrade") == 0 and r.get("name")), "")
    return name if not base or base == name else f"{base}（{name}）"


def profile_line(request: Request, uid: int, role: int | None) -> tuple[str, int | None]:
    """(资料那一行, 角色编号)。role=None：没填编号，查对方现在用的角色，编号取服务器返回的（查不出 = None）。"""
    rid = None if role is None else f"{uid}_r{role}"
    try:
        resp = api.offline_user_info(request, uid, rid=rid, index=None if rid is None else 0)
    except p.AllianceError as exc:
        return f"查不到对方资料（UID 或角色编号可能填错了）：{exc}", None
    returned = sheet.resolve_returned_target(resp)
    if role is None:
        role = uid_lookup.parse_rid(uid, returned["rid"]) if returned else None
        if role is None:
            return "查到了这个 UID，但服务器没告诉现在用的是第几个角色，请自己填角色编号", None
    elif returned and returned["rid"] != rid:
        return f"没有角色编号 {role}：服务器返回的是 {uid_lookup.returned_name(uid, returned)}，请核对编号", None
    user = resp.get("user") if isinstance(resp.get("user"), Mapping) else {}
    level = state.num(user.get("level"))
    return (f"{user.get('name') or '（没有名字）'} · {role_text(user.get('charId'))}"
            + (f" · {int(level)} 级" if level is not None else "") + f" · 战力 {_n(user.get('fightPower'))}"), role


def arena_line(request: Request, rid: str, pet: bool) -> str:
    a = arena.PET_ARENA if pet else arena.ARENA
    try:
        row = arena._flat(request(a.details, {"targetId": rid}))
    except p.AllianceError as exc:
        return f"{a.title}：查不到（{exc}），可能没报名；执行时会改打榜首"
    if pet:
        total, battle = state.num(row.get("totalScore")), state.num(row.get("battleScore"))
        power = _n(total - battle) if total is not None and battle is not None else "?"
        return f"{a.title}：已报名 · 宠物战力 {power} · 战功 {_n(total)}"
    who = row.get("p2") if state.num(row.get("index")) else row.get("p1")
    who = who if isinstance(who, Mapping) else {}
    return (f"{a.title}：已报名 · 出战 {who.get('name') or role_text(who.get('charId'))} 战力 {_n(who.get('fightPower'))}"
            f" · 战功 {_n(row.get('totalScore'))}")


def cave_line(request: Request, rid: str) -> str:
    try:
        rows = request(treasure_cave.R_PUBLISHED, {"rid": rid}).get("data")
    except p.AllianceError as exc:
        return f"藏宝洞：查不到（{exc}），执行时会改打推荐"
    rows = [r for r in rows if isinstance(r, Mapping) and r.get("entityId")] if isinstance(rows, list) else []
    if not rows:
        return "藏宝洞：没有发布洞府，执行时会改打推荐"

    def star(r) -> int:
        return int(state.num(state.find(r, "defensiveLevel", depth=3)) or 0)
    best = max(rows, key=star)  # 和 treasure_cave.target_cave 同一个挑法
    caves = "，".join(f"{star(r)} 星（挑战码 {r['entityId']}）" + ("← 会打这个" if r is best and len(rows) > 1 else "") for r in rows)
    return f"藏宝洞：发布了 {len(rows)} 个洞府：{caves}"


def preview(request: Request, task_id: str, uid: int, role: int | None) -> tuple[str, int | None]:
    """(给用户看的文字, 角色编号)。没填编号时用服务器返回的编号接着查，对话框把它填进输入框；编号也查不出就只写资料那一行。"""
    first, found = profile_line(request, uid, role)
    lines = [first]
    if role is None:
        if found is None:
            return first, None
        lines[0] += f"（现在用的是角色编号 {found}，已自动填入）"
    rid = f"{uid}_r{found if role is None else role}"
    if task_id == treasure_cave.ID:
        lines.append(cave_line(request, rid))
    else:
        lines.append(arena_line(request, rid, pet=task_id == arena.PET_ARENA.id))
    return "\n".join(lines), found if role is None else role
