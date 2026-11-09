"""联盟仓库：每日半价（dayBuy=2;0.5）买 2 个所选材料；仓库库存不足 2 个时，可先从同账号勾选的存档出售补货。

界面“选材料”：材料方块（只能选一种；每天半价买 2 个）+ 缺货时用来出售补货的存档（可多选，可不选）。
同一账号同一时间只能有一个存档在线，所以出售要临时登录别的存档（``Session.other_save``），
结束后重新登录回当前存档。出售前先核对：该存档属于同一联盟、持有足够的该材料；
出售后必须看到仓库库存确实增加才继续，否则停止，不自动重试。
"""

from __future__ import annotations

from contextlib import contextmanager

from .. import protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, UncertainOutcome, task

ID = "union_warehouse"
ROUTE_GET = "league.leagueHandler.getLeagueWarehouse"
ROUTE_BUY = "scene.leagueHandler.buyItem"
ROUTE_SELL = "scene.leagueHandler.sellItem"
DAILY_LIMIT = 2                                  # 每天半价名额（个）
CURRENCIES = frozenset({101026007, 101026013})   # 只允许这两种非付费货币


def materials() -> list[tuple[int, str]]:
    out = []
    for row in state.table_rows("UnionWarehouse"):
        iid, cur, cost = state.num(row.get("id")), state.num(row.get("itemId")), state.num(row.get("buy_count"))
        if iid and row.get("dayBuy") == "2;0.5" and cur in CURRENCIES and cost and cost > 0 and cost % 2 == 0:
            out.append((iid, str(row.get("name"))))
    return sorted(out)


def _name(item_id: int) -> str:
    row = state.row("Prop", item_id)
    return str(row.get("name")) if row else f"物品{item_id}"


class _Stop(Exception):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def stock_of(warehouse, item_id: int) -> int | None:
    """库存；表里没有该键 = 0；格式坏 → None。"""
    if not isinstance(warehouse, dict):
        return None
    n = state.num(warehouse.get(str(item_id), warehouse.get(item_id, 0)))
    return n if n is not None and n >= 0 else None


def read_stock(session, league_id, item_id: int, who: str) -> int:
    n = stock_of(session.request(ROUTE_GET, {"leagueId": str(league_id)}).get("warehouse"), item_id)
    if n is None:
        raise _Stop(BLOCKED, f"{who}读到的仓库库存格式无效")
    return n


def league_of(info):
    league = state.league(info)
    return state.league_id(league) if isinstance(league, dict) else None


@contextmanager
def seller_session(ctx: TaskContext, slot: int):
    """出售用的会话：就是当前存档时直接用当前会话，否则临时登录那个存档。"""
    try:
        if slot == ctx.session.slot:
            yield ctx.session
        else:
            with ctx.session.other_save(slot) as other:
                yield other
    except (_Stop, UncertainOutcome):
        raise
    except p.AllianceError as exc:
        raise _Stop(BLOCKED, f"存档 {slot} 登录或读取失败：{exc}") from exc


def held(session, slot: int, league_id, item_id: int, name: str) -> int:
    """该存档持有的材料数；必须属于同一联盟。"""
    if str(league_of(session.info)) != str(league_id):
        raise _Stop(BLOCKED, f"存档 {slot} 未确认属于同一联盟")
    n = state.bag_count(session.info, item_id)
    if n is None:
        raise _Stop(BLOCKED, f"存档 {slot} 没有返回可核对的{name}数量")
    return n


def restock(ctx: TaskContext, sellers: list[int], league_id, item_id: int, name: str) -> list[str]:
    """从勾选的存档出售，把仓库补到至少 2 个。返回出售记录；任何不确定都抛 _Stop。"""
    # 联盟仓库是共享状态：读取初始库存后，其他成员可能已经补足。
    need = max(0, 2 - read_stock(ctx.session, league_id, item_id, ""))
    if need <= 0:
        return []
    counts = []
    for slot in sellers:  # 先逐个核对持有量：总数不够就一件都不卖
        with seller_session(ctx, slot) as s:
            counts.append((slot, held(s, slot, league_id, item_id, name)))
    total = sum(n for _, n in counts)
    if total < need:
        # 卖家盘点期间也可能被其他联盟成员补货，报卖家库存不足前再查一次。
        need = max(0, 2 - read_stock(ctx.session, league_id, item_id, ""))
        if need <= 0:
            return []
        if total < need:
            raise _Stop(BLOCKED, f"{name}仓库缺 {need} 个，但勾选的存档一共只有 {total} 个；未出售、未购买")
    sold: list[str] = []
    for slot, _ in counts:
        if ctx.stopped:
            raise _Stop(SKIPPED, "用户停止，未继续出售")
        if need <= 0:
            break
        with seller_session(ctx, slot) as s:
            have = held(s, slot, league_id, item_id, name)  # 重新读取：别的存档/成员可能刚动过
            before = read_stock(s, league_id, item_id, f"存档 {slot} ")
            need = max(0, 2 - before)  # 以出售前的最新库存为准（别的成员可能刚补过货）
            n = min(need, have)
            if n <= 0:
                continue
            if ctx.stopped:
                raise _Stop(SKIPPED, "用户停止，未继续出售")
            s.request(ROUTE_SELL, {"itemId": item_id, "count": n, "type": "whSell", "buyType": -1})
            after = read_stock(s, league_id, item_id, f"存档 {slot} ")
            if after - before < n:
                raise _Stop(BLOCKED, f"存档 {slot} 出售 {name}×{n} 已发送，但仓库没有确认增加；停止购买，不自动重试")
            sold.append(f"存档 {slot} {name}×{n}")
            need -= n
    return sold


@task(ID, "联盟仓库", reads={ROUTE_GET}, writes={ROUTE_BUY, ROUTE_SELL}, spends={ROUTE_BUY},
      choices=Choices(materials, "材料", option="materials", limit=1, picks=(
          Pick("sellers", "缺货时从这些存档出售补货", lambda acc: [(i, f"存档 {i}") for i in range(1, 9)], single=False),
      )))
def run(ctx: TaskContext) -> TaskResult:
    picked = ctx.options.get("materials") or []
    item_id = state.num(picked[0]) if picked else None
    if item_id is None:
        return TaskResult(ID, SKIPPED, "没有选择要购买的材料")
    if item_id not in dict(materials()):
        return TaskResult(ID, BLOCKED, "所选材料不在半价材料表里，未购买")
    row = state.row("UnionWarehouse", item_id)
    name, currency, cost = str(row.get("name")), state.num(row.get("itemId")), state.num(row.get("buy_count"))  # 两个半价 = 一份原价
    sellers = sorted({n for n in (state.num(x) for x in ctx.options.get("sellers") or []) if n and 1 <= n <= 8})

    used = state.used(ctx.info, "leagueBuyItemCnt")
    if used is None:
        return TaskResult(ID, BLOCKED, "没有读到今日联盟仓库购买计数，未购买以免误买原价")
    if DAILY_LIMIT - used < 2:
        return TaskResult(ID, SKIPPED, f"今日半价名额不足 2 个（已买 {used}）")
    league_id = league_of(ctx.info)
    if not league_id:
        return TaskResult(ID, SKIPPED, "当前角色未加入联盟")
    have = state.bag_count(ctx.info, currency)
    if have is None:
        return TaskResult(ID, BLOCKED, f"没有读到{_name(currency)}余额，未购买")
    if have < cost:
        return TaskResult(ID, SKIPPED, f"{_name(currency)}不足（{have}/{cost}），未购买")  # 先查钱：不够就别去出售补货

    sold: list[str] = []
    try:
        stock = read_stock(ctx.session, league_id, item_id, "")
        if stock < 2:
            if not sellers:
                return TaskResult(ID, SKIPPED, f"{name} 仓库库存 {stock} 个，不足 2 个，也没有选择补货存档，未购买")
            sold = restock(ctx, sellers, league_id, item_id, name)
            if sold:  # 切换过存档：以重新登录后的数据为准
                ctx.refresh()
                fresh_used = state.used(ctx.info, "leagueBuyItemCnt")
                if fresh_used is None or DAILY_LIMIT - fresh_used < 2:
                    return TaskResult(ID, BLOCKED, f"已出售 {'、'.join(sold)}，但刷新后无法确认还剩 2 个半价名额，未购买")
            # 无论是否由本任务出售，都刷新共享仓库库存；restock 可能因其他成员补足而返回空记录。
            stock = read_stock(ctx.session, league_id, item_id, "")
            if stock < 2:
                return TaskResult(ID, BLOCKED, f"已出售 {'、'.join(sold) or '无'}，但仓库仍不足 2 个；未购买")
            have = state.bag_count(ctx.info, currency)
            if have is None or have < cost:
                return TaskResult(ID, BLOCKED, f"已出售 {'、'.join(sold)}，但重新读取后{_name(currency)}不足，未购买")
    except _Stop as stop:
        status = SUBMITTED if sold and stop.status == SKIPPED else stop.status
        return TaskResult(ID, status, (f"已出售 {'、'.join(sold)}；" if sold else "") + stop.detail)
    prefix = f"出售补货：{'、'.join(sold)}；" if sold else ""
    if ctx.stopped:
        return TaskResult(ID, SUBMITTED if sold else SKIPPED, f"{prefix}用户停止，未购买")
    try:
        ctx.request(ROUTE_BUY, {"itemId": item_id, "count": 2, "type": "whBuy", "buyType": 0})
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"{prefix}购买 {name}×2 失败：{exc}")
    return TaskResult(ID, SUBMITTED, f"{prefix}已半价购买 {name}×2，消耗{_name(currency)}×{cost}")
