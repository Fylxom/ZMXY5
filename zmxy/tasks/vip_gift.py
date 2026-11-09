"""VIP 礼包：按 VIP 配置和已领取记录领取当前档位的礼包。"""

from __future__ import annotations

from typing import Any, Mapping

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "vip_gift"
ROUTE_VIP_GIFT = "scene.giftHandler.getGift"

# 进角响应里"是否已领取"的字段名在各客户端版本不一致；vipGiftBagId 是奖励定义 id，不是领取标记
_CLAIM_KEYS = (
    "vipGiftClaimed", "vip_gift_claimed", "vipGiftReceived", "vip_gift_received",
    "giftClaimed", "gift_claimed", "giftReceived", "gift_received",
    "isVipGiftClaimed", "isVipGiftReceived",
)
_GENERIC_KEYS = (
    "isClaimed", "isReceived", "hasClaimed", "hasReceived", "claimed", "received",
    "giftState", "gift_state", "receiveState", "receive_state", "status",
)
_CAN_KEYS = ("canReceive", "can_receive", "canClaim", "can_claim", "canGet", "can_get")
_TRUE = {"1", "true", "yes", "on", "claimed", "received", "已领取", "已完成"}
_FALSE = {"0", "false", "no", "off", "unclaimed", "未领取", "未完成"}
_DONE_WORDS = ("已领取", "已完成", "领取过")
_LIMIT_WORDS = ("次数", "上限", "用完", "材料", "不足", "冷却") + _DONE_WORDS


def _flag(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v) if v in (0, 1) else None
    s = str(v).strip().lower()
    return True if s in _TRUE else False if s in _FALSE else None


def claim_state(value: Any, depth: int = 4, gift_ctx: bool = False) -> bool | None:
    """True=已领取 / False=未领取 / None=服务器没说。"""
    if depth < 0:
        return None
    if isinstance(value, Mapping):
        for k in _CLAIM_KEYS:
            if k in value and (s := _flag(value[k])) is not None:
                return s
        if gift_ctx:  # 泛用字段只在 vip/gift 对象内可信，避免误读账号 status
            for k in _GENERIC_KEYS:
                if k in value and (s := _flag(value[k])) is not None:
                    return s
            for k in _CAN_KEYS:
                if k in value and (s := _flag(value[k])) is not None:
                    return not s
        for k in ("vip", "vipGift", "vip_gift", "gift", "gifts"):
            if k in value and (s := claim_state(value[k], depth - 1, True)) is not None:
                return s
        for child in value.values():
            if (s := claim_state(child, depth - 1, gift_ctx)) is not None:
                return s
    elif isinstance(value, list):
        for child in value:
            if (s := claim_state(child, depth - 1, gift_ctx)) is not None:
                return s
    return None


def _claimed_gift_ids(info: Mapping[str, Any]) -> set[str] | None:
    """与客户端 GiftBagManager 一致，从三处 gift 映射收集已领取的礼包 id。"""
    claimed: set[str] = set()
    owners = [info]
    for key in ("everyWeekSetting", "everyDaySetting"):
        owner = info.get(key)
        if owner is not None:
            if not isinstance(owner, Mapping):
                return None
            owners.append(owner)
    for owner in owners:
        if "gift" not in owner:
            continue
        gifts = owner["gift"]
        if not isinstance(gifts, Mapping):
            return None
        claimed.update(str(gift_id) for gift_id in gifts)
    return claimed


def choose_gift_id(info: Mapping[str, Any], configured: Any = None) -> tuple[int | None, str | None]:
    """按 VIP 面板选择：先领当前档 vipGiftBagId，已领后才领 giftBagId。"""
    if configured not in (None, ""):
        gift_id = state.num(configured)
        return (gift_id, None) if gift_id is not None else (None, "VIP 礼包 ID 设置无效")

    level = state.vip_level(info)
    if level is None:
        return None, "读不到 VIP 等级，无法确定礼包档位"
    if level <= 0:
        return 0, None
    row = state.row("VIP", level)
    if row is None:
        return None, f"VIP 配置缺少等级 {level}"
    level_gift = state.num(row.get("vipGiftBagId"))
    daily_gift = state.num(row.get("giftBagId"))
    claimed = _claimed_gift_ids(info)
    if level_gift is None or daily_gift is None or claimed is None:
        return None, "VIP 礼包配置或已领取记录不完整"
    if level_gift > 0 and str(level_gift) not in claimed:
        return level_gift, None
    return (daily_gift if daily_gift > 0 and str(daily_gift) not in claimed else 0), None


@task(ID, "VIP 礼包", writes={ROUTE_VIP_GIFT}, account_wide=True)
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未领取 VIP 礼包")
    gift_id, problem = choose_gift_id(ctx.info, ctx.options.get("vip_gift_id"))
    if problem:
        return TaskResult(ID, BLOCKED, f"{problem}，未领取")
    if gift_id <= 0:
        return TaskResult(ID, SKIPPED, "当前角色没有可领取的 VIP 礼包")
    try:
        resp = ctx.request(ROUTE_VIP_GIFT, {"giftId": gift_id})
    except p.AllianceError as exc:
        msg = str(exc)
        if any(w in msg for w in _DONE_WORDS):
            return TaskResult(ID, SKIPPED, f"今日已领取（giftId={gift_id}）")
        status = SKIPPED if any(w in msg for w in _LIMIT_WORDS) else BLOCKED
        return TaskResult(ID, status, f"未领取：{msg}")
    if claim_state(resp) is True:
        return TaskResult(ID, SKIPPED, f"今日已领取（giftId={gift_id}）")
    gains = dungeon.Gains()
    gains.add(resp, ctx.session.drain_pushes())
    return TaskResult(ID, SUBMITTED, f"领取成功（giftId={gift_id}）{dungeon.gain_note(gains)}")
