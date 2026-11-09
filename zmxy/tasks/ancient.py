"""上古蛮荒（周常页）：createSinglePlayerCopy 创建 → 领首领宝箱 ``getDungeonTreasureDrop`` / ``getDungeonTreasureReward {i}`` → 结算，不翻牌。

参照提取出的 ``manhuang.py``：固定走多人版：关卡 1400101–1400113 也是直接用 createSinglePlayerCopy 创建的（不提供单人版）。次数是共享宝箱次数（``dungeonTreasure[1400100].remainCnt``，读不到就不创建）。
选项同其他副本：选关 + 每关次数（没有“自动选最高关”）。入口 490 不做。
“上古蛮荒”定时（globalcfg）执行时选项里带 ``plan``（关卡用周常页上选的），次数不用填，进角后按现有次数现算：
``"all"`` = 有几次打几次；``"growth"`` = 按装备宝石养成周留次数（见 growth_keep）。
"""

from __future__ import annotations

from dataclasses import replace

from .. import dungeon, online, quota, state
from . import level_pick
from ..core import BLOCKED, SKIPPED, Choices, TaskContext, TaskResult, task

ID = "ancient_wilderness"
MAX_TIMES = 10  # 次数输入框的上限（每关次数）
RESERVE_WEEK = 1  # 次数留给装备宝石养成周（state.GROWTH_WEEKS 的序号）
DAILY_GAIN = 2  # 每天 0 点恢复 2 次，最多 10 次
FIRST_LEVEL, FIRST_MAP_END, LAST_LEVEL = 1400101, 1400108, 1400113  # 第一图 荒（1400101–08），第二图 异（1400109–13）


def catalog() -> list[tuple[int, str]]:
    """多人版关卡 1400101–1400113。"""
    levels = [lv for mid in range(FIRST_LEVEL, LAST_LEVEL + 1) if (lv := dungeon.ancient_level(mid))]
    return [(lv.mid, ("荒·" if lv.mid <= FIRST_MAP_END else "异·") + lv.name) for lv in levels]


def auto_times(ctx: TaskContext) -> int | None:
    """界面“自动配置”：共享宝箱次数（进角信息 dungeonTreasure 的 remainCnt）；读不到不改。"""
    left = quota.of(ctx.info, FIRST_LEVEL).left
    return None if left is None else int(min(left, MAX_TIMES))


def growth_keep(now: int | float | None, live: int | None) -> int | None:
    """全局“按养成周”：今天要留下几次（0 = 有几次打几次）；None = 装备宝石周开始那天游戏还没更新，一次都不打。
    装备宝石周开始前 4 天按 state.growth_reserve 逐天多留（开始那天正好攒满 10 次）；表里上一周已经在 0 点结束、
    线上还没切到装备宝石周 = 等更新（读不到线上版本也按等更新算，宁可不打）。周内和平时都全打，周内每天恢复的次数当天用掉。"""
    return state.growth_keep(now, live, RESERVE_WEEK, DAILY_GAIN)


def _planned(ctx: TaskContext, plan: str) -> TaskContext | TaskResult:
    """全局任务：按现有次数和计划算出这次打几次，填进 times。"""
    left = quota.of(ctx.info, FIRST_LEVEL).left
    if left is None:
        return TaskResult(ID, BLOCKED, "读不到上古蛮荒剩余次数，未执行")
    keep = 0
    if plan == "growth":
        now = ctx.now
        keep = growth_keep(now, online.growth_week(now))
        if keep is None:
            return TaskResult(ID, SKIPPED, f"装备宝石养成周今天开始，游戏还没更新，{int(left)} 次全部留着")
    times = min(int(left) - keep, MAX_TIMES)
    if times <= 0:
        return TaskResult(ID, SKIPPED, f"现有 {int(left)} 次，今天要留 {keep} 次，不打" if keep else "今天没有次数")
    ctx.log(f"现有 {int(left)} 次" + (f"，留 {keep} 次给装备宝石养成周" if keep else "") + f"，打 {times} 次")
    return replace(ctx, options={**ctx.options, "times": times})


def _level(raw) -> dungeon.Level | None:
    mid = state.num(raw) or 0
    return dungeon.ancient_level(mid) if FIRST_LEVEL <= mid <= LAST_LEVEL else None


@task(ID, "上古蛮荒", writes=dungeon.ROUTES[dungeon.ANCIENT], group="周常",
      times_max=MAX_TIMES, times_default=dungeon.DEFAULT_TIMES, choices=Choices(catalog, "关", limit=1), auto=auto_times, reserve=1, reserve_gain=2)  # 次数留给装备宝石养成周；每天涨 2 次、最多 10 次
def run(ctx: TaskContext) -> TaskResult:
    plan = ctx.options.get("plan")
    if plan is not None:
        if plan not in ("all", "growth"):
            return TaskResult(ID, BLOCKED, f"未知的次数计划 {plan}，未执行")
        planned = _planned(ctx, plan)
        if isinstance(planned, TaskResult):
            return planned
        ctx = planned
    return level_pick.run_levels(ctx, ID, MAX_TIMES, _level, " 不是上古蛮荒关卡，未执行任何关卡")
