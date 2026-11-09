"""通天河请教（周常页）：在别人运镖的船进入“请教区域”后请教，打赢拿材料。每天 ``tongtianRiverRobNum``（4）次。

界面“选择”：船只（多选，在材料网格上方）+ 材料（多选，只做所选船只下出现的材料；没选 = 不执行）+ 开关“只请教不低于自己的”（开 = 目标等级在 [自己, 自己+robLevel]；关 = 上下各 robLevel 级）。

流程（客户端 TTRProxy / TTRShipView）：``getEscortList`` 取目标列表和 ``serverTime``（毫秒）→ 过滤：不是自己、船只和材料对得上、
等级差 ≤ ``robLevel``（开关打开时只要不低于自己的） 且战力比 ≥ ``robPower``（与客户端同一道门槛）、本船被请教次数 < ``robMax``（行里的 ``robEvents``，没有就 ``getRobEvent``）、
处于“请教区域”（运镖已过 ``firstArea`` 分钟，且离结束还有 ≥ ``thirdArea`` 分钟；之前是安全区，之后是天灾区）→
``beginAttack {targetId}`` → ``createSinglePlayerCopy {mid:1100101}``（客户端的本地战斗，敌人固定一个）→
``endAttack {targetId: beginAttack 返回的 escortData.uid, win:1}``。

没有可请教的目标就每 10 秒刷新一次，最多等 30 分钟；有目标还在安全区时，按列表的 ``serverTime`` 算好进入请教区的时刻（扣掉读完列表后筛选花的时间），等到前 1 秒再每 0.1 秒试 ``beginAttack``。
被别人抢先（“正在被请教”等）→ 换下一个目标；结算前任何一步失败都不发 ``endAttack``；写请求超时 → 结果未知，不重发。
旧版用一个“本地战斗引擎”假装打赢，协议上它什么也没发，新版直接去掉。付费刷新/移除冷却的路由一律不碰。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Mapping

from .. import data, dungeon, api, protocol as p, state, timeplan
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, UncertainOutcome, task

ID = "tongtian_consult"
R_LIST = api.R_ESCORT_LIST
R_EVENTS = "scene.tongtianHandler.getRobEvent"
R_BEGIN = "scene.tongtianHandler.beginAttack"
R_END = "scene.tongtianHandler.endAttack"
R_COPY = dungeon.R_CREATE
R_POWER = api.R_POWER
BATTLE_MID = 1100101
SHIPS = ("木舟", "宝船", "银帆", "海舰")
REFRESH_SECONDS = 10.0
PREASK_SECONDS = 1.0  # 按服务器时间，目标进入请教区前 1 秒开始试 beginAttack（用户要求，原来 2 秒）
POLL_SECONDS = 0.1
MAX_POLL_SECONDS = 20.0  # 每 0.1 秒试一次 beginAttack 最多试这么久（提前 2 秒开始试，正常几秒内就进请教区）
MAX_WAIT_SECONDS = 1800.0
MAX_TIMES = 4  # 次数输入框的上限（每日请教次数）
BUSY = ("正在被其他人请教", "正在被请教", "已被其他人", "目标不存在", "目标已变化", "请稍后再试", "重复请求", "被其他人挑战", "正在被挑战", "对方正在战斗中")
NOT_READY = ("未到请教区域", "不可请教", "不能请教", "尚未进入请教", "当前区域", "安全区域", "安全区", "请教时间未到")


def common() -> Mapping[str, Any]:
    return data.row("TongtianRiver", 1) or {}


def target_config(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """列表行 → TongtianRiverEscortTarget 记录。goodType×1000 + 船位优先，其次直接用 goodId。"""
    table = state.table("TongtianRiverEscortTarget")
    good_type, good = state.num(row.get("goodType")), state.num(row.get("goodId"))
    if good_type is not None and good is not None and 0 <= good < 1000 and isinstance(table.get(str(good_type * 1000 + good)), Mapping):
        return table[str(good_type * 1000 + good)]
    found = table.get(str(row.get("goodId")))
    return found if isinstance(found, Mapping) else None


def material_of(config: Mapping[str, Any]) -> int | None:
    parts = str(config.get("target_subType")).split(";")
    return state.num(parts[1]) if len(parts) > 1 else None


def materials(ship: str | None = None) -> list[int]:
    return sorted({m for r in state.table_rows("TongtianRiverEscortTarget")
                   if (ship is None or r.get("name") == ship) and (m := material_of(r)) is not None})


def material_name(item_id: int | None) -> str:
    return str((state.row("Prop", item_id) or {}).get("name") or item_id) if item_id is not None else "未识别材料"


def region(row: Mapping[str, Any], server_time: int) -> tuple[str, float] | None:
    """(区域, 距离进入请教区的秒数)：安全 / 请教 / 天灾。"""
    end = state.num(row.get("endTime"))
    trip = state.num(row.get("escortTime"))
    if trip is None:
        ship = state.row("TongtianRiverShip", state.num(row.get("shipId")) or 1) or state.row("TongtianRiverShip", 1)
        trip = state.num((ship or {}).get("escortTime"))
    first, third = state.num(common().get("firstArea")), state.num(common().get("thirdArea"))
    if end is None or trip is None or first is None or third is None:
        return None
    left = (end - server_time) / 1000
    elapsed = trip - left / 60
    if elapsed < first:
        return "安全", max(0.0, (first - elapsed) * 60)
    return ("天灾", 0.0) if left / 60 < third else ("请教", 0.0)


def rows_of(listing: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = listing.get("list")
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if isinstance(item, Mapping):
            out.append(item)
    return out


def passes_gate(row: Mapping[str, Any], mine: tuple[int, float], upper_only: bool = False) -> bool:
    """客户端 getEscortListBack 的门槛：等级差和战力比，外加我们自己的开关：upper_only = 只请教不低于自己的。缺字段一律不过。"""
    user = row.get("userdata")
    if not isinstance(user, Mapping):
        return False
    level = state.num(user.get("level"))
    try:
        power = float(user.get("fightPower"))
    except (TypeError, ValueError):
        return False
    if level is None or power <= 0 or mine[1] <= 0:
        return False
    return ((not upper_only or level >= mine[0])
            and abs(level - mine[0]) <= (state.num(common().get("robLevel")) or 0)
            and min(mine[1], power) / max(mine[1], power) >= float(common().get("robPower") or 0))


def my_level_power(ctx: TaskContext) -> tuple[int, float] | None:
    info = ctx.info
    p1 = info.get("p1") if isinstance(info, Mapping) else None
    level = state.num(p1.get("level")) if isinstance(p1, Mapping) else state.num(info.get("level"))
    power = None
    try:
        power = state.num(api.character_power(ctx.request).get("p1"))  # 实时战力；进角里的 power 可能过期
    except p.AllianceError:
        pass
    if power is None:
        power = state.num(info.get("power", info.get("fightPower")))
    return (level, float(power)) if level is not None and power is not None and power > 0 else None


def rob_count(ctx: TaskContext, row: Mapping[str, Any]) -> int:
    events = row.get("robEvents")
    if isinstance(events, list):
        return len(events)
    raw = ctx.request(R_EVENTS, {"targetId": row.get("uid")}).get("event")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise p.AllianceError("getRobEvent 返回无法解析的 event") from exc
    if not isinstance(raw, list):
        raise p.AllianceError("getRobEvent 没有返回 event 数组")
    return len(raw)


def classify(ctx: TaskContext, listing: Mapping[str, Any], ships: set[str], wanted: set[int], mine, me: str, skip: set[str], full: set[str], why: dict[str, int], upper_only: bool):
    """(请教区目标列表, 安全区目标 [(行, 等待秒数)])；请教区按已运镖时间从短到长。why 累计各条件筛掉的数量，供日志诊断。"""
    now = state.num(listing.get("serverTime"))
    if now is None:
        raise p.AllianceError("目标列表缺少 serverTime，无法判断区域")
    ready, safe, limit = [], [], state.num(common().get("robMax")) or 0

    def drop(reason: str) -> None:
        why[reason] = why.get(reason, 0) + 1

    for row in rows_of(listing):
        key = str(row.get("uid", ""))
        config = target_config(row)
        if not key or key == me or key in skip or key in full:
            continue
        if config is None or config.get("name") not in ships:
            drop("船只不符")
            continue
        if material_of(config) not in wanted:
            drop("材料不符")
            continue
        if not passes_gate(row, mine, upper_only):
            drop("等级/战力不符")
            continue
        if (area := region(row, now)) is None or area[0] == "天灾":
            drop("天灾区")
            continue
        if rob_count(ctx, row) >= limit:
            full.add(key)  # 被请教次数只增不减，下次不用再查
            continue
        (safe if area[0] == "安全" else ready).append((row, area[1]))
    safe.sort(key=lambda t: t[1])
    return [r for r, _ in ready], safe


def has(error: Exception, words: tuple[str, ...]) -> bool:
    return any(w in str(error) for w in words)


def _pause(ctx: TaskContext, seconds: float, lost: str) -> None:
    """等一会儿。被用户停止或连接中断（lost 是说明）抛 dungeon.Stop。"""
    if ctx.sleep(seconds):
        return
    if ctx.stopped:
        raise dungeon.Stop("已被用户停止")
    error = ctx.session.idle_error
    raise dungeon.Stop(lost + (f"（{error}）" if error else ""))


def consult(ctx: TaskContext, row: Mapping[str, Any], gains: dungeon.Gains, deadline: float) -> str:
    """请教一个目标。返回 "done" / "busy"（被抢先，换目标）/ "timeout"；失败抛 dungeon.Stop。"""
    began = None
    give_up = time.monotonic() + MAX_POLL_SECONDS
    while began is None:
        if ctx.stopped:
            raise dungeon.Stop("已被用户停止")
        if time.monotonic() > deadline:
            return "timeout"
        try:
            began = ctx.request(R_BEGIN, {"targetId": row.get("uid")})
        except p.AllianceError as exc:
            if has(exc, BUSY):
                return "busy"
            if has(exc, NOT_READY):
                if time.monotonic() > give_up:  # 一直“不可请教”：不是差那一两秒，是这个目标请教不了，换下一个
                    return "busy"
                _pause(ctx, POLL_SECONDS, "等待目标可请教时连接中断")
                continue
            raise dungeon.Stop(f"开始请教失败：{exc}") from exc
    escort = began.get("escortData")
    target = escort.get("uid") if isinstance(escort, Mapping) else None
    if target in (None, ""):
        raise dungeon.Stop("beginAttack 响应缺少 escortData.uid，无法确认目标；未结算")
    try:
        ctx.request(R_COPY, {"mid": BATTLE_MID, "rnd": random.randint(1, 10000)})
    except p.AllianceError as exc:
        raise dungeon.Stop(f"创建战斗失败：{exc}；未结算") from exc
    try:
        gains.add(ctx.request(R_END, {"targetId": target, "win": 1}), ctx.session.drain_pushes())
    except p.AllianceError as exc:
        raise dungeon.Stop(f"结算失败：{exc}；不重发，请登录游戏核对") from exc
    return "done"


def ships() -> list[tuple[str, str]]:
    return [(s, s) for s in SHIPS]


def _plan(ctx: TaskContext, times: int):
    """校验选项和今日次数。返回 (船只, 材料, 船只名, 暂停前已请教次数, 这次要请教几次, 我的等级/战力)；不能请教时返回结果。"""
    chosen = {x for x in ctx.options.get("ships") or [] if x in SHIPS}
    wanted = {state.num(m) for m in ctx.options.get("materials") or []} - {None}
    if not chosen:
        return TaskResult(ID, SKIPPED, "没有选择船只")
    legal = {m for sh in chosen for m in materials(sh)}
    ship = "、".join(sh for sh in SHIPS if sh in chosen)
    if not wanted:
        return TaskResult(ID, SKIPPED, "没有选择材料")
    if not wanted & legal:
        return TaskResult(ID, SKIPPED, f"所选材料不属于{ship}（可选：{'、'.join(material_name(m) for m in sorted(legal))}）")
    wanted &= legal
    prior = dungeon.resume_done(ctx).get("n", 0)  # 暂停前已请教的次数
    ctx.progress["n"] = prior
    times -= prior
    if times <= 0:
        return TaskResult(ID, SUBMITTED, "暂停前已经打完设置的次数")
    used, cap = state.used(ctx.info, "tongtianRiverRobNum"), state.num(common().get("tongtianRiverRobNum"))
    if used is None or not cap:
        return TaskResult(ID, BLOCKED, "读不到今日请教次数（everyDaySetting.tongtianRiverRobNum），未请教")
    left = min(max(0, cap - used), times)
    if left == 0:
        return TaskResult(ID, SKIPPED, "今日请教次数已用完")
    mine = my_level_power(ctx)
    if mine is None:
        return TaskResult(ID, BLOCKED, "读不到角色等级/战力，无法按客户端门槛筛选目标，未请教")
    return chosen, wanted, ship, prior, left, mine


def _hunt(ctx: TaskContext, chosen: set[str], wanted: set[int], ship: str, prior: int, left: int, mine, gains: dungeon.Gains) -> tuple[int, str | None, bool]:
    """反复刷新目标列表、挑一个去请教，直到打满 left 次。返回 (次数, 原因, 是否失败)。"""
    me = str(ctx.info.get("uid", ""))
    done, skip, full = 0, set(), set()
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    why = None
    failed = False
    ctx.log(f"请教 0/{left}，开始寻找{ship}目标")
    try:
        while done < left:
            if ctx.stopped:
                why = "已被用户停止"
                break
            if time.monotonic() > deadline:
                why = f"等了 {int(MAX_WAIT_SECONDS)} 秒仍没有可请教的目标"  # 以“等”开头：没打成也算跳过
                break
            listing = api.escort_list(ctx.request)
            fetched = time.monotonic()  # 列表里 serverTime 对应的本机时刻：之后筛选（查被请教次数）花的时间要从等待里扣掉
            dropped: dict[str, int] = {}
            ready, safe = classify(ctx, listing, chosen, wanted, mine, me, skip, full, dropped, bool(ctx.options.get("upper_only")))
            target = ready[0] if ready else None
            if target is None and safe and safe[0][1] < deadline - time.monotonic():
                row, eta = safe[0]
                eta -= time.monotonic() - fetched
                ctx.log(f"请教 {done}/{left}，{ship}目标 {row.get('uid')} 还在安全区，{max(0.0, eta):.0f} 秒后进入请教区")
                if eta > PREASK_SECONDS:
                    _pause(ctx, eta - PREASK_SECONDS, "等待目标进入请教区期间连接中断")
                target = row
            if target is None:
                seen = len(rows_of(listing))
                detail = "、".join(f"{k}{v}" for k, v in dropped.items())
                ctx.log(f"请教 {done}/{left}，没有可请教的目标（列表 {seen} 条{'，筛掉：' + detail if detail else ''}{'，已满次 ' + str(len(full)) if full else ''}），10 秒后刷新")
                _pause(ctx, REFRESH_SECONDS, "刷新请教目标期间连接中断")
                continue
            outcome = consult(ctx, target, gains, deadline)
            if outcome == "busy":
                skip.add(str(target.get("uid")))
                continue
            if outcome == "timeout":
                why = "等待目标进入请教区域超时"
                break
            done += 1
            ctx.progress["n"] = prior + done
            skip.clear()
            ctx.log(f"请教 {done}/{left}，继续寻找" if done < left else f"请教 {done}/{left} 完成")
    except dungeon.Stop as stop:
        why = str(stop)
        failed = not ctx.stopped
    except p.AllianceError as exc:
        why = f"读取目标失败：{exc}"
        failed = True
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"已请教 {done} 次；{exc}") from exc
    return done, why, failed


@task(ID, "请教", reads={R_LIST, R_EVENTS, R_POWER}, writes={R_BEGIN, R_COPY, R_END}, group="周常", account_wide=True, needs=(("materials",), ("ships",)),
      times_max=MAX_TIMES, choices=Choices(lambda: [(m, material_name(m)) for m in materials()], "材料", option="materials", cols=6, caption="灵气", toggles_left=True,
                      toggles=(("upper_only", "只请教等级不低于自己的"),),
                      note="只做所选船只下的所选材料。等级差、战力比按游戏门槛；开着“只请教等级不低于自己的”只找等级 ≥ 自己的。\n"
                           "目标还在安全区就等它进请教区；没有目标每 10 秒刷新，最多等 30 分钟。",
                      picks=(Pick("ships", "船只", lambda acc: ships(), single=False, top=True),)))
def run(ctx: TaskContext) -> TaskResult:
    if why := timeplan.closed(ID, ctx.now):
        return TaskResult(ID, SKIPPED, f"{why}，未请教")
    times = dungeon.read_times(ctx, ID, MAX_TIMES)
    if isinstance(times, TaskResult):
        return times
    if times == 0:
        return TaskResult(ID, SKIPPED, "次数留空，不执行")
    plan = _plan(ctx, times)
    if isinstance(plan, TaskResult):
        return plan
    chosen, wanted, ship, prior, left, mine = plan
    gains = dungeon.Gains()
    done, why, failed = _hunt(ctx, chosen, wanted, ship, prior, left, mine, gains)
    note = f"{ship}请教 {done} 次" + dungeon.gain_note(gains)
    if why:
        status = BLOCKED if failed else SUBMITTED if done else SKIPPED
        return TaskResult(ID, status, f"{note}；{why}" if done else why)
    return TaskResult(ID, SUBMITTED, note)
