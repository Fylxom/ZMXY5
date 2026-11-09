"""神秘商店：每次买当前货架里阶段最高、且灵魂标价的商品，直到 VIP 次数用完。"""

from __future__ import annotations

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "mystery_shop"
ROUTE_GET = "scene.shopHandler.getMysteryShop"
ROUTE_BUY = "scene.shopHandler.buyMysteryShop"
MAX_RUNS = 50
STAGE = {101: 1, 102: 2, 103: 3, 105: 4, 106: 5}  # 服务器的摊位号 -> 1~5


def offers(resp) -> list[tuple[int, dict]]:
    """[(摊位 1~5, 表行)]：只认 mysteryShop.stall 里"阶段→商品 id"，且与 MysteryShop 表一致的。"""
    stall = (state.section(resp, "mysteryShop") or {}).get("stall")
    out = []
    for key, item in (stall.items() if isinstance(stall, dict) else ()):
        n = state.num(key)
        stage = STAGE.get(n, n)
        row = state.row("MysteryShop", item)
        if row and stage and 1 <= stage <= 5 and state.num(row.get("stallNum")) == stage:
            out.append((stage, row))
    return out


def remaining(info, resp) -> int | None:
    common = state.row("Common", 1)
    limit = state.by_vip(common.get("mysteryShopNum"), state.vip_level(info)) if common else None
    shop = state.section(resp, "mysteryShop")
    used = state.num(shop.get("count")) if shop else None
    return None if limit is None or used is None or used < 0 or used > limit else limit - used


@task(ID, "神秘商店", reads={ROUTE_GET}, writes={ROUTE_BUY}, spends={ROUTE_BUY})
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未读取或购买神秘商店商品")
    try:
        resp = ctx.request(ROUTE_GET)
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"读取神秘商店失败，未购买：{exc}")
    left = remaining(ctx.info, resp)
    if left is None:
        return TaskResult(ID, BLOCKED, "无法确认神秘商店今日次数，未购买")
    runs, done, names = min(left, MAX_RUNS), 0, []
    if runs <= 0:
        return TaskResult(ID, SKIPPED, "今日神秘商店次数已用完")
    spirit = state.spirit(ctx.info)
    if spirit is None:
        return TaskResult(ID, BLOCKED, "无法确认灵魂余额，未购买")
    gains = dungeon.Gains()

    def end(status_if_done: str, why: str) -> TaskResult:
        return TaskResult(ID, status_if_done if done else BLOCKED, f"已购买 {done} 次；{why}{dungeon.gain_note(gains)}")

    for i in range(runs):
        if ctx.stopped:
            break
        if i:  # 每次购买后货架会变，重新读取并重新确认次数
            try:
                resp = ctx.request(ROUTE_GET)
            except p.AllianceError as exc:
                return end(BLOCKED, f"刷新货架失败：{exc}")
            fresh_left = remaining(ctx.info, resp)
            if fresh_left is None:
                return end(BLOCKED, "无法确认刷新后的今日次数，停止购买")
            if fresh_left <= 0:
                break
        shelf = offers(resp)
        if not shelf:
            return end(SUBMITTED, "货架没有可核对的商品")
        stage, row = max(shelf, key=lambda o: o[0])
        cost = state.num(row.get("consume_count")) or 0
        if row.get("consume_type") != "coin" or row.get("consume_subType") != "spirit" or cost <= 0:
            return end(SUBMITTED, f"{row.get('name')} 不是灵魂标价，未购买")
        if spirit < cost:
            return TaskResult(ID, SUBMITTED if done else SKIPPED, f"已购买 {done} 次；灵魂不足{dungeon.gain_note(gains)}")
        if ctx.stopped:
            break
        try:
            r = ctx.request(ROUTE_BUY, {"stallNum": stage})
        except p.AllianceError as exc:
            return end(BLOCKED, f"第 {i + 1} 次失败：{exc}")
        gains.add(r, ctx.session.drain_pushes())
        left_spirit = state.spirit(r)
        spirit = left_spirit if left_spirit is not None else spirit - cost
        resp, done = r, done + 1
        names.append(f"阶段{stage}·{row.get('name')}")
    detail = f"购买 {done} 次：" + "、".join(names) + dungeon.gain_note(gains)
    if ctx.stopped:
        detail += "；用户停止"
    return TaskResult(ID, SUBMITTED if done else SKIPPED, detail)
