"""许愿池：先领上期奖励，再从 4 个灵魂池里按优先级选 3 个，把每个池今日的次数许满。

池子：彻悟池(pos 3)、无量池(pos 4)、本阵营的轮回池(阵营*10+3)、圆满池(阵营*10+4)。
许愿只花灵魂；总费用大于灵魂余额时一次都不许。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "wish_pool"
ROUTE_INFO = "scene.activityHandler.getWishPoolInfo"
ROUTE_WISH = "scene.activityHandler.wish"
ROUTE_REWARD = "scene.activityHandler.getWishReward"

# 数字越小越优先；表里没有的奖励排最后（1000）。第 4 位池（pos%10==4）另有一套排序。
RANK = {"紫金葫芦": 1, "性格果": 3, "益灵花": 5, "明灵花": 6, "去灵花": 8, "启蒙花": 9, "万能强化石": 11,
        "蓝色羽毛": 12, "妖元": 13, "幻之精华": 14, "火灵葫芦": 15, "1级宝石盒": 16}
RANK4 = {"5级打孔器": 0, "五级打孔器": 0, "仙玉土": 2, "2级宝石盒": 7, "体质果": 7, "回血果": 7, "成长洗炼石": 7,
         "攻击果": 7, "潜力果": 7, "灵金石": 7, "祝福洗炼石": 7, "传心草": 10}
FEATHER_BRANCH = {"2星转生石", "根骨果", "生命果", "紫色羽毛", "防御果"}


def priority(o: Mapping[str, Any]) -> int:
    reward, pos = o["reward"], o["pos"]
    if pos % 10 == 4:
        if reward in ("紫金葫芦", "性格果", "益灵花", "明灵花"):
            return RANK[reward]
        if reward in FEATHER_BRANCH and o["base_reward"] == "羽枝":
            return 4
        if reward in RANK4:
            return RANK4[reward]
    return RANK.get(reward, 1000)


def rows_of(resp: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rows = resp.get(key)
    if isinstance(rows, Mapping):
        rows = [rows] if "id" in rows else list(rows.values())
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


def _week_wish(owner: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """everyWeekSetting.wish；读不到 → None。"""
    week = owner.get("everyWeekSetting")
    wish = week.get("wish") if isinstance(week, Mapping) else None
    return wish if isinstance(wish, Mapping) else None


def prior_reward_ready(resp: Mapping[str, Any], info: Mapping[str, Any]) -> bool | None:
    """上期有选择、没领过、且服务器显示该池有奖励 → True；信息不足 → None。"""
    last_key = str(resp.get("lastKey") or "")
    if not last_key:
        return None
    wish = next((w for w in map(_week_wish, (resp, info)) if w is not None), None)
    if wish is None:
        return None
    prev = wish.get(last_key)
    pos = prev.get("pos") if isinstance(prev, Mapping) else None
    if not isinstance(pos, Mapping) or not pos or "reward" in prev:
        return False
    if "last" not in resp:
        return None
    chosen = {state.num(x) for x in pos}
    for r in rows_of(resp, "last"):
        cfg = state.row("Wish", r.get("id"))
        reward_users = r.get("rewardUser")
        if (cfg and state.num(cfg.get("wishPos")) in chosen
                and isinstance(reward_users, (Mapping, list, tuple)) and reward_users):
            return True
    return False


def options(resp: Mapping[str, Any], info: Mapping[str, Any]) -> tuple[list[dict] | None, str]:
    common = state.row("WishCommon", 1)
    if not common:
        return None, "许愿配置缺失"
    league = state.league(info)
    camp = state.num(league.get("camp")) if isinstance(league, Mapping) else None
    found, camps = [], set()
    for r in rows_of(resp, "cur"):
        cfg = state.row("Wish", r.get("id"))
        if not cfg:
            continue
        pos, utype = state.num(cfg.get("wishPos")), state.num(cfg.get("unionType"))
        camp_pool = pos is not None and utype and utype > 10 and pos // 10 == utype - 10 and pos % 10 in (3, 4)
        if camp_pool:
            camps.add(utype - 10)
        if (camp_pool or (utype == 0 and pos in (3, 4))) and cfg.get("consume_type") == "coin" and cfg.get("consume_subType") == "spirit":
            found.append((r, cfg))
    if camp is None:
        if len(camps) != 1:
            return None, "无法确认本阵营许愿池"
        camp = next(iter(camps))
    if camp <= 0:
        return None, "当前角色没有本阵营许愿池"
    wanted = {(3, 0): "彻悟池", (camp * 10 + 3, camp + 10): "轮回池", (4, 0): "无量池", (camp * 10 + 4, camp + 10): "圆满池"}
    out: dict[tuple, dict] = {}
    for r, cfg in found:
        key = (state.num(cfg.get("wishPos")), state.num(cfg.get("unionType")))
        if key not in wanted:
            continue
        if key in out:
            return None, f"{wanted[key]}出现重复候选项"
        costs = [state.num(x) for x in str(cfg.get("consume_count")).split(",")]
        wid, count_add, cap = state.num(r.get("id")), state.num(cfg.get("countAdd")), state.num(cfg.get("wishCount"))
        max_level = state.num(common.get("maxLevel"))
        base = state.row("Prop", cfg.get("base_subType"))
        if None in costs or not wid or not count_add or not cap or cap <= 0 or not max_level:
            return None, f"{wanted[key]}配置不完整"
        out[key] = {"wish_id": wid, "pos": key[0], "pool": wanted[key], "reward": str(cfg.get("des") or ""),
                    "base_reward": str(base.get("name")) if base else "", "limit": min(cap, max_level), "costs": costs}
    missing = [n for k, n in wanted.items() if k not in out]
    return (None, "服务器许愿池缺少：" + "、".join(missing)) if missing else (list(out.values()), "")


def today(info: Mapping[str, Any], cur_key: str) -> dict[int, int] | None:
    wk = state.weekly(info)
    wish = wk.get("wish", {}) if wk is not None else None
    day = wish.get(cur_key, {}) if isinstance(wish, Mapping) else None
    pos = day.get("pos", {}) if isinstance(day, Mapping) else None
    if not isinstance(pos, Mapping):
        return None
    out = {state.num(k): state.num(v) for k, v in pos.items()}
    return None if None in out or None in out.values() or any(v < 0 for v in out.values()) else out


def count_after(resp: Mapping[str, Any], cur_key: str, pos: int) -> int | None:
    day = (_week_wish(resp) or {}).get(cur_key)
    positions = day.get("pos") if isinstance(day, Mapping) else None
    return state.num(positions.get(str(pos), positions.get(pos))) if isinstance(positions, Mapping) else None


# getWishPoolInfo 含 guard 关键词 wish：需要登记为已审核路由，但它仍是只读请求。
@task(ID, "许愿池", reads={ROUTE_INFO}, writes={ROUTE_WISH, ROUTE_REWARD},
      spends={ROUTE_INFO, ROUTE_WISH, ROUTE_REWARD})
def run(ctx: TaskContext) -> TaskResult:
    try:
        resp = ctx.request(ROUTE_INFO, {})
        info = ctx.refresh()
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"读取或刷新许愿池状态失败，未许愿：{exc}")
    cur_key = str(resp.get("curKey") or "")
    if not cur_key:
        return TaskResult(ID, BLOCKED, "服务器没有返回当前许愿日标识，未许愿")
    note, gains = "", dungeon.Gains()
    ready = prior_reward_ready(resp, info)
    if ready:
        if ctx.stopped or ctx.winding:
            return TaskResult(ID, SKIPPED, "已停止，未领取上期奖励或许愿")
        try:
            claimed = dungeon.Gains()  # 上期奖励单独统计：后面许愿的收获另算，否则结果里同一批奖励会写两遍
            claimed.add(ctx.request(ROUTE_REWARD, {}), ctx.session.drain_pushes())
            resp = ctx.request(ROUTE_INFO, {})
            info = ctx.refresh()  # 领奖可能改变灵魂余额和 everyWeekSetting.wish，后续规划必须用新快照
        except p.AllianceError as exc:
            return TaskResult(ID, BLOCKED, f"上期许愿奖励领取或刷新失败：{exc}，未继续许愿")
        cur_key = str(resp.get("curKey") or "")
        if not cur_key:
            return TaskResult(ID, BLOCKED, "已领取上期奖励，但刷新响应缺少许愿日标识，未许愿")
        note = "；已领取上期奖励" + dungeon.gain_note(claimed)
    opts, why = options(resp, info)
    chosen_before = today(info, cur_key)
    if opts is None or chosen_before is None:
        return TaskResult(ID, BLOCKED, (why or "无法确认今日许愿选择记录") + "，未许愿" + note)
    if len(chosen_before) > (state.num((state.row("WishCommon", 1) or {}).get("wishMax")) or 3):
        return TaskResult(ID, BLOCKED, "已选许愿池数量异常，未许愿" + note)
    by_pos = {o["pos"]: o for o in opts}
    slots = 3 - len(chosen_before)
    chosen = [by_pos[x] for x in chosen_before if x in by_pos]
    for o in sorted(opts, key=lambda o: (priority(o), o["pos"])):
        if slots > 0 and o["pos"] not in chosen_before:
            chosen.append(o)
            slots -= 1
    if not chosen:
        return TaskResult(ID, SKIPPED, "今日三个许愿名额已被其他池占用" + note)
    plan, total = [], 0
    for o in sorted(chosen, key=lambda o: (priority(o), o["pos"])):
        cur = chosen_before.get(o["pos"], 0)
        costs = o["costs"][cur:o["limit"]]
        if cur > o["limit"] or len(costs) != o["limit"] - cur or any(c <= 0 for c in costs):
            return TaskResult(ID, BLOCKED, f"{o['pool']}次数或灵魂消耗表异常，未许愿" + note)
        plan.append((o, cur, costs))
        total += sum(costs)
    balance = state.spirit(info)
    if balance is None:
        return TaskResult(ID, BLOCKED, "没有读到灵魂余额，未许愿" + note)
    summary = "；".join(f"{o['pool']}·{o['reward']}" for o, _, _ in plan)
    if total == 0:
        return TaskResult(ID, SKIPPED, f"今日已许满：{summary}" + note)
    if balance < total:
        return TaskResult(ID, BLOCKED, f"{summary}；需要灵魂 {total:,}，当前 {balance:,.0f}，一次都未许" + note)
    done = spent = 0
    for o, cur, costs in plan:
        for cost in costs:
            if ctx.stopped or ctx.winding:
                return TaskResult(ID, SUBMITTED if done else SKIPPED, f"已停止；许愿 {done} 次，灵魂 {spent:,}" + note)
            resp = ctx.request(ROUTE_WISH, {"wishId": o["wish_id"]})  # 超时由框架按"结果未知"处理
            gains.add(resp, ctx.session.drain_pushes())
            if count_after(resp, cur_key, o["pos"]) != cur + 1:
                return TaskResult(ID, BLOCKED, f"{o['pool']}许愿后服务器次数没有确认从 {cur} 变为 {cur + 1}，已停止（不重发）" + note)
            cur, done, spent = cur + 1, done + 1, spent + cost
    return TaskResult(ID, SUBMITTED, f"{summary}；许愿 {done} 次，灵魂 {spent:,}" + note + dungeon.gain_note(gains))
