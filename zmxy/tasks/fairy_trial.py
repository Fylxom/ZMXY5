"""幻兽试炼：领取已完成的派遣，再用低阶幻兽派满空位；没有可派任务时用免费刷新（派遣次数用完也刷，留给明天）。不用点券。"""

from __future__ import annotations

from typing import Any, Mapping

from .. import protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "fairy_trial"
ROUTE_SEND = "scene.fairyHandler.collect"
ROUTE_REWARD = "scene.fairyHandler.getCollectReward"
ROUTE_REFRESH = "scene.fairyHandler.refreshCollectTask"  # 只发 type=0（免费）


def slots_of(value: Any) -> dict[int, Mapping[str, Any]] | None:
    data = state.find(value, "fairyCollectData", depth=8)
    if isinstance(data, Mapping):
        data = [data.get(str(i), data.get(i)) for i in range(3)]
    if not isinstance(data, list):
        return None
    return {i: s for i, s in enumerate(data[:3]) if isinstance(s, Mapping)}


def ids_of(slot: Mapping[str, Any]) -> list[str] | None:
    v = slot.get("fairy")
    if v is None:
        return []
    return [str(x) for x in v if x not in (None, "")] if isinstance(v, list) else None


def free_left(info, key: str, limit_key: str) -> int | None:
    help_row, used = state.row("FairySendHelp", 1), state.used(info, key)
    limit = state.num(help_row.get(limit_key)) if help_row else None
    return None if limit is None or limit < 0 or used is None else max(0, limit - used)


def candidates(info) -> list[dict]:
    """全部幻兽（背包 + 1P/2P 装备），按（阶、等级）从低到高；origin 是基础幻兽 id（试炼条件用它）。"""
    base = {}
    for r in state.table_rows("Fairy"):
        rid, step, orig = state.num(r.get("resourceId")), state.num(r.get("step")), state.num(r.get("originalId"))
        if rid is not None and step is not None and orig is not None:
            base[(rid, step)] = orig
    found: dict[str, dict] = {}

    def walk(v: Any) -> None:
        if isinstance(v, Mapping):
            if v.get("entityId") not in (None, ""):
                tid, step, lv = state.num(v.get("id")), state.num(v.get("step")), state.num(v.get("level"))
                if None not in (tid, step, lv) and step >= 0 and lv >= 0:
                    f = dict(v, origin=base.get((tid, step), tid), _step=step, _lv=lv)
                    old = found.get(str(v["entityId"]))
                    if old is None or (step, lv) > (old["_step"], old["_lv"]):
                        found[str(v["entityId"])] = f
            else:
                for c in v.values():
                    walk(c)
        elif isinstance(v, list):
            for c in v:
                walk(c)

    bag = info.get("bag")
    walk(bag.get("fairy") if isinstance(bag, Mapping) else None)
    for k in ("p1", "p2"):
        role = info.get(k)
        eq = role.get("equipments") if isinstance(role, Mapping) else None
        walk(eq.get("fairy") if isinstance(eq, Mapping) else None)
    return sorted(found.values(), key=lambda f: (f["_step"], f["_lv"], str(f["entityId"])))


def pick_pair(task_row: Mapping[str, Any], cands: list[dict], used: set[str]) -> tuple[str, str] | None:
    """条件 condition_1="基础id_最低阶;…"（任一）+ condition_2=另一只的最低阶；用最低阶的幻兽凑。"""
    need = []
    for order, raw in enumerate(str(task_row.get("condition_1") or "").split(";")):
        a, _, b = raw.partition("_")
        if state.num(a) is not None and state.num(b) is not None:
            need.append((state.num(a), state.num(b), order))
    first = sorted(
        (f["_step"], f["_lv"], order, str(f["entityId"]), f)
        for tid, min_step, order in need for f in cands
        if str(f["entityId"]) not in used and f["origin"] == tid and f["_step"] >= min_step
    )
    min2 = state.num(task_row.get("condition_2"))
    if not first or min2 is None:
        return None
    f1 = first[0][4]
    f2 = next((f for f in cands if str(f["entityId"]) not in used and f is not f1
               and f["origin"] != f1["origin"] and f["_step"] >= min2), None)
    return (str(f1["entityId"]), str(f2["entityId"])) if f2 else None


@task(ID, "幻兽试炼", writes={ROUTE_SEND, ROUTE_REWARD, ROUTE_REFRESH}, spends={ROUTE_REFRESH})
def run(ctx: TaskContext) -> TaskResult:
    slots, now, info = slots_of(ctx.info), ctx.now, ctx.info
    if slots is None:
        return TaskResult(ID, BLOCKED, "服务器没有返回 fairyCollectData，未领取或派遣")
    if now is None:
        return TaskResult(ID, BLOCKED, "没有取得服务器时间，无法判断派遣是否完成")
    notes, changed = [], 0

    def end(status: str, why: str = "") -> TaskResult:
        return TaskResult(ID, status if changed or status != SUBMITTED else SKIPPED, "；".join(notes + ([why] if why else [])) or "当前没有可处理的派遣")

    for pos, slot in slots.items():
        if ctx.stopped:
            notes.append("已停止，未继续领取")
            break
        ids, exp = ids_of(slot), state.seconds(slot.get("expiredTime"))
        if ids is None or exp is None:
            return end(BLOCKED, f"派遣位 {pos + 1} 状态不完整，已停止")
        if ids and 0 < exp <= now:
            try:
                resp = ctx.request(ROUTE_REWARD, {"pos": pos})
            except p.AllianceError as exc:
                return end(BLOCKED, f"派遣位 {pos + 1} 领取失败：{exc}")
            changed += 1
            new = slots_of(resp)
            if new is None:
                return end(BLOCKED, f"派遣位 {pos + 1} 领取响应没有返回新列表，已停止")
            slots = new
            notes.append(f"派遣位 {pos + 1} 已领取")

    send_left, refresh_left = free_left(info, "fairySendDayNum", "sendDayNum"), free_left(info, "fairyRefreshDayNum", "refreshDayNum")
    if send_left is None or refresh_left is None:
        return end(BLOCKED, "无法确认免费派遣/刷新次数")
    cands, table = candidates(info), state.table("FairySend")
    for _ in range(send_left + refresh_left + 1):
        if ctx.stopped:
            notes.append("已停止")
            break
        used = {i for s in slots.values() for i in (ids_of(s) or [])}
        vacant = [(pos, s) for pos, s in slots.items() if ids_of(s) == [] and state.seconds(s.get("expiredTime")) == 0]
        if not vacant:
            notes.append("今日免费派遣次数已用完" if send_left <= 0 else "三个派遣位都在进行中")
            break
        plans = []
        for pos, s in vacant:
            t = table.get(str(state.num(s.get("id"))))
            pair = pick_pair(t, cands, used) if isinstance(t, Mapping) else None
            if pair:
                plans.append((state.num(t.get("level")) or 99, pos, t, pair))
        if plans and send_left <= 0:  # 次数用完但空位里有能派的：留着明天派，不刷新
            notes.append("今日免费派遣次数已用完")
            break
        if plans:
            _, pos, t, pair = min(plans, key=lambda x: x[:2])
            try:
                resp = ctx.request(ROUTE_SEND, {"pos": pos, "ids": list(pair)})
            except p.AllianceError as exc:
                return end(BLOCKED, f"派遣位 {pos + 1} 失败：{exc}")
            changed, send_left = changed + 1, send_left - 1
            new = slots_of(resp)
            if new is None or ids_of(new.get(pos, {})) != list(pair):
                return end(BLOCKED, "派遣后服务器没有确认指定幻兽，已停止（不重发）")
            slots = new
            notes.append(f"已派遣 {t.get('name', '任务')}")
            continue
        if refresh_left <= 0:
            notes.append(("今日免费派遣次数已用完；" if send_left <= 0 else "") + "没有可派遣的任务，免费刷新次数已用完")
            break
        try:
            resp = ctx.request(ROUTE_REFRESH, {"type": 0})
        except p.AllianceError as exc:
            return end(BLOCKED, f"免费刷新失败：{exc}")
        changed, refresh_left = changed + 1, refresh_left - 1
        new = slots_of(resp)
        if new is None:
            return end(BLOCKED, "刷新响应没有返回新列表，已停止（不重发）")
        slots = new
        notes.append("已免费刷新派遣列表")
    return end(SUBMITTED)
