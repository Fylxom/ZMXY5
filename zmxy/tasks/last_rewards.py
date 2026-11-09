"""上届奖励（周常页）：分别确认竞技场和斗宠大会的上届奖励状态，再领取尚未领取的排名奖励。

两个入口是同一个请求，只有路由不同：``scene.arenaHandler.getReward`` / ``scene.petArenaHandler.getReward``，参数为空。
响应的 ``give`` 为空 = 上届没有奖励（客户端提示“上届没有奖励”）；有奖励时 ``give`` 里的 ``type="prevRank"``、``count`` 是上届排名，客户端领奖弹窗用它显示排名。

界面“自动配置”会顺便读领取状态：客户端按 ``everyWeekSetting.arenaSetting|petArenaSetting.rewardTime`` > 0 把“领取”按钮置灰，
所以 > 0 = 已领取，否则 = 未领取（不代表一定有奖励）。进角信息里没有这段就读一次 ``getArenaInfo``。
领取前分不出“没有奖励”和“有奖励没领”，所以执行后把每个入口的结果（领到 / 无奖励）及响应里读到的上周排名记进存档 profile 的 ``last_rewards_out``（带周序号），
任务结果日志保留排名；周常行的领取状态摘要通过 ``describe`` 显示，但不显示排名。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from .. import dungeon, api, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

ID = "last_rewards"
R_ARENA = "scene.arenaHandler.getReward"
R_PET = "scene.petArenaHandler.getReward"
R_INFO_ARENA = api.R_ARENA_INFO
R_INFO_PET = api.R_PET_ARENA_INFO


class _Entry(NamedTuple):
    name: str   # 任务结果里的名字
    short: str  # 周常行摘要和 profile 里的名字
    route: str
    info_route: str
    setting: str


ENTRIES = (_Entry("竞技场", "竞技场", R_ARENA, R_INFO_ARENA, "arenaSetting"),
           _Entry("斗宠大会", "斗宠", R_PET, R_INFO_PET, "petArenaSetting"))
OUT_KEY = "last_rewards_out"  # 存档 profile 里记本周执行结果：week、竞技场/斗宠状态，以及可选的 竞技场_rank / 斗宠_rank
GOT, NONE, CLAIMED = "got", "none", "claimed"  # 领到 / 无奖励 / 服务器说“奖励已领取”（之前就领过，有没有奖励不知道）
OUTCOME_TEXT = {GOT: "已领取", NONE: "无奖励", CLAIMED: "已领取"}


_CN = timezone(timedelta(hours=8))


def week_key(now: int | None = None) -> str:
    """本周的周序号（北京时间，周一开始），如 ``2026-45``。now = 服务器时间（秒）；
    界面没连着服务器时传 None，用上次进角学到的服务器时钟（servertime）。"""
    from .. import servertime
    return datetime.fromtimestamp(servertime.now() if now is None else now, _CN).strftime("%G-%V")


def claimed(ctx: TaskContext, entry: _Entry) -> bool | None:
    """这一届奖励是不是已经领过；读不到 = None。"""
    setting = state.section(ctx.info, entry.setting)
    if setting is None:
        try:
            setting = state.section(api.arena_info(ctx.request, pet=entry.info_route == R_INFO_PET), entry.setting)
        except p.AllianceError:
            return None
    timestamp = state.num(setting.get("rewardTime", 0)) if setting is not None else None
    return timestamp > 0 if timestamp is not None and timestamp >= 0 else None


def previous_rank(response) -> int | None:
    """getReward 的 give 项 ``{type: "prevRank", count}`` 是客户端显示的上届名次。"""
    if not isinstance(response, dict) or not isinstance(response.get("give"), list):
        return None
    for item in response["give"]:
        if isinstance(item, dict) and item.get("type") == "prevRank":
            rank = state.num(item.get("count"))
            return rank if rank is not None and rank > 0 else None
    return None


def describe(states: dict[str, bool | None], outcome: dict | None = None) -> tuple[str, bool | None]:
    """（显示文字, 是否还有没领的）。本周执行过的结果（outcome）优先，其次服务器的领取状态；都没有 = 未读取。
    是否还有没领的：True/False，任一入口未知 = None。"""
    outcome = outcome or {}
    parts, unclaimed = [], []
    for name in (e.short for e in ENTRIES):
        if outcome.get(name) in OUTCOME_TEXT:
            parts.append(f"{name} {OUTCOME_TEXT[outcome[name]]}")
            unclaimed.append(False)
            continue
        got = states.get(name)
        parts.append(f"{name} {'未读取' if got is None else '已领取' if got else '未领取'}")
        unclaimed.append(None if got is None else not got)
    pending = True if any(value is True for value in unclaimed) else None if any(value is None for value in unclaimed) else False
    return " · ".join(parts), pending


def auto_status(ctx: TaskContext) -> tuple[str, bool | None, dict[str, bool | None], str]:
    """界面“自动配置”：（显示文字, 是否还有没领的, 各入口的领取状态, 按服务器时间算的周序号）；界面再按本周执行结果重算文字，见 describe。"""
    states = {e.short: claimed(ctx, e) for e in ENTRIES}
    return (*describe(states), states, week_key(ctx.now))


@task(ID, "上届奖励", reads={R_INFO_ARENA, R_INFO_PET}, writes={R_ARENA, R_PET}, group="周常", auto=auto_status)
def run(ctx: TaskContext) -> TaskResult:
    got, none, failed, already = [], [], [], []
    unprocessed: list[str] = []
    outcome: dict[str, object] = {}
    try:
        for index, entry in enumerate(ENTRIES):
            name, short = entry.name, entry.short
            received = None if ctx.stopped else claimed(ctx, entry)
            if ctx.stopped:
                unprocessed.extend(e.name for e in ENTRIES[index:])
                break
            if received is None:
                failed.append(f"{name}（无法确认上届奖励领取状态，未领取）")
                continue
            if received:
                already.append(name)
                outcome[short] = CLAIMED
                continue
            gains = dungeon.Gains()
            try:
                resp = ctx.request(entry.route, {})
                gains.add(resp, ctx.session.drain_pushes())
            except p.AllianceError as exc:
                if "已领取" in str(exc):  # 这届奖励之前领过：不是失败
                    already.append(name)
                    outcome[short] = CLAIMED
                    continue
                failed.append(f"{name}({exc})")
                continue
            rank = previous_rank(resp)
            if rank is not None:
                outcome[f"{short}_rank"] = rank
            has_reward = bool(gains.items) or rank is not None
            rank_note = f"（上周排名第 {rank} 名）" if rank is not None else ""
            if has_reward:
                got.append(f"{name}{rank_note}{dungeon.gain_note(gains)}")
                outcome[short] = GOT
            else:
                none.append(name)
                outcome[short] = NONE
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"上届奖励：已领 {'、'.join(got) or '无'}；{exc}") from exc
    text = "；".join(x for x in (
        f"已领取 {'；'.join(got)}" if got else "",
        f"{'、'.join(none)}上届没有奖励" if none else "",
        f"{'、'.join(already)}之前已领取" if already else "",
        f"领取被拒绝：{'、'.join(failed)}" if failed else "",
        f"已被用户停止，未处理 {'、'.join(unprocessed)}" if unprocessed else "",
    ) if x) or "已被用户停止"
    profile = {OUT_KEY: {**outcome, "week": week_key(ctx.now)}} if outcome else None  # 界面按存档合并进本周记录
    partial = bool(unprocessed and (got or none or already))
    if failed or partial:
        return TaskResult(ID, BLOCKED, text, profile=profile)
    return TaskResult(ID, SUBMITTED if got else SKIPPED, text, profile=profile)
