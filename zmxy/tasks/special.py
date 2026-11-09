"""九宫玄塔 / 十殿阎罗 / 神兽森林（手打；扫荡见 forest_sweep.py）/ 天树王：DungeonHelper 共享次数组里的单人副本。

关卡全部来自 ``DungeonHelper`` 表的组类型（九宫 = 1/8/13/21，十殿阎罗 + 五毒炼狱 = 3，神兽森林 = 25，天树王 = 4）。
同一组共用每日免费次数（``quota.of``：上限 − 全部成员 dailyCount 之和），所以选很多关也只会打到次数用完；
读不到就不创建。选项与主线/精英相同：``levels`` + ``times``（每关最多几次，留空不执行，没有默认关卡）。

流程各不相同的只有房间：普通九宫玄塔要依次走完层内的传送门房间（``dungeon.tower_route``，最后一间结算），
天树王要先进入各自的 Boss 房（与主线一致），其余都是一间房：创建 → 拾取 → 结算，没有翻牌。
"""

from __future__ import annotations

from .. import data, dungeon, state
from . import level_pick
from ..core import Choices, TaskContext, TaskResult, task

MAX_TIMES = 50
NINE_TOWER_NORMAL = 1  # DungeonHelper.type：普通九宫玄塔，要走传送门房间

# (任务 id, 标题, DungeonHelper 组类型, 是否带 Boss 房)
GROUPS = (
    ("nine_tower", "九宫玄塔", (1, 8, 13, 21), False),
    ("ten_yama", "十殿阎罗", (3,), False),
    ("forest", "神兽森林", (25,), False),
    ("tree_king", "天树王", (4,), True),
)


@data.cached
def members(types: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(sorted(int(k) for k, r in data.table("DungeonHelper").items() if r.get("type") in types))


def level_of(types: tuple[int, ...], boss_room: bool, mid: int) -> dungeon.Level | None:
    if mid not in members(types):
        return None
    if (data.row("DungeonHelper", mid) or {}).get("type") == NINE_TOWER_NORMAL:
        route = dungeon.tower_route(mid)
        return dungeon.plain_level(mid, dungeon.SPECIAL, route) if route else None
    boss = dungeon._boss_room(mid) if boss_room else None
    return dungeon.plain_level(mid, dungeon.SPECIAL, (mid, boss) if boss else None)


def short_name(name: str) -> str:
    """选择框里的短名：普通九宫玄塔一层 → 普通·一层，精英九宫玄塔-第1层 → 精英·1层，四象宝塔-持国天王 → 四象·持国。"""
    if name.startswith("九宫玄塔"):
        floor = name.removeprefix("九宫玄塔").removesuffix("层")
        if len(floor) != 1 or floor not in "一二三四五六七八九":  # 数据表更新后出现认不出的层名：原样显示，别让界面崩
            return name
        return f"普通·{'一二三四五六七八九'.index(floor) + 1}层"
    for prefix, label in (("精英九宫玄塔-第", "精英·"), ("四象宝塔-", "四象·"), ("七宝玲珑塔-托塔天王·", "七宝·")):
        if name.startswith(prefix):
            return label + name.removeprefix(prefix).removesuffix("天王")
    return name


def catalog(types: tuple[int, ...], boss_room: bool) -> list[tuple[int, str]]:
    levels = [lv for mid in members(types) if (lv := level_of(types, boss_room, mid))]
    return [(lv.mid, lv.name.removeprefix("天树王·") if boss_room else short_name(lv.name)) for lv in levels]


def run_group(ctx: TaskContext, task_id: str, types: tuple[int, ...], boss_room: bool) -> TaskResult:
    return level_pick.run_levels(ctx, task_id, MAX_TIMES, lambda raw: level_of(types, boss_room, state.num(raw) or 0),
                                 " 不是该任务的可挑战关卡，未执行任何关卡")


def register(group_id: str) -> None:
    """注册一个任务。注册顺序 = 界面顺序（神兽森林·扫荡、万象古树要排在天树王之前，所以天树王单独在 tree_king.py 注册）。"""
    task_id, title, types, boss_room = next(g for g in GROUPS if g[0] == group_id)
    choices = Choices(lambda: catalog(types, boss_room), "关", times=True, times_max=MAX_TIMES, times_default=dungeon.DEFAULT_TIMES, limit=1)
    forest = task_id == "forest"
    reads = {dungeon.R_SELECT_MONSTER} if forest else set()  # 神兽森林要先查本次出怪才能拾取
    writes = set(dungeon.ROUTES[dungeon.SPECIAL])
    if forest:  # 神兽森林有两种模式：扫荡（默认）/ 手打，行上点按选择
        from . import forest_sweep

        reads |= {forest_sweep.R_POWER}
        writes |= {forest_sweep.R_SWEEP}

    @task(task_id, title, reads=reads, writes=writes, choices=choices,
          modes=(("manual", "手打"), ("sweep", "扫荡")) if forest else (), mode_default="sweep" if forest else "")
    def run(ctx: TaskContext) -> TaskResult:
        if forest and ctx.options.get("mode", "sweep") != "manual":
            return forest_sweep.run(ctx)
        return run_group(ctx, task_id, types, boss_room)


for _group_id in ("nine_tower", "ten_yama", "forest"):
    register(_group_id)
