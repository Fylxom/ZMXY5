"""妖王帖：只用普通招募（id=1）的免费次数，受每日上限与背包空位限制；不用招募券。"""

from __future__ import annotations

from .. import dungeon, protocol as p, state
from ..core import BAG_FULL_HINT, BLOCKED, PAUSED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "monster_card"
ROUTE = "scene.monsterCardHandler.lottery"
MAX_RUNS = 50


def _empty_slots(info) -> int | None:
    """普通物品栏空位：isConsume!=1 的已知物品按 id 合并占格。"""
    bag = info.get("bag") if isinstance(info, dict) else None
    items = bag.get("item") if isinstance(bag, dict) else None
    size = state.num(bag.get("itemBagMax")) if isinstance(bag, dict) else None
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list) or size is None or size < 0:
        return None
    ids = set()
    for it in items:
        iid = state.num(it.get("id", it.get("itemId"))) if isinstance(it, dict) else None
        prop = state.row("Prop", iid) if iid else None
        if not prop:
            return None
        is_consume = state.num(prop.get("isConsume"))
        if is_consume is None:
            return None
        if is_consume != 1:
            ids.add(iid)
    return max(0, size - len(ids))


@task(ID, "妖王帖", writes={ROUTE}, spends={ROUTE})
def run(ctx: TaskContext) -> TaskResult:
    row = state.row("MonsterCardLottery", 1)
    if not row or row.get("consume_type") != "item":
        return TaskResult(ID, BLOCKED, "招募配置缺失或消耗类型未确认，未发送请求")
    try:
        info = ctx.refresh()
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"刷新妖王帖状态失败：{exc}，未发送请求")
    free = state.by_vip(row.get("vipFreeNum"), state.vip_level(info))
    cap, per_bag = state.num(row.get("monsterCardLotteryDayNum")), state.num(row.get("bagLimit"))
    daily = state.daily(info)
    if free is None or not cap or not per_bag or per_bag <= 0 or daily is None:
        return TaskResult(ID, BLOCKED, "无法确认免费招募次数或每日上限，未发送请求")
    raw = daily.get("monsterCardLottery")
    if raw is not None and not isinstance(raw, dict):
        return TaskResult(ID, BLOCKED, "妖王帖每日计数格式异常，未发送请求")
    st = raw if isinstance(raw, dict) else {}  # 客户端默认空对象，空对象里的两个计数默认 0
    free_used, day_used = state.num(st.get("freeNum_1", 0)), state.num(st.get("limit_1", 0))
    if free_used is None or free_used < 0 or day_used is None or day_used < 0:
        return TaskResult(ID, BLOCKED, "无法确认妖王帖已用次数，未发送请求")
    free_left = max(0, free - free_used)
    day_left = max(0, cap - day_used)
    slots = _empty_slots(info)
    if slots is None:
        return TaskResult(ID, BLOCKED, "背包数据不完整，无法确认空位，未发送请求")
    runs = min(MAX_RUNS, free_left, day_left, slots // per_bag)
    if runs <= 0:
        if slots < per_bag and free_left > 0 and day_left > 0:  # 还有次数却因背包没空位招不了
            return TaskResult(ID, PAUSED, BAG_FULL_HINT + "（招募需要空位，未发送请求）")
        why = "背包空间不足" if slots < per_bag else "今日次数已用完" if day_left <= 0 else "免费招募次数已用完"
        return TaskResult(ID, SKIPPED, f"{why}，未发送请求（不使用招募券）")
    done, gains = 0, dungeon.Gains()
    for _ in range(runs):
        if ctx.stopped:
            break
        try:
            gains.add(ctx.request(ROUTE, {"id": 1}), ctx.session.drain_pushes())
        except p.AllianceError as exc:
            if not done:
                return TaskResult(ID, BLOCKED, f"招募失败：{exc}")
            return TaskResult(ID, SUBMITTED, f"招募 {done}/{runs} 次，之后停止：{exc}{dungeon.gain_note(gains)}")
        done += 1
    if not done and ctx.stopped:
        return TaskResult(ID, SKIPPED, "用户停止，未开始招募")
    detail = f"招募 {done}/{runs} 次"
    if ctx.stopped and done < runs:
        detail += "；用户停止"
    return TaskResult(ID, SUBMITTED, f"{detail}{dungeon.gain_note(gains)}")
