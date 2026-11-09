"""万象古树（丹熏山）：击败妖王 → 领地面掉落与宝箱 → 结算。

和噩梦关卡相似：次数是共享宝箱次数（``dungeonTreasure[1400600].remainCnt``，读不到就不创建）；
区别是创建时带 ``costGem:0``，地面掉落也走 ``getDungeonTreasureReward {i}``（不是 syncDropInfo），领完宝箱直接结算、不翻牌。
关卡来自 DungeonHelper 组类型 27。选项同其他副本：``levels`` + ``times``，没有默认关卡。
"""

from __future__ import annotations

from .. import data, dungeon, state
from . import level_pick
from ..core import Choices, TaskContext, TaskResult, task

ID = "wanxiang_tree"
MAX_TIMES = 50
HELPER_TYPE = 27


def catalog() -> list[tuple[int, str]]:
    mids = sorted(int(k) for k, r in data.table("DungeonHelper").items() if r.get("type") == HELPER_TYPE)
    return [(lv.mid, lv.name) for mid in mids if (lv := dungeon.wanxiang_level(mid))]


def _level(raw) -> dungeon.Level | None:
    mid = state.num(raw)
    return dungeon.wanxiang_level(mid or 0) if mid in {m for m, _ in catalog()} else None


@task(ID, "万象古树", writes=dungeon.ROUTES[dungeon.WANXIANG],
      choices=Choices(catalog, "关", times=True, times_max=MAX_TIMES, times_default=dungeon.DEFAULT_TIMES, limit=1))
def run(ctx: TaskContext) -> TaskResult:
    return level_pick.run_levels(ctx, ID, MAX_TIMES, _level, " 不是万象古树关卡，未执行任何关卡")
