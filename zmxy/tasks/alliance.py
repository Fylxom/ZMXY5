"""联盟贡献：灵魂贡献 devoteId=1，每日 1 次。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import time

from .. import protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "alliance"
READY_SECOND = 15 * 60 + 30  # 服务器 00:15 后再留 30 秒结算缓冲
SETTLEMENT_WAIT = 30
SETTLEMENT_LIMIT = 5 * 60


def _settling(exc: p.AllianceError) -> bool:
    return "联盟数据处理中" in str(exc)


def _clock(now: int) -> tuple[datetime, str]:
    server, local = datetime.fromtimestamp(now, state._CN), datetime.now(state._CN)
    return server, f"服务器 {server:%H:%M:%S}，本地 {local:%H:%M:%S}，服务器－本地 {now - int(local.timestamp()):+d} 秒"


def _wait(ctx: TaskContext, seconds: float, detail: str) -> TaskResult | None:
    ctx.log(detail)
    if ctx.sleep(seconds) and not ctx.stopped and not ctx.winding:
        return None
    stopped = ctx.stopped or ctx.winding
    return TaskResult(ID, SKIPPED if stopped else BLOCKED,
                      "等待期间用户停止，未再次提交" if stopped else "等待期间连接中断，未再次提交")


def _league_and_count(ctx: TaskContext):
    league = state.league(ctx.info)
    if league is None or not state.league_id(league):
        return None, None
    devote = ctx.config.devote_id
    count = state.league_daily_count(league, devote)
    if count is not None:
        return league, count
    # 进角响应不带次数时，客户端会先刷新联盟详情；依次尝试几种请求体
    lid = state.league_id(league)
    for body in ({"leagueId": lid}, {"id": lid}, {}):
        try:
            refreshed = state.league(ctx.request(p.ROUTE_LEAGUE_INFO, body))
        except p.AllianceError as exc:
            if _settling(exc):
                raise  # 明确处于结算期时先等待，不连续换三种请求体查询
            continue
        if refreshed is None:
            continue
        count = state.league_daily_count(refreshed, devote)
        if count is not None:
            return refreshed, count
    return league, None


def _contribute(ctx: TaskContext, refreshed_day: datetime) -> TaskResult | None:
    cfg = ctx.config
    league, count = _league_and_count(ctx)
    now = ctx.now
    if now is None:
        return TaskResult(ID, BLOCKED, "服务器未返回时间，未提交")
    if datetime.fromtimestamp(now, state._CN).date() != refreshed_day.date():
        return None  # 查询期间跨了服务器午夜：重新取当天快照并等待结算
    if league is None:
        return TaskResult(ID, SKIPPED, "当前角色没有联盟")
    if count is None:
        return TaskResult(ID, BLOCKED, "服务器未返回今日贡献次数，未提交")  # 读不到不等于 0
    if count >= cfg.daily_limit:
        return TaskResult(ID, SKIPPED, f"今日已完成（{count}/{cfg.daily_limit}）")
    spirit = state.spirit(ctx.info)
    if spirit is None:
        return TaskResult(ID, BLOCKED, "服务器未返回灵魂余额，未提交")
    if spirit < cfg.spirit_cost:
        return TaskResult(ID, SKIPPED, f"灵魂不足（{spirit:g}/{cfg.spirit_cost}）")

    if ctx.stopped or ctx.winding:
        return TaskResult(ID, SKIPPED, "用户停止，未提交")
    resp = ctx.request(p.ROUTE_DEVOTE, {"devoteId": cfg.devote_id, "password": cfg.second_password})
    updated = resp.get("self")
    if isinstance(updated, dict) and isinstance(updated.get("league"), dict):
        updated = updated["league"]
    new_count = state.league_daily_count(updated, cfg.devote_id) if isinstance(updated, dict) else None
    if new_count is None:
        return TaskResult(ID, SUBMITTED, "服务器返回成功，联盟贡献已提交")
    if new_count <= count:
        return TaskResult(ID, BLOCKED, "服务器返回成功但次数未增加，请核对")
    return TaskResult(ID, SUBMITTED, f"贡献成功，今日 {new_count}/{cfg.daily_limit}")


@task(ID, "联盟贡献", reads={p.ROUTE_LEAGUE_INFO}, writes={p.ROUTE_DEVOTE}, account_wide=True,
      defer_until=(0, 15))
def run(ctx: TaskContext) -> TaskResult:
    if ctx.config.devote_id != 1:
        return TaskResult(ID, BLOCKED, "仅允许灵魂联盟贡献 devoteId=1")
    deadline = None
    clock = ""
    while True:
        if ctx.stopped or ctx.winding:
            return TaskResult(ID, SKIPPED, "用户停止，未提交")
        if deadline is not None and time.monotonic() >= deadline:
            return TaskResult(ID, BLOCKED, f"联盟数据持续处理中，已等待 5 分钟，本次未完成；{clock}")
        try:
            ctx.refresh()  # 等待及明确拒绝后重新读取今日次数、余额，不能沿用旧快照
            now = ctx.now
            if now is None:
                return TaskResult(ID, BLOCKED, "服务器未返回时间，无法确认联盟结算已结束，未提交")
            server, clock = _clock(now)
            ready = server.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + READY_SECOND
            if now < ready:
                interrupted = _wait(ctx, ready - now, f"{clock}；等待服务器 00:15:30 后再贡献")
                if interrupted is not None:
                    return interrupted
                continue
            ctx.log(f"联盟贡献时间核对：{clock}")
            result = _contribute(ctx, server)
            if result is None:
                continue
            return replace(result, detail=f"{result.detail}；{clock}")
        except p.AllianceError as exc:
            if not _settling(exc):
                raise
            # 写请求的传输异常是 UncertainOutcome，不会进入此分支；只处理服务器明确的结算拒绝。
            if deadline is None:
                deadline = time.monotonic() + SETTLEMENT_LIMIT
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            seconds = min(SETTLEMENT_WAIT, remaining)
            interrupted = _wait(ctx, seconds, f"联盟数据仍在结算，{seconds:g} 秒后刷新今日次数再判断；{clock}")
            if interrupted is not None:
                return interrupted
