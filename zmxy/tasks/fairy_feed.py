"""幻兽喂养：为 1P、2P 各选一只幻兽，用普通灵魂喂养，直到免费次数、灵魂或角色等级用完。

选择：界面“选幻兽”里 1P / 2P 各一个下拉框。幻兽列表来自**上一次运行**时读到的角色数据
（存在账号设置的 profile 里）：界面“选幻兽”里的“读取”按钮会登录并立即读取；任务运行时也会顺便更新。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, task

ID = "fairy_feed"
ROUTE = "scene.fairyHandler.upgrade"
PROFILE_KEY = "fairy_feed_owned"
OWNERS = ("1P", "2P")


def _stage_name(template_id: int, step: int) -> str:
    """幻兽当前阶段的显示名；服务器可能给基础 id 也可能给资源 id。"""
    rows = [r for r in state.table_rows("Fairy")
            if state.num(r.get("step")) == step and template_id in (state.num(r.get("originalId")), state.num(r.get("resourceId")))]
    row = next((r for r in rows if state.num(r.get("resourceId")) == template_id), rows[0] if rows else None)
    if row and row.get("name"):
        return str(row["name"])
    base = state.row("FairyId", template_id) if step == 1 else None  # FairyId 只有未进阶的名字
    return str(base.get("name", "")) if base else ""


def owned(info: Mapping[str, Any]) -> list[dict]:
    """背包 + 1P/2P 装备里所有幻兽：{entityId, name, step, level, index(0=1P/1=2P)}，同一只取阶最高的那份。"""
    found: dict[str, dict] = {}

    def walk(v: Any, owner: int | None) -> None:
        if isinstance(v, Mapping):
            idx = state.num(v.get("index"))
            idx = idx if idx in (0, 1) else owner
            eid, tid, step, level = str(v.get("entityId") or ""), state.num(v.get("id")), state.num(v.get("step")), state.num(v.get("level"))
            if eid and None not in (tid, step, level) and idx in (0, 1):
                old = found.get(eid)
                if old is None or step > old["step"]:
                    found[eid] = {"entityId": eid, "name": _stage_name(tid, step) or f"幻兽{tid}", "step": step, "level": level, "index": idx}
            for key, child in v.items():
                k = str(key).lower()
                walk(child, 0 if k in ("0", "p1") else 1 if k in ("1", "p2") else idx)
        elif isinstance(v, list):
            for c in v:
                walk(c, owner)

    bag = info.get("bag")
    walk(bag.get("fairy") if isinstance(bag, Mapping) else None, None)
    for owner, key in enumerate(("p1", "p2")):
        role = info.get(key)
        eq = role.get("equipments") if isinstance(role, Mapping) else None
        walk(eq.get("fairy") if isinstance(eq, Mapping) else None, owner)
    return sorted(found.values(), key=lambda f: (f["index"], f["name"], f["step"], f["level"], f["entityId"]))


def _items_for(owner: int):
    def items(acc: Mapping[str, Any]) -> list[tuple[str, str]]:
        fairies = (acc.get("profile") or {}).get(PROFILE_KEY) or []
        return [(f["entityId"], f"{f['name']}\t{f['step']}阶 · {f['level']}级")  # \t 后面是下拉框里靠右的灰色小字
                for f in fairies if f.get("index") == owner]
    return items


def remaining(info, now, owner: int, level: int) -> int | None:
    """该角色今日还能免费(灵魂)喂养几次。"""
    row = state.row("FairyFeed", level)
    used = state.used(info, f"feedDayNum_{owner}")
    if not row or row.get("consume_type") != "coin" or row.get("consume_subType") != "spirit" or used is None:
        return None
    key = "feedDayNum_Activity" if state.activity_open(row.get("activityId"), now) else "feedDayNum"
    base = state.num(row.get(key))
    vip = state.row("VIP", state.vip_level(info))
    extra = state.num(vip.get("freeFairyFeedNum")) if vip else 0  # 读不到 VIP 时只按基础次数，宁少勿多
    return None if base is None or base < 0 or extra is None else max(0, base + extra - used)


def _profile(info: Mapping[str, Any]) -> dict:
    return {PROFILE_KEY: owned(info), "fairy_feed_p2": isinstance(info.get("p2"), Mapping)}


def _feed(ctx: TaskContext, fairy: dict, owner: int, left: int, spirit: int, role_level: int, notes: list[str]) -> tuple[int, int, int, int, bool]:
    """喂一只，到次数、灵魂或角色等级用完。返回 (喂了几次, 现等级, 剩余次数, 剩余灵魂, 是否出错要全部停下)。"""
    tag, eid, level, name = OWNERS[owner], fairy["entityId"], fairy["level"], fairy["name"]
    count = 0
    while left > 0 and level < role_level and not ctx.stopped:
        cost = state.num((state.row("FairyFeed", level) or {}).get("consume_count")) or 0
        if cost <= 0 or spirit < cost:
            notes.append(f"{tag} {name} 灵魂不足，停止")
            break
        try:
            resp = ctx.request(ROUTE, {"entityId": eid, "feedId": level, "index": owner})
        except p.AllianceError as exc:
            notes.append(f"{tag} {name} 喂养失败：{exc}")
            return count, level, left, spirit, True
        new = resp.get("fairy") if isinstance(resp, Mapping) else None
        new_level = state.num(new.get("level")) if isinstance(new, Mapping) and str(new.get("entityId")) == eid else None
        if new_level is None:
            notes.append(f"{tag} {name} 请求已返回，但缺少匹配的新状态，结果未确认；停止")
            return count, level, left, spirit, True
        count, left, spirit, level = count + 1, left - 1, spirit - cost, new_level
    return count, level, left, spirit, False


@task(ID, "幻兽喂养", writes={ROUTE}, needs=(("fairy_p1", "fairy_p2"),),
      choices=Choices(None, "幻兽", load=lambda info: _profile(info), picks=(Pick("fairy_p1", "1P", _items_for(0)), Pick("fairy_p2", "2P", _items_for(1)))))
def run(ctx: TaskContext) -> TaskResult:
    info = ctx.info
    mine = owned(info)
    profile = _profile(info)
    wanted = [(o, str(e)) for o, e in ((0, ctx.options.get("fairy_p1")), (1, ctx.options.get("fairy_p2"))) if e]

    def result(status: str, detail: str) -> TaskResult:
        return TaskResult(ID, status, detail, profile=profile)

    if not wanted:
        return result(SKIPPED, f"没有选择要喂养的幻兽（已读取：1P {sum(f['index'] == 0 for f in mine)} 只，2P {sum(f['index'] == 1 for f in mine)} 只，可在“选幻兽”里选择）")
    try:
        info = ctx.refresh()
    except p.AllianceError as exc:
        return result(BLOCKED, f"刷新幻兽喂养状态失败，未喂养：{exc}")
    mine = owned(info)
    profile = _profile(info)
    spirit = state.spirit(info)
    if spirit is None:
        return result(BLOCKED, "没有读到灵魂余额，未喂养")
    notes, fed, bad = [], 0, False
    for owner, eid in wanted:
        tag = OWNERS[owner]
        if ctx.stopped:
            notes.append(f"{tag} 用户停止，未喂养")
            break
        fairy = next((f for f in mine if f["entityId"] == eid and f["index"] == owner), None)
        role = info.get(("p1", "p2")[owner])
        role_level = state.num(role.get("level")) if isinstance(role, Mapping) else None
        if fairy is None or role_level is None:
            notes.append(f"{tag} 所选幻兽不在当前存档里（或没有该角色），未喂养")
            bad = True
            continue
        name = fairy["name"]
        left = remaining(info, ctx.now, owner, fairy["level"])
        if left is None:
            notes.append(f"{tag} {name} 无法确认喂养次数，未喂养")
            bad = True
            continue
        count, level, left, spirit, failed = _feed(ctx, fairy, owner, left, spirit, role_level, notes)
        bad = bad or failed
        fed += count
        if count:
            detail = f"{tag} {name} 喂养 {count} 次，现 {level} 级"
            notes.append(detail + ("（用户停止）" if ctx.stopped else ""))
        elif ctx.stopped:
            notes.append(f"{tag} {name} 用户停止，未喂养")
        elif left == 0:
            notes.append(f"{tag} {name} 今日免费次数已用完")
        elif level >= role_level:
            notes.append(f"{tag} {name} 已达角色等级")
        if failed or ctx.stopped:
            break
    return result(BLOCKED if bad else (SUBMITTED if fed else SKIPPED), "；".join(notes))
