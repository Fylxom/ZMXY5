"""快速养成：用掉当前养成周对应的两个轮换 Boss 今日剩余的免费次数。

优先使用当前数据表的 SpeedDevelop / Activity 配置。检测到线上版本比本地新时，说明本地表未跟上，
才按四周固定轮换表兜底；无法确认备用周次或关卡规则时停止，不猜测目标。

难度（行上点按切换：普通 / 噩梦，默认噩梦）：普通 = 活动 dungeonId 的第 1 个，噩梦 = 第 2 个。
同一活动的普通/噩梦共用一个免费次数（DungeonHelper 共享组，上限取组内第一条，已用 = 成员 dailyCount 之和），
读不到就不创建。
"""

from __future__ import annotations

from dataclasses import replace

from .. import dungeon, online, quota, state
from ..core import BLOCKED, SKIPPED, TaskContext, TaskResult, task

ID = "quick_develop"
LEVELS = ("普通", "噩梦")
DEFAULT_LEVEL = "噩梦"

# 数据更新失败、线上版本领先本地时使用的后备轮换；周序号对应 state.GROWTH_WEEKS。
# 每项为（普通关卡 ID，DungeonHelper 类型）；IDs 与次数组是游戏固定配置。
GROWTH_BOSSES = {
    1: ((500114, 14), (500137, 26)),  # 邪·悟空、邪·瑶瑶
    2: ((500116, 15), (500118, 16)),  # 邪·唐僧、邪·龙女
    3: ((500123, 18), (500128, 22)),  # 邪·八戒、邪·白龙
    4: ((500125, 19), (500121, 17)),  # 邪·沙僧、邪·二郎神
}


def _level(pair: tuple[int, int], helper_type: int, difficulty: str) -> dungeon.Level | str:
    mid = pair[LEVELS.index(difficulty)]
    helper, group, lv = state.row("DungeonHelper", mid), quota._helper_group(mid), dungeon.quick_level(mid)
    if not helper or state.num(helper.get("type")) != state.num(helper_type) or group is None or set(group[1]) != set(pair) or lv is None:
        return "轮换 Boss 的共享免费次数规则无法确认"
    return lv


def _table_bosses(now: int | None, difficulty: str) -> list[dungeon.Level] | str:
    """使用本地已更新的 SpeedDevelop / Activity 表找本期开放关卡。"""
    row = state.row("SpeedDevelop", 1)
    if row is None or now is None:
        return "缺少快速养成配置或服务器时间，无法确认本期活动"
    activities, types = str(row.get("xie_activityId", "")).split(";"), str(row.get("dungeonHelperType", "")).split(";")
    if len(activities) != 2 or len(types) != 2:
        return "快速养成的轮换配置不完整"
    found: list[dungeon.Level] = []
    for activity_id, helper_type in zip(activities, types):
        if not state.activity_open(activity_id, now):
            continue
        activity = state.row("Activity", activity_id)
        pair = tuple(state.num(x) for x in str((activity or {}).get("dungeonId", "")).split(";"))
        if len(pair) != 2 or any(mid is None or mid <= 0 for mid in pair):
            return "轮换 Boss 的关卡配置无效"
        level = _level(pair, state.num(helper_type), difficulty)
        if isinstance(level, str):
            return level
        found.append(level)
    return found


def _fallback_bosses(now: int | None, difficulty: str) -> list[dungeon.Level] | str:
    """本地表落后线上时，依据线上养成周使用固定关卡轮换。"""
    week = online.growth_week(now)
    if week not in GROWTH_BOSSES:
        return "无法确认当前养成周，不能确定本期快速养成 Boss"
    found: list[dungeon.Level] = []
    for normal_mid, helper_type in GROWTH_BOSSES[week]:
        level = _level((normal_mid, normal_mid + 1), helper_type, difficulty)
        if isinstance(level, str):
            return "本期快速养成 Boss 的关卡或共享免费次数配置无法确认"
        found.append(level)
    return found


@task(ID, "快速养成", writes=dungeon.ROUTES[dungeon.QUICK],
      modes=tuple((d, d) for d in LEVELS), mode_default=DEFAULT_LEVEL)
def run(ctx: TaskContext) -> TaskResult:
    difficulty = ctx.options.get("mode") or DEFAULT_LEVEL
    if difficulty not in LEVELS:
        return TaskResult(ID, BLOCKED, "难度无效，未创建副本")
    fallback = online.newer_online() is not None
    prefix = "本地表未跟上线上版本，已启用四周轮换备用配置；" if fallback else ""
    levels = (_fallback_bosses if fallback else _table_bosses)(ctx.now, difficulty)
    if isinstance(levels, str):
        return TaskResult(ID, BLOCKED, f"{prefix}{levels}，未创建副本")
    if not levels:
        return TaskResult(ID, SKIPPED, f"{prefix}本期快速养成 Boss 活动未开放")
    left = {}
    for lv in levels:
        q = quota.of(ctx.info, lv.mid)
        if q.left is None:
            return TaskResult(ID, BLOCKED, f"{prefix}无法确认 {lv.name} 的免费次数（{q.why}），未创建副本")
        left[lv.mid] = int(q.left)
    levels = [lv for lv in levels if left[lv.mid] > 0]
    if not levels:
        return TaskResult(ID, SKIPPED, f"{prefix}今日免费次数已用完")
    result = dungeon.run_levels(ctx, ID, levels, lambda lv: left[lv.mid])
    return replace(result, detail=prefix + result.detail) if prefix else result
