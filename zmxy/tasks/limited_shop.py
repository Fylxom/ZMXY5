"""限时抢购（周常）：领本周养成福利，再按 GlobalBuy 周更表买正在上架的灵魂商品。

只买灵魂标价、无角色门槛的商品；数量取库存、账号限购、灵魂余额的最小值。
部分商品读不准时只跳过那一件，最后整体标 blocked，不把没确认的当成买完。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .. import data, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

ID = "limited_time_shop"
R_INFO = "scene.shopHandler.getLimitShopInfo"
R_BUY = "scene.shopHandler.buyGlobalLimitShop"
R_CLAIM_GIFT = "scene.activityHandler.getActFreeGift"


def _date(value: Any) -> datetime | None:
    for fmt in ("%Y/%m/%d-%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=state._CN)
        except ValueError:
            pass
    return None


def _active(row: Mapping[str, Any], now: int) -> bool | None:
    preview, shelf, under = (_date(row.get(k)) for k in ("previewTime", "shelfTime", "underTime"))
    if preview is None or shelf is None or under is None:
        return None
    current = datetime.fromtimestamp(now, state._CN)
    return preview <= current and shelf <= current <= under


def _label(row: Mapping[str, Any]) -> str:
    return str(row.get("name") or row.get("id") or "未知商品")


def _offers(now: int | None, notes: list[str]) -> tuple[list[Mapping[str, Any]], bool]:
    """(当前可买的灵魂商品, 是否有读不准的)；跳过原因写进 notes。"""
    rows = [r for r in state.table_rows("GlobalBuy") if r.get("consume_type") == "coin" and r.get("consume_subType") == "spirit"]
    if rows and now is None:
        notes.append("缺少服务器时间，无法确认限时商品是否上架")
        return [], True
    selected, uncertain = [], False
    for row in rows:
        active, name = _active(row, now), _label(row)
        if active is False:
            continue
        roles = state.num(row.get("roleConditions", 0))  # 客户端 ShopVo 里是 int，缺省为 0
        cost = state.num(row.get("consume_count"))
        if active is None:
            why = "上架时间字段无效"
        elif roles is None:
            why = "角色条件字段缺失或无效"
        elif roles:
            notes.append(f"{name}有角色条件，跳过")
            continue
        elif not state.num(row.get("id")) or (cost or 0) <= 0:
            why = "的商品编号或灵魂价格无效"
        else:
            selected.append(row)
            continue
        notes.append(f"{name}{why}，跳过")
        uncertain = True

    def sort_key(row: Mapping[str, Any]) -> tuple[int, int]:
        order = state.num(row.get("order"))
        return 2**31 if order is None else order, state.num(row.get("id"))

    return sorted(selected, key=sort_key), uncertain


def _limit(row: Mapping[str, Any]) -> int | None:
    """客户端按 周 → 永久 → 日 取每账号限购；都没写时只买一件。"""
    for key in ("weekPurchaseMaxNum", "purchaseMaxNum", "dayPurchaseMaxNum"):
        if row.get(key) in (None, ""):
            continue
        value = state.num(row[key])
        if value is None or value < 0:
            return None
        if value > 0:
            return value
    return 1


def _used(info: Mapping[str, Any], shop_id: int) -> int | None:
    """客户端 ShopMallManager 的周 / 日 / 永久购买计数；三处都没有该商品才是 0。"""
    week, day = state.weekly(info) or {}, state.daily(info) or {}
    for source in (week.get("shopBuyCount"), day.get("shopBuyCount"), info.get("shop")):
        if source is None:
            continue
        if not isinstance(source, Mapping):
            return None
        entry = source.get(str(shop_id), source.get(shop_id))
        if entry is None:
            continue
        count = state.num(entry.get("buyCount")) if isinstance(entry, Mapping) else None
        return None if count is None or count < 0 else count
    return 0


def _stock(response: Mapping[str, Any], shop_id: int) -> int | None:
    stocks = response["data"]
    count = state.num(stocks.get(str(shop_id), stocks.get(shop_id)))
    return None if count is None or count < 0 else count


def _welfare_id() -> int | None:
    gift_id = state.num((state.row("SpeedDevelop", 1) or {}).get("welfareId"))
    return gift_id if gift_id and gift_id > 0 else None


def _gift_claimed(act_free_gifts: Any, gift_id: int) -> bool | None:
    """客户端 ActivityManager.isGetFreeGiftSD：SpeedDevelop[welfareId] 为真即已领。"""
    if not isinstance(act_free_gifts, Mapping):
        return None
    developed = act_free_gifts.get("SpeedDevelop")
    if developed in (None, ""):
        return False
    if not isinstance(developed, Mapping):
        return None
    if str(gift_id) not in developed and gift_id not in developed:
        return False
    value = developed.get(str(gift_id), developed.get(gift_id))
    if isinstance(value, bool):
        return value
    number = state.num(value)
    return None if number is None or number < 0 else number > 0


def _claim_gift(ctx: TaskContext, notes: list[str]) -> tuple[bool, bool]:
    """(本次领到了, 领取受阻)。"""
    try:
        gift_id = _welfare_id()
    except data.DataError as exc:
        notes.append(f"读取 SpeedDevelop 养成福利配置失败：{exc}")
        return False, True
    if gift_id is None:
        notes.append("SpeedDevelop 养成福利编号缺失或无效，未领取")
        return False, True
    claimed = _gift_claimed(ctx.act_free_gifts, gift_id)
    if claimed is None:
        notes.append("无法确认养成福利领取状态，未领取")
        return False, True
    if claimed:
        notes.append("养成福利本周已领取")
        return False, False
    try:
        ctx.request(R_CLAIM_GIFT, {"giftId": gift_id, "giftKey": "SpeedDevelop"})
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"养成福利领取结果未知；请登录游戏核对后再运行。{exc}") from exc
    except p.AllianceError as exc:
        notes.append(f"养成福利领取失败：{exc}")
        return False, True
    notes.append("已领取养成福利")
    return True, False


def _purchase_count(row: Mapping[str, Any], live: Mapping[str, Any], info: Mapping[str, Any],
                    balance: float, notes: list[str]) -> tuple[int, bool]:
    """(这件能买几个, 是否读不准)；买不了的原因写进 notes。"""
    shop_id, name, cost = state.num(row.get("id")), _label(row), state.num(row.get("consume_count"))
    stock, used, maximum = _stock(live, shop_id), _used(info, shop_id), _limit(row)
    if stock is None:
        notes.append(f"{name}库存数量缺失或无效，跳过")
        return 0, True
    if stock <= 0:
        notes.append(f"{name}已售罄")
        return 0, False
    if used is None or maximum is None:
        notes.append(f"{name}的限购状态无法确认，跳过")
        return 0, True
    if maximum <= used:
        notes.append(f"{name}已达到账号限购")
        return 0, False
    count = min(maximum - used, stock, int(balance // cost))
    if count <= 0:
        notes.append(f"灵魂不足，未购买{name}（每件 {cost}）")
    return max(0, count), False


@task(ID, "限时抢购", reads={R_INFO}, writes={R_BUY, R_CLAIM_GIFT}, spends={R_BUY}, group="周常")
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已被用户停止，未领取或购买")
    notes: list[str] = []
    gift_got, unsure = _claim_gift(ctx, notes)

    def blocked(why: str) -> TaskResult:
        return TaskResult(ID, BLOCKED, "；".join((*notes, why)))

    try:
        offers, bad_rows = _offers(ctx.now, notes)
    except data.DataError as exc:
        return blocked(f"读取 GlobalBuy 周更数据失败：{exc}")
    unsure = unsure or bad_rows
    if not offers:
        notes.append("当前没有符合条件的灵魂限时商品")
        return TaskResult(ID, BLOCKED if unsure else SUBMITTED if gift_got else SKIPPED, "；".join(notes))
    try:
        live = ctx.request(R_INFO, {"shopIds": [state.num(row.get("id")) for row in offers]})
    except p.AllianceError as exc:
        return blocked(f"读取限时抢购库存失败，未购买：{exc}")
    if not isinstance(live.get("data"), Mapping):
        return blocked("服务器未返回限时抢购库存，未购买")
    balance = state.spirit(ctx.info)
    if balance is None:
        return blocked("无法确认当前灵魂数量，未购买")

    bought: list[str] = []
    tail, partial = "", False
    for index, row in enumerate(offers):
        if ctx.stopped:
            tail = "；已被用户停止，未处理 " + "、".join(map(_label, offers[index:]))
            partial = bool(bought or gift_got)
            break
        count, shaky = _purchase_count(row, live, ctx.info, balance, notes)
        unsure = unsure or shaky
        if not count:
            continue
        shop_id, name, cost = state.num(row.get("id")), _label(row), state.num(row.get("consume_count"))
        try:
            response = ctx.request(R_BUY, {"shopId": shop_id, "count": count})
        except UncertainOutcome as exc:
            confirmed = "、".join((["领取养成福利"] if gift_got else []) + bought) or "无"
            raise UncertainOutcome(f"限时抢购：此前已确认完成 {confirmed}；{exc}") from exc
        except p.AllianceError as exc:
            notes.append(f"{name}购买失败：{exc}；停止后续购买")
            rest = offers[index + 1:]
            tail = "；后续商品未处理 " + "、".join(map(_label, rest)) if rest else ""
            partial = True
            break
        balance -= cost * count
        after = state.spirit(response)
        balance = balance if after is None else min(balance, after)
        bought.append(f"{name}×{count}（{cost * count} 灵魂）")

    detail = "；".join((f"已购买 {len(bought)} 种：" + ("、".join(bought) or "无"), *notes)) + tail
    return TaskResult(ID, BLOCKED if unsure or partial else SUBMITTED if bought or gift_got else SKIPPED, detail)
