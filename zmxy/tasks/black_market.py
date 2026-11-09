"""黑市商店：只买货架第一件灵魂标价的商品，并用 VIP 免费刷新次数逐次刷新再买。"""

from __future__ import annotations

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "black_market"
ROUTE_GET = "scene.shopHandler.getBlackMarket"
ROUTE_BUY = "scene.shopHandler.buyBlackMarket"
ROUTE_REFRESH = "scene.shopHandler.freeForceRefreshBlackMarket"  # 付费刷新 forceRefreshBlackMarket 不登记
ROUTE_GET_ACTIVITY = ROUTE_GET + "_activity"
ROUTE_BUY_ACTIVITY = ROUTE_BUY + "_activity"
ROUTE_REFRESH_ACTIVITY = ROUTE_REFRESH + "_activity"


def first_offer(resp, table_name: str) -> tuple[dict, int] | None:
    """货架第一件及已售数量（按 stallNum 稳定排序）；库存不完整时返回 None。"""
    stock = (state.section(resp, "blackMarket") or {}).get("data")
    if not isinstance(stock, dict):
        return None
    rows = [r for k in stock if (r := state.row(table_name, k)) and state.num(r.get("id")) == state.num(k)]
    rows.sort(key=lambda r: state.num(r.get("stallNum")) or 0)
    if not rows:
        return None
    offer = rows[0]
    count = state.num(stock.get(str(offer["id"]), stock.get(offer["id"])))
    return (offer, count) if count is not None and count >= 0 else None


def free_refreshes(info, resp, activity: bool) -> int | None:
    vip = state.row("VIP", state.vip_level(info))
    limit = state.num(vip.get("freeblackMarketNum")) if vip else None
    key = "freeblackMarketNum_activity" if activity else "freeblackMarketNum"
    response_daily = state.daily(resp)
    if response_daily is not None and key in response_daily:
        used = state.used(resp, key)
    else:
        # 响应只带了一部分时客户端（VipModelManager.setVipCount）保留旧值，这里同样不当成 0
        used = state.used(info, key)
    return None if limit is None or used is None else max(0, limit - used)


@task(ID, "黑市商店", reads={ROUTE_GET, ROUTE_GET_ACTIVITY},
      writes={ROUTE_BUY, ROUTE_BUY_ACTIVITY, ROUTE_REFRESH, ROUTE_REFRESH_ACTIVITY},
      spends={ROUTE_BUY, ROUTE_BUY_ACTIVITY, ROUTE_REFRESH, ROUTE_REFRESH_ACTIVITY})
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未购买黑市商品")
    common = state.row("Common", 1) or {}
    activity_id = state.num(common.get("blackMarketActivityId"))
    activity = bool(activity_id and ctx.now is not None and state.activity_open(activity_id, ctx.now))
    suffix = "_activity" if activity else ""
    market_table = "BlackMarket_activity" if activity else "BlackMarket"
    route_get = ROUTE_GET + suffix
    route_buy = ROUTE_BUY + suffix
    route_refresh = ROUTE_REFRESH + suffix
    resp = ctx.request(route_get)
    first = first_offer(resp, market_table)
    if first is None:
        return TaskResult(ID, BLOCKED, "黑市没有返回可核对的货架")
    offer, stock_count = first
    refreshes = free_refreshes(ctx.info, resp, activity)
    balance = state.spirit(ctx.info)
    bought, refreshed, notes, has_issue = 0, 0, [], False
    gains = dungeon.Gains()

    def buy(row, sold_count: int, label: str) -> bool:
        """买一件灵魂标价商品；返回是否应继续刷新。"""
        nonlocal balance, bought, has_issue
        name, cost = row.get("name") or row.get("id"), state.num(row.get("consume_count"))
        if sold_count != 0:
            notes.append(f"{label}{name} 已售出，未购买")
            return True
        if row.get("consume_type") != "coin" or row.get("consume_subType") != "spirit" or not cost or cost <= 0:
            notes.append(f"{label}{name} 不是灵魂标价，未购买")
            return True
        if balance is None or balance < cost:
            notes.append(f"{label}灵魂不足，未购买{name}")
            return False
        if ctx.stopped:
            notes.append(f"{label}收到停止请求，未购买{name}")
            return False
        try:
            r = ctx.request(route_buy, {"shopId": state.num(row["id"])})
        except p.AllianceError as exc:
            has_issue = True
            notes.append(f"{label}购买{name}失败：{exc}")
            return True
        gains.add(r, ctx.session.drain_pushes())
        left = state.spirit(r)
        balance = left if left is not None and left < balance else balance - cost
        bought += 1
        notes.append(f"{label}已购买{name}（灵魂 {cost}）")
        return True

    go = buy(offer, stock_count, "当前货架：")
    if refreshes is None:
        has_issue = True
        notes.append("无法确认免费刷新次数，未刷新")
    for _ in range(refreshes or 0):
        if not go or ctx.stopped:
            break
        try:
            r = ctx.request(route_refresh)
        except p.AllianceError as exc:
            has_issue = True
            notes.append(f"免费刷新停止：{exc}")
            break
        refreshed += 1
        first = first_offer(r, market_table)
        if first is None:
            has_issue = True
            notes.append("刷新响应没有货架，停止")
            break
        offer, stock_count = first
        go = buy(offer, stock_count, f"第{refreshed}次刷新：")
    status = BLOCKED if has_issue else SUBMITTED if bought or refreshed else (BLOCKED if "不足" in "".join(notes) else SKIPPED)
    return TaskResult(ID, status, f"购买 {bought} 件，免费刷新 {refreshed} 次；" + "；".join(notes) + dungeon.gain_note(gains))
