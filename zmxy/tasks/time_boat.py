"""时光之舟（周常页）：用时光能量兑换所选副本的额外挑战次数。

界面：周常页里的独立模块（``mainwin/boat.py`` 的 ``_build_boat``）——顶部时光能量，九宫玄塔（id 3）/ 神兽森林（id 2）/ 噩梦副本（id 4）
各一行，显示每次消耗、本周剩余兑换次数、挑战次数，各有自己的次数框和“兑换”按钮；点哪一行的“兑换”，
界面就把 ``options = {"levels": [id], "times": n}`` 交给本任务。
目标的消耗、每周上限、关卡全部来自 ``TimeEnergy`` 表，并且必须是 ``consume_type=coin / consume_subType=energy``（时光能量）。

协议（客户端 TimeBoatProxy.buy）：
* 九宫/神兽（``type=0``）：按表里 ``dungeonId`` 的顺序，取第一个 ``dailyCount>0`` 的关卡 →
  ``activityHandler.copyBuyCountByEnergy {energyId, dungeonId}``，响应必须带回该关卡的最新 ``dungeon.dailyCount``；
* 噩梦（``type=4``）：``activityHandler.buyDungeonTreasureCnt {energyId:4}``，必须看到 ``everyWeekSetting.timeEnergy[4]`` 恰好加 1
  （响应、推送里没有就刷新角色信息再核对），对不上就停止，**不重发**。

次数 = 本周剩余兑换次数 ``weekChangeNum − everyWeekSetting.timeEnergy[id]``（没有该键按 0，前提是 timeEnergy 对象存在）与现有时光能量
``info.energy ÷ 每次消耗`` 的较小者。选项 ``crystal`` = True（界面在能量不够时弹确认框，用户点“是”才带上）时，每次兑换前能量不够就用背包里的时光水晶补足
（``itemHandler.useItem {index:0, count:1, id:时光水晶}``，每个 +1，用完刷新角色信息核对能量）；没有这个选项一律不用水晶。不碰点券。

“兑换并挑战”（选项 ``fight`` = True）：兑换来的次数马上打掉，再兑换下一批，直到兑换够设置的次数（``_fight_rounds``）。
一批最多兑换“挑战次数上限”那么多（九宫上限 3、设置 9 次 = 兑换 3 次打 3 次，共三轮）。打用的是对应任务本身
（``FIGHT``：九宫玄塔 / 神兽森林 / 噩梦关卡），关卡、模式等设置原样沿用那个任务在日常页的设置。
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass, field, replace

from .. import data, dungeon, api, protocol as p, quota, servertime, state
from ..core import BLOCKED, REGISTRY, SKIPPED, SUBMITTED, TaskContext, TaskResult, task
from . import forest_sweep

ID = "time_boat"
PROFILE_KEY = "time_boat"
R_COPY = "scene.activityHandler.copyBuyCountByEnergy"
R_TREASURE = "scene.activityHandler.buyDungeonTreasureCnt"
R_USE = api.R_USE_ITEM  # 使用时光水晶（客户端 TimeEnergyView 的道具面板 → UseItemCmd）：每个 +1 时光能量
CRYSTAL = 101027001  # 时光水晶（Prop 表 propType 1027，attributeT1=coin / attributeV1=energy / attributeV2=1）
OFFERS = {3: "九宫玄塔", 2: "神兽森林", 4: "噩梦副本"}  # 界面顺序
TREASURE_SHARE = 1400200
MAX_TIMES = 50
FIGHT = {3: "nine_tower", 2: "forest", 4: "dungeon_nightmare"}  # 兑换并挑战：兑换来的次数交给哪个任务打（沿用它自己的设置）
GROWTH_OFFER = {2: 3, 3: 2}  # 养成周（state.GROWTH_WEEKS 的序号）→ 这一周要用掉兑换次数的目标：幻兽法宝 → 九宫玄塔，灵宠宝石 → 神兽森林
REGEN = 2  # 每天 0 点补充几点时光能量（Common.timeEnergyRePoint 读不到时用）


@dataclass
class Wrote:
    """这次运行改过服务器上的什么：决定收尾要不要刷新、哪些兑换项的周计数已确认。"""
    anything: bool = False
    offers: set[int] = field(default_factory=set)


def _common(key: str):
    return state.num((data.row("Common", 1) or {}).get(key))


def _energy(info) -> int | None:
    return state.num(info.get("energy")) if isinstance(info, dict) else None


def offer(offer_id: int) -> dict | None:
    """通过表校验的兑换项；表对不上返回 None。"""
    row = data.row("TimeEnergy", offer_id)
    if not row or state.num(row.get("id")) != offer_id:
        return None
    kind, cost, weekly = state.num(row.get("type")), state.num(row.get("consume_count")), state.num(row.get("weekChangeNum"))
    if row.get("consume_type") != "coin" or row.get("consume_subType") != "energy" or not cost or cost <= 0 or not weekly or weekly <= 0:
        return None
    if offer_id == 4:
        if kind != 4 or row.get("dungeonId") != "-1" or state.num(row.get("dungeonTreasureShareId")) != TREASURE_SHARE:
            return None
        return {"id": 4, "name": OFFERS[4], "cost": cost, "weekly": weekly, "type": 4, "mids": ()}
    mids = [state.num(x) for x in str(row.get("dungeonId")).split(";")]
    if kind != 0 or not mids or any(m is None or m <= 0 for m in mids):
        return None
    return {"id": offer_id, "name": OFFERS[offer_id], "cost": cost, "weekly": weekly, "type": 0, "mids": tuple(mids)}


def catalog() -> list[tuple[int, str]]:
    return [(i, name) for i, name in OFFERS.items() if offer(i)]


def weekly_used(source, offer_id: int) -> int | None:
    """本周已兑换次数。timeEnergy 对象缺失 → None；有对象但没有该键 → 0。"""
    week = state.weekly(source)
    counts = week.get("timeEnergy") if week else None
    if not isinstance(counts, dict):
        return None
    n = state.num(counts.get(str(offer_id), counts.get(offer_id, 0)))
    return n if n is not None and n >= 0 else None


def copy_target(info, mids) -> int | None:
    """客户端规则：配置顺序里第一个今日已用次数 > 0 的关卡。"""
    for mid in mids:
        used = quota._daily_used(info, mid)
        if used is None:
            return None
        if used > 0:
            return mid
    return None


def fight_quota(info, target: dict) -> tuple[int | None, int | None]:
    """游戏里“挑战次数 剩余/上限”：九宫/神兽取第一个关卡的共享次数，噩梦取共享宝箱池。读不到返回 (None, None)。"""
    mid = target["mids"][0] if target["type"] == 0 else state.num(str((data.row("DungeonTreasureShare", TREASURE_SHARE) or {}).get("shareDailyDungeonId")).split(";")[0])
    if not mid:
        return None, None
    left = quota.of(info, mid).left
    if left is None or left == float("inf"):
        return None, None
    if target["type"] == 4:
        cap = state.num((data.row("DungeonTreasureShare", TREASURE_SHARE) or {}).get("challengeNumMax"))
    else:
        group = quota._helper_group(mid)
        cap = group[2] if group else state.num((data.row("Dungeon", mid) or {}).get("dailyCountMax"))
    return max(0, int(left)), cap


def profile(info) -> dict:
    """界面看板用的快照：时光能量，以及每个目标的 消耗 / 本周剩余兑换次数 / 挑战次数。读不到的字段为 None。"""
    energy, top = _energy(info), _common("timeEnergyMax")
    rows = {}
    for offer_id, _ in catalog():
        target = offer(offer_id)
        used = weekly_used(info, offer_id)
        left, cap = fight_quota(info, target)
        rows[str(offer_id)] = {"cost": target["cost"], "used": used,
                               "remain": None if used is None else max(0, target["weekly"] - used),
                               "fight": left, "cap": cap}
    at = servertime.now()
    return {PROFILE_KEY: {"energy": energy, "max": top, "rows": rows, "at": at, "week": _week_key(at)}}


def _week_key(at: float | int | None) -> str | None:
    if not at:
        return None
    here = datetime.fromtimestamp(at, state._CN).isocalendar()
    return f"{here.year}-{here.week:02d}"


def _week_confirmed(row: dict, old: dict, old_used, same_week: bool, confirmed_here: bool, baseline: bool) -> bool:
    used = state.num(row.get("used"))
    changed = used is not None and old_used is not None and used != old_used
    if same_week:
        # 同一周的旧快照可能早于这个标记，默认可信；跨周合并判出旧计数没变时才会明确写 False
        return (bool(old.get("week_confirmed", True)) or row.get("week_confirmed") is True or confirmed_here
                or changed or (used is not None and old_used is None))
    return bool(row.get("week_confirmed")) or confirmed_here or changed or (baseline and old_used is None and used is not None)


def merge_profile(current: dict, previous: dict | None = None, *, confirmed: set[int] = frozenset(),
                  baseline: bool = True) -> dict:
    """跨周时服务器可能还没更新旧计数：没变过的计数记成未确认。之前没有计数可比时，baseline 决定是否信任这次读到的。"""
    previous = previous if isinstance(previous, dict) else {}
    old_rows = previous.get("rows") if isinstance(previous.get("rows"), dict) else {}
    rows = current.get("rows") if isinstance(current.get("rows"), dict) else {}
    old_week, week = previous.get("week") or _week_key(previous.get("at")), current.get("week") or _week_key(current.get("at"))
    same_week = bool(week and old_week == week)
    merged = {}
    for key, raw in rows.items():
        row = dict(raw) if isinstance(raw, dict) else {}
        old = old_rows.get(key) if isinstance(old_rows.get(key), dict) else {}
        old_used = state.num(old.get("used"))
        old_remain = state.num(old.get("remain"))
        if old_used is None and old_remain is not None:
            target = offer(state.num(key))
            old_used = target["weekly"] - old_remain if target and 0 <= old_remain <= target["weekly"] else None
        row["week_confirmed"] = _week_confirmed(row, old, old_used, same_week, state.num(key) in confirmed, baseline)
        merged[key] = row
    return {**current, "week": week, "rows": merged}


def board(acc) -> dict:
    """周常页时光之舟模块要显示的内容：rows = [(id, 名称, 每次消耗, 本周剩余, 挑战次数)]。

    数据来自上一次读取/兑换时存进存档设置的快照，没读过显示 —（不当成 0）。"""
    snap = (acc.get("profile") or {}).get(PROFILE_KEY) or {}
    rows = snap.get("rows") or {}
    out = []
    for offer_id, name in catalog():
        row = rows.get(str(offer_id)) or {}
        fight = "—" if row.get("fight") is None else f"{row['fight']}/{row['cap']}" if row.get("cap") is not None else str(row["fight"])
        out.append((offer_id, name, offer(offer_id)["cost"], "—" if row.get("remain") is None else str(row["remain"]), fight))
    regen = _common("timeEnergyRePoint")
    note = (f"每天 0 点补充 {regen} 点 · " if regen else "") + "兑换次数每周重置"
    return {"energy": snap.get("energy"), "max": snap.get("max"), "note": note, "at": snap.get("at"), "rows": out}


def estimate(acc, now: float) -> int | None:
    """预估的时光能量：快照里的能量 + 从读取到现在过了几个 0 点（北京时间）× 每天补充的点数，不超过上限。
    没读过、读不到能量或上限 → None。只给界面看，兑换仍以服务器实时数据为准。"""
    snap = (acc.get("profile") or {}).get(PROFILE_KEY) or {}
    energy, top, at = snap.get("energy"), snap.get("max"), snap.get("at")
    if energy is None or not top or not at:
        return None
    days = (datetime.fromtimestamp(now, state._CN).date() - datetime.fromtimestamp(at, state._CN).date()).days
    regen = _common("timeEnergyRePoint") or REGEN
    return max(energy, min(top, energy + max(0, days) * regen))


def urgent(acc, now: float, live: int | None = None) -> set[int]:
    """界面上名字要变红的兑换目标（周常页“次数提醒”开着时）：本周的兑换次数该用了、但还没用完。
    * 九宫玄塔 / 神兽森林：对应的养成周里——第一周的周六、周日（兑换次数周一重置，不用就没了），
      和第二周的周一到活动结束（``state.growth_second_week``）；
    * 噩梦副本：每周六、周日（北京时间）。
    只有本周计数已确认且剩余为 0 才会取消提醒；跨周时服务器可能还没更新旧计数，旧计数会保留提醒，
    直到读到变化或本周成功兑换后再按当前周状态判断。"""
    here = datetime.fromtimestamp(now, state._CN)
    weekend = here.weekday() >= 5
    due = {GROWTH_OFFER.get(state.growth_second_week(now, live=live))} | ({4, GROWTH_OFFER.get(state.growth_now(now, live=live))} if weekend else set())
    snap = (acc.get("profile") or {}).get(PROFILE_KEY) or {}
    at, rows = snap.get("at"), snap.get("rows") or {}
    fresh = bool(at) and _week_key(at) == _week_key(now)

    def confirmed_this_week(row: dict) -> bool:
        # 同一周、没有标记的是早于这个标记的快照，算可信；merge_profile 判出旧计数没变时会明确写 False
        return fresh and row.get("week_confirmed", True) is True and row.get("remain") == 0

    return {i for i, _ in catalog() if i in due and not confirmed_this_week(rows.get(str(i)) or {})}


@task(ID, "时光之舟", spends={R_COPY, R_TREASURE}, group="周常", needs=(("levels",),),
      reads={dungeon.R_SELECT_MONSTER, forest_sweep.R_POWER},  # “兑换并挑战”要用到九宫 / 神兽森林（手打、扫荡）/ 噩梦关卡的路由
      writes={R_COPY, R_TREASURE, R_USE, forest_sweep.R_SWEEP} | dungeon.ROUTES[dungeon.SPECIAL] | dungeon.ROUTES[dungeon.NIGHTMARE],
      auto=lambda ctx: ("时光之舟", profile(ctx.info)))  # 周常页“自动配置”顺带刷新时光之舟看板
def run(ctx: TaskContext) -> TaskResult:
    wrote = Wrote()
    result = _run(ctx, wrote)
    try:  # 把最新的能量/次数写回存档设置，供周常页的时光之舟模块显示；读不到就沿用上次的
        info = ctx.refresh() if result.status == SUBMITTED or wrote.anything else ctx.info
        snapshot = profile(info)
        snapshot[PROFILE_KEY] = merge_profile(snapshot[PROFILE_KEY], confirmed=wrote.offers, baseline=False)
        return replace(result, profile=snapshot)
    except Exception:
        return result


def _picked(ctx: TaskContext) -> dict | None:
    """选中的兑换项：必须正好一个、并通过表校验。"""
    chosen = [state.num(v) for v in ctx.options.get("levels") or []]
    return offer(chosen[0]) if len(chosen) == 1 and chosen[0] in OFFERS else None


def _plan(ctx: TaskContext, times: int):
    """校验选项和余额，算出这次能兑换几次。返回 (目标, 时光能量, 本周已兑换, 本周还可兑换, 计划次数)；不能兑换时返回结果。"""
    if not ctx.options.get("levels"):
        return TaskResult(ID, SKIPPED, "没有选择兑换目标")
    if times == 0:
        return TaskResult(ID, SKIPPED, "兑换次数留空，不执行")
    target = _picked(ctx)
    if target is None:
        return TaskResult(ID, BLOCKED, "所选目标没有通过 TimeEnergy 表校验，未执行")
    name, cost, info = target["name"], target["cost"], ctx.info
    energy = _energy(info)
    used = weekly_used(info, target["id"])
    if energy is None or energy < 0:
        return TaskResult(ID, BLOCKED, "服务器没有返回有效的时光能量余额，未兑换")
    if used is None:
        return TaskResult(ID, BLOCKED, f"无法确认{name}本周已兑换次数（everyWeekSetting.timeEnergy），未兑换")
    left = max(0, target["weekly"] - used)
    if left == 0:
        return TaskResult(ID, SKIPPED, f"{name}本周兑换次数已用完（{used}/{target['weekly']}）")
    crystals = 0
    if ctx.options.get("crystal"):
        owned = state.bag_count(info, CRYSTAL)
        if owned is None:
            return TaskResult(ID, BLOCKED, "读不到背包里的时光水晶数量，未兑换")
        crystals = owned
    affordable = (energy + crystals) // cost
    planned = min(times, left, affordable)
    if planned == 0:
        extra = f"，时光水晶 {crystals} 个也不够" if ctx.options.get("crystal") else "（不使用时光水晶）"
        return TaskResult(ID, SKIPPED, f"时光能量不足：{name}每次需要 {cost} 点，当前 {energy} 点{extra}")
    return target, energy, used, left, planned


def _top_up(ctx: TaskContext, name: str, cost: int, energy: int, info, spent: list[int]):
    """能量不够兑换一次：用时光水晶补足。返回 (最新 info, 能量, 停止原因)；用掉的水晶数累加到 spent[0]。"""
    need = cost - energy
    try:
        for _ in range(need):
            if ctx.stopped:
                return info, energy, "已被用户停止"
            api.use_item(ctx.request, CRYSTAL)
            spent[0] += 1
        if ctx.stopped:
            return info, energy, "已被用户停止"
        info = ctx.refresh()
    except p.AllianceError as exc:
        return info, energy, f"使用时光水晶补足能量失败：{exc}"
    real = _energy(info)
    if real is None or real < cost:
        return info, energy, f"用了 {need} 个时光水晶后能量仍不足（服务器 {real}，需要 {cost}），停止"
    ctx.log(f"{name}：用 {need} 个时光水晶补足能量（现在 {real} 点）")
    return info, real, None


def _treasure_used(ctx: TaskContext, response) -> int | None:
    """兑换宝箱次数后，服务器的本周已兑换次数：先看响应，再看推送，最后重读角色信息；都读不到 → None。"""
    now = weekly_used(response, 4)
    if now is None:
        now = weekly_used(ctx.session.drain_pushes(), 4)
    if now is None:
        try:
            now = weekly_used(ctx.refresh(), 4)
        except p.AllianceError:
            now = None
    return now


def _run(ctx: TaskContext, wrote: Wrote) -> TaskResult:
    times = dungeon.read_times(ctx, ID, MAX_TIMES)
    if isinstance(times, TaskResult):
        return times
    return _fight_rounds(ctx, times, wrote) if ctx.options.get("fight") else _exchange(ctx, times, wrote)[2]


def _fight_rounds(ctx: TaskContext, times: int, wrote: Wrote) -> TaskResult:
    """兑换并挑战：有挑战次数就先打（对应任务按自己的设置打），打完了再兑换下一批（一批最多 = 挑战次数上限），直到兑换够 times 次。
    断点（背包满等）记的是已兑换次数 ``progress["n"]``：继续时先把换来还没打的打掉，再接着兑换剩下的。"""
    target = _picked(ctx)
    if target is None or times == 0:
        return _exchange(ctx, times, wrote)[2]  # 没选目标 / 次数留空 / 表对不上：和只兑换时一样的说明
    name, spec = target["name"], REGISTRY[FIGHT[target["id"]]]
    opts = {k: v for k, v in (ctx.all_options.get(spec.id) or {}).items() if k != "resume_done"}
    if not opts.get("levels"):
        return TaskResult(ID, BLOCKED, f"「{spec.title}」还没有选关卡（挑战沿用它自己的设置），未兑换")
    prior = dungeon.resume_done(ctx).get("n", 0)
    done, rounds, crystals, fights, why, note, before = 0, 0, 0, [], None, "", None  # before：上一次挑战前的剩余次数
    while True:
        if ctx.stopped:
            why = "已被用户停止"
            break
        try:
            info = ctx.refresh()
        except p.AllianceError as exc:
            why = str(exc)
            break
        left, cap = fight_quota(info, target)
        if left is None or not cap:
            why = f"读不到{name}的挑战次数，停止"
            break
        if left > 0:
            if before is not None and left >= before:
                why = f"「{spec.title}」按现在的设置打不掉剩下的 {left} 次挑战次数，停止"
                break
            before = left
            result = spec.run(replace(ctx, options=opts, progress={}))
            if result.status != SKIPPED:  # 挑战可能打掉几次才被阻止
                wrote.anything = True
            if result.status != SUBMITTED:
                why = f"「{spec.title}」{result.detail}"
                break
            fights.append(result.detail)
            continue
        before = None
        if prior + done >= times:
            break
        n, used, result = _exchange(ctx, min(times - prior - done, cap), wrote)
        done, crystals, rounds = done + n, crystals + used, rounds + bool(n)
        if n:
            ctx.progress["n"] = prior + done
        if result.status == SKIPPED:
            note = result.detail  # 本周兑换次数用完 / 能量不够：正常结束
            break
        if result.status != SUBMITTED:
            why = result.detail
            break
    head = f"{name}兑换 {done} 次（分 {rounds} 轮），消耗时光能量 {done * target['cost']} 点" + (f"（其中用了 {crystals} 个时光水晶）" if crystals else "")
    detail = "；".join(([head] if done else []) + fights)
    if why:
        return TaskResult(ID, BLOCKED, f"{detail}；{why}" if detail else why)
    if not detail:
        return TaskResult(ID, SKIPPED, note or "断点前已兑换完设置的次数")
    return TaskResult(ID, SUBMITTED, f"{detail}；{note}" if note else detail)


def _exchange(ctx: TaskContext, times: int, wrote: Wrote) -> tuple[int, int, TaskResult]:
    """兑换 times 次。返回 (兑换成功的次数, 用掉的时光水晶, 结果)。"""
    plan = _plan(ctx, times)
    if isinstance(plan, TaskResult):
        return 0, 0, plan
    target, energy, used, left, planned = plan
    name, cost, info = target["name"], target["cost"], ctx.info
    done, why, spent = 0, None, [0]  # spent[0]：用掉的时光水晶
    for number in range(1, planned + 1):
        if ctx.stopped:
            why = "已被用户停止"
            break
        if energy < cost:  # 只会在用户确认过用水晶时发生（planned 已按“能量+水晶”算过）
            info, energy, why = _top_up(ctx, name, cost, energy, info, spent)
            if why:
                break
        if target["type"] == 0:
            mid = copy_target(info, target["mids"])
            if mid is None:
                why = "没有已用过次数（dailyCount>0）的关卡可兑换，或副本记录读不到"
                break
            route, body = R_COPY, {"energyId": target["id"], "dungeonId": mid}
        else:
            route, body = R_TREASURE, {"energyId": 4}
        try:
            response = ctx.request(route, body)
        except p.AllianceError as exc:
            why = f"第 {number} 次被服务器拒绝：{exc}"
            break
        wrote.anything = True  # 服务器已接受，后面即使响应不全要停止也算写过
        if target["type"] == 0:
            update = response.get("dungeon")
            count = state.num(update.get("dailyCount")) if isinstance(update, dict) else None
            if not isinstance(update, dict) or state.num(update.get("id")) != mid or count is None or count < 0:
                why = f"第 {number} 次响应没有带回关卡 {mid} 的 dailyCount，停止"
                done += 1  # 请求已被服务器接受（code 200），能量按已消耗计
                break
            info["dungeon"] = _with_record(info.get("dungeon"), mid, update)
        else:
            now = _treasure_used(ctx, response)
            if now != used + 1:
                why = f"第 {number} 次后服务器兑换次数没有明确 +1（{used}→{now}），停止且不重发"
                break
            used = now
        wrote.offers.add(target["id"])
        done += 1
        energy -= cost
        ctx.log(f"{name} {done}/{planned}")
    used_crystals = spent[0]
    detail = f"{name}兑换 {done} 次，消耗时光能量 {done * cost} 点" + (f"（其中用了 {used_crystals} 个时光水晶）" if used_crystals else "（未使用点券/时光水晶）")
    if why:
        return done, used_crystals, TaskResult(ID, BLOCKED, f"{detail}；{why}" if done else why)
    return done, used_crystals, TaskResult(ID, SUBMITTED, detail + (f"；还可兑换 {left - done} 次，能量够 {max(0, energy // cost)} 次" if done < times else ""))


def _with_record(records, mid: int, update):
    """把服务器返回的关卡记录并回本地副本记录，下一次选关用最新的 dailyCount。"""
    if isinstance(records, list):
        rows = [dict(r) if isinstance(r, dict) else r for r in records]
        for i, r in enumerate(rows):
            if isinstance(r, dict) and state.num(r.get("id")) == mid:
                rows[i] = {**r, **update}
                return rows
        return [*rows, dict(update)]
    merged = dict(records) if isinstance(records, dict) else {}
    merged[str(mid)] = {**(merged.get(str(mid)) or {}), **update}
    return merged
