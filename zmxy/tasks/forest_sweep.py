"""神兽森林的扫荡模式（任务 forest 里选“扫荡”时走这里）：用客户端的扫荡按钮（``copyHandler.sweep``，奖励直接在响应里），不打战斗、不拾取。

只发免费的普通扫荡（``type:0``）；``type:1`` 是客户端"次数/能量用完后再来一次"的确认入口，从不发。
开始前逐项核对（任何一项读不到就不请求）：服务器记录里该关 ``passNum>0``；战力 ≥ ``Sweep`` 表该关 ``powerMin``
（登录快照不够时再读一次 ``characterHandler.getPower`` 取 p1/p2 较大者）；``sweepEnergy`` 够扣
（``ConsumeConfig[7]`` 每次 10）；与手打共用的神兽森林免费次数（``quota.of``）> 0。
每次响应里的新 ``passNum`` 用于下一次请求，与客户端 SweepProxy 一致；两次扫荡间隔 1 秒。
选项同其他副本：``levels`` + ``times``；模式在行上点按选择（扫荡 / 手打，默认扫荡）。
"""

from __future__ import annotations

from .. import daycut, dungeon, api, protocol as p, quota, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome
from . import special
from .rewards import with_idle_error

ID = "forest"
R_SWEEP = "scene.copyHandler.sweep"
R_POWER = api.R_POWER
GAP = 1.0
TYPES, BOSS = (25,), False


def record(info, mid: int):
    """服务器给的该关记录。登录数据里是 {关卡id: 记录} 或列表；扫荡响应里 dungeon 直接就是这一关的记录。"""
    history = info.get("dungeon") if isinstance(info, dict) else None
    if isinstance(history, dict):
        if state.num(history.get("id")) == mid:
            return history
        history = history.get(str(mid)) or next((r for r in history.values() if isinstance(r, dict) and state.num(r.get("id")) == mid), None)
        return history if isinstance(history, dict) else None
    return next((r for r in history or [] if isinstance(r, dict) and state.num(r.get("id")) == mid), None)


def energy_cost() -> int | None:
    row = state.row("ConsumeConfig", 7)
    if not row or row.get("consume_subType") != "sweepEnergy":
        return None
    n = state.num(row.get("consume_count"))
    return n if n and n > 0 else None


def power_min(mid: int) -> int | None:
    row = state.row("Sweep", mid)
    return state.num(row.get("powerMin")) if row and state.num(row.get("type")) == 1 else None


def current_power(ctx: TaskContext, need: int) -> int | None:
    power = state.num(ctx.info.get("power"))
    if power is None or power < need:  # 登录快照不够才查（只读）
        try:
            r = api.character_power(ctx.request)
        except p.AllianceError:
            r = {}
        known = [n for n in (power, state.num(r.get("p1")), state.num(r.get("p2"))) if n is not None]
        power = max(known) if known else None
    return power


def run(ctx: TaskContext) -> TaskResult:
    times = dungeon.read_times(ctx, ID, special.MAX_TIMES, dungeon.DEFAULT_TIMES)
    if isinstance(times, TaskResult):
        return times
    if not ctx.options.get("levels"):
        return TaskResult(ID, SKIPPED, "没有选择关卡")
    if times == 0:
        return TaskResult(ID, SKIPPED, "每关次数留空，不执行")
    levels = []
    for raw_mid in ctx.options["levels"]:
        lv = special.level_of(TYPES, BOSS, state.num(raw_mid) or 0)
        if lv is None:
            return TaskResult(ID, BLOCKED, f"{raw_mid} 不是神兽森林关卡，未执行任何关卡")
        if all(x.mid != lv.mid for x in levels):
            levels.append(lv)
    cost = energy_cost()
    energy = state.num(ctx.info.get("sweepEnergy"))
    if cost is None or energy is None or energy < 0:
        return TaskResult(ID, BLOCKED, "无法确认扫荡能量，未请求扫荡（避免消耗点券）")

    gains, parts, why, failed = dungeon.Gains(), [], "", False
    prior = dungeon.resume_done(ctx)  # 暂停前已扫的次数：从设置的次数里扣掉
    ctx.progress.update(prior)
    try:
        for lv in levels:
            before = prior.get(str(lv.mid), 0)
            if times - before <= 0:
                continue
            done, why, failed = sweep_level(ctx, lv, times - before, energy, cost, gains)
            energy = done[1]
            if done[0]:
                parts.append(f"{lv.name}×{done[0]}")
                ctx.progress[str(lv.mid)] = before + done[0]
            if why:
                why = f"{lv.name}：{why}"
                break
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"{'，'.join(parts) or '尚无完成的关卡'}；{exc}") from exc
    text = ("已扫荡 " + "、".join(parts)) if parts else ""
    if gains.items:
        text += f"；获得 {gains.text()}"
    if why:
        status = BLOCKED if failed else SUBMITTED if parts else SKIPPED
        return TaskResult(ID, status, f"{text + '；' if text else ''}停止于 {why}")
    return TaskResult(ID, SUBMITTED if parts else SKIPPED, text or "没有可用的免费次数")


def sweep_level(ctx: TaskContext, lv: dungeon.Level, times: int, energy: int, cost: int, gains: dungeon.Gains):
    """返回 ((已扫次数, 剩余能量), 停止原因, 是否失败)。"""
    rec = record(ctx.info, lv.mid)
    pass_num = state.num(rec.get("passNum")) if rec else None
    if pass_num is None or pass_num <= 0:
        return (0, energy), "服务器记录未确认已通关（passNum 未大于 0），未请求扫荡", True
    need = power_min(lv.mid)
    if need is None:
        return (0, energy), "没有客户端速通战力门槛，未请求扫荡", True
    power = current_power(ctx, need)
    if power is None or power < need:
        return (0, energy), f"当前战力{'未知' if power is None else power}，低于速通要求 {need}，未请求扫荡", True
    q = quota.of(ctx.info, lv.mid)
    if q.left is None:
        return (0, energy), f"无法确认免费次数（{q.why}），未请求扫荡", True
    spent, done = ctx.session.spent, 0
    ctx.log(f"{lv.name} 扫荡 0/{times}")
    while done < times:
        if ctx.stopped:
            return (done, energy), "已被用户停止", False
        if q.left - spent[q.key] <= 0:
            break  # 免费次数用完（神兽森林手打/扫荡共用）
        if why := daycut.reason(ctx.session):
            return (done, energy), why, False
        if energy < cost:
            return (done, energy), f"速通能量 {energy} 不足 {cost}", False
        if not ctx.sleep(GAP):
            if ctx.stopped:
                return (done, energy), "已被用户停止", False
            return (done, energy), with_idle_error(ctx, "扫荡间隔等待时连接中断"), True
        spent[q.key] += 1  # 先占用：请求结果未知时不能当作没用过
        try:
            response = ctx.request(R_SWEEP, {"mid": lv.mid, "type": 0, "passNum": pass_num})
        except p.AllianceError as exc:
            spent[q.key] -= 1  # 服务器明确拒绝：没有占用次数
            return (done, energy), f"服务器拒绝扫荡：{exc}", True
        gains.add(response, ctx.session.drain_pushes())
        left = state.num(state.find(response, "sweepEnergy", depth=6))
        energy = left if left is not None else energy - cost
        updated = record(response, lv.mid)
        new_pass = state.num(updated.get("passNum")) if updated else None
        if new_pass is not None and new_pass > 0:
            pass_num = new_pass
        done += 1
        ctx.log(f"{lv.name} 扫荡 {done}/{times}")
    return (done, energy), "", False
