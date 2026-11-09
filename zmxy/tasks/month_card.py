"""超值月卡：每日领取月卡奖励（``getMonthCardReward``，请求体为空，只领取不花费）。

月卡数据在进角响应 ``info.monthCard``（卡 id → ``expiredTime``/``history``）。没有未过期的月卡就不发请求；
每张有效卡的 ``history`` 里已有今天（客户端的键格式 ``年/月/日``，不补零）就不再发请求。
服务器时间、月卡有效期或领取记录格式无法确认时停止；省略 history 按客户端视为尚无记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "month_card"
ROUTE = "scene.activityHandler.getMonthCardReward"
_DONE_WORDS = ("已领取", "已经领取", "领取过", "今日已")


def active_cards(info: Mapping[str, Any], now: int) -> list[Mapping[str, Any]] | None:
    """未过期的月卡；进角信息里没有月卡数据 → None。"""
    cards = state.section(info, "monthCard")
    if cards is None:
        return None
    found = []
    for card in cards.values():
        if not isinstance(card, Mapping):
            return None
        expired = state.seconds(card.get("expiredTime"))
        if expired is None or expired < 0:
            return None
        if expired > now:
            if not isinstance(card.get("history", {}), Mapping):
                return None
            found.append(card)
    return found


def today_key(now: int) -> str:
    d = datetime.fromtimestamp(now, state._CN)
    return f"{d.year}/{d.month}/{d.day}"


def all_claimed(cards: list[Mapping[str, Any]], now: int) -> bool:
    key = today_key(now)
    return all(isinstance(c.get("history"), Mapping) and key in c["history"] for c in cards)


@task(ID, "超值月卡", writes={ROUTE}, account_wide=True)
def run(ctx: TaskContext) -> TaskResult:
    now = ctx.now
    if now is None:
        return TaskResult(ID, BLOCKED, "缺少服务器时间，无法确认月卡有效期和今日领取记录，未领取")
    cards = active_cards(ctx.info, now)
    if cards is None:
        return TaskResult(ID, BLOCKED, "月卡有效期或领取记录不完整，未领取")
    if not cards:
        return TaskResult(ID, SKIPPED, "没有有效的月卡")
    if all_claimed(cards, now):
        return TaskResult(ID, SKIPPED, "今日已领取")
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未领取月卡奖励")
    try:
        resp = ctx.request(ROUTE, {})
    except p.AllianceError as exc:
        msg = str(exc)
        if any(w in msg for w in _DONE_WORDS):
            return TaskResult(ID, SKIPPED, "今日已领取")
        return TaskResult(ID, BLOCKED, f"未领取：{msg}")
    gains = dungeon.Gains()
    gains.add(resp, ctx.session.drain_pushes())
    return TaskResult(ID, SUBMITTED, f"已领取月卡奖励{dungeon.gain_note(gains)}")
