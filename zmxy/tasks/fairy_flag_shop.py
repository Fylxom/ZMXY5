"""妖幡卷商店（周常页）：把所选的"周限"妖幡卷买到每周限购 35 件（用仙魄，不用点券/元宝）。

商品只认 ``Shop`` 表里 9 个同时满足"名称含妖幡卷且以(周限)结尾、``fairyMoney`` 标价、``weekPurchaseMaxNum=35``、
没有下架时间"的行（8022–8030）。2026-11 起原来的 9 个"日限"商品（8001 等）已下架（``underTime`` 2026/11/1），不再支持。
领取仙魄挪到了藏宝洞任务里（打完藏宝洞顺带领），这里只买。
界面多选，没选 = 不执行。已购数量按客户端 ShopMallManager 的顺序：周记录 → 日记录 → 永久记录，
都没有该商品按 0（前提是服务器给了 everyWeekSetting）；读不到、或仙魄买不起所选的全部 → 一件都不买。
同一件商品一次买满剩余数量（``{shopId, count}``）；服务器报"购买cd中，请稍后再试"说明没有执行，等 1 秒再试，最多 3 次，
其他失败不重试。请求超时 → ``UncertainOutcome``。
"""

from __future__ import annotations

from .. import api, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, TaskContext, TaskResult, UncertainOutcome, task

ID = "fairy_flag_shop"
R_BUY = api.R_SHOP_BUY
ITEM_IDS = (8022, 8023, 8024, 8025, 8026, 8027, 8028, 8029, 8030)
WEEKLY_LIMIT = 35
GAP, COOLDOWN_RETRIES = 1.0, 3


def good(item_id: int):
    row = state.row("Shop", item_id)
    name = str((row or {}).get("name", ""))
    if (not row or item_id not in ITEM_IDS or "妖幡卷" not in name or not name.endswith("(周限)")
            or row.get("consume_type") != "coin" or row.get("consume_subType") != "fairyMoney"
            or state.num(row.get("weekPurchaseMaxNum")) != WEEKLY_LIMIT or (state.num(row.get("consume_count")) or 0) <= 0
            or str(row.get("underTime", "-1")) != "-1"):
        return None
    return row


def catalog() -> list[tuple[int, str]]:
    return [(i, str(good(i)["name"]).replace("(周限)", "")) for i in ITEM_IDS if good(i)]


def bought(info, item_id: int) -> int | None:
    """本周已购数量；everyWeekSetting 缺失或记录损坏 → None。"""
    if state.weekly(info) is None:
        return None
    week = state.weekly(info).get("shopBuyCount") or {}
    day = (state.daily(info) or {}).get("shopBuyCount") or {}
    forever = info.get("shop") or {}
    for scope in (week, day, forever):
        if not isinstance(scope, dict):
            return None
        record = scope.get(str(item_id)) or scope.get(item_id)
        if not record:
            continue
        n = state.num(record.get("buyCount")) if isinstance(record, dict) else None
        return n if n is not None and n >= 0 else None
    return 0


def cooldown(exc: Exception) -> bool:
    text = str(exc).casefold().replace(" ", "")
    return "购买cd中" in text and "请稍后再试" in text


@task(ID, "妖幡卷", writes={R_BUY}, spends={R_BUY}, group="周常",
      choices=Choices(catalog, "商品", option="shop_ids"))
def run(ctx: TaskContext) -> TaskResult:
    chosen = []
    for raw in ctx.options.get("shop_ids") or []:
        item_id = state.num(raw)
        if item_id is None or good(item_id) is None:
            return TaskResult(ID, BLOCKED, "选择中有已下架或未审核的商品（妖幡卷已改为周限），请重新选择；未购买")
        if item_id not in chosen:
            chosen.append(item_id)
    if not chosen:
        return TaskResult(ID, SKIPPED, "未选择妖幡卷商品")
    return buy(ctx, chosen)


def buy(ctx: TaskContext, chosen: list[int]) -> TaskResult:
    if ctx.stopped or ctx.winding:
        return TaskResult(ID, SKIPPED, "用户停止，未购买")
    try:
        ctx.refresh()
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"刷新购买前状态失败，未购买：{exc}")
    if ctx.stopped or ctx.winding:
        return TaskResult(ID, SKIPPED, "用户停止，未购买")
    money = state.num(state.find(ctx.info, "fairyMoney", depth=8))
    if money is None or money < 0:
        return TaskResult(ID, BLOCKED, "服务器未返回仙魄余额，未发送购买请求")
    plans, total, full = [], 0, []
    for item_id in sorted(chosen, key=ITEM_IDS.index):
        used = bought(ctx.info, item_id)
        row = good(item_id)
        if used is None:
            return TaskResult(ID, BLOCKED, f"服务器未返回 {row['name']} 的有效已购数量，未购买")
        if used >= WEEKLY_LIMIT:
            full.append(row["name"])
            continue
        plans.append((item_id, row, WEEKLY_LIMIT - used))
        total += (WEEKLY_LIMIT - used) * state.num(row["consume_count"])
    if not plans:
        return TaskResult(ID, SKIPPED, "所选商品本周均已达到限购 35 件")
    if money < total:
        return TaskResult(ID, BLOCKED, f"仙魄不足（当前 {money}，买满需 {total}），未发送任何购买请求")
    done: list[str] = []
    first = True

    def confirmed() -> str:
        return "、".join(done) or "0 件"

    def stopped() -> TaskResult:
        return TaskResult(ID, SUBMITTED if done else SKIPPED, f"用户停止；已确认购买 {confirmed()}")

    try:
        for item_id, row, count in plans:
            for attempt in range(COOLDOWN_RETRIES + 1):
                if ctx.stopped or ctx.winding:
                    return stopped()
                if not first and not ctx.sleep(GAP):
                    if ctx.stopped or ctx.winding:
                        return stopped()
                    reason = ctx.session.idle_error
                    return TaskResult(ID, BLOCKED, "购买间隔等待时连接中断" + (f"（{reason}）" if reason else "") + f"；已确认购买 {confirmed()}")
                first = False
                try:
                    api.buy_shop(ctx.request, item_id, count)
                except p.AllianceError as exc:
                    if cooldown(exc) and attempt < COOLDOWN_RETRIES:
                        continue
                    return TaskResult(ID, BLOCKED, f"购买 {row['name']}×{count} 失败：{exc}；此前已确认 {confirmed()}；本次不再重试")
                done.append(f"{row['name']}×{count}")
                break
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"此前已确认购买 {confirmed()}；{exc}") from exc
    return TaskResult(ID, SUBMITTED, f"已确认购买 {'、'.join(done)}；共消耗仙魄 {total}")
