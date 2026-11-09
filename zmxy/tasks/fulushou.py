"""福禄寿（福星 / 禄星 / 寿星）小游戏：创建单人关卡后按 100 分结算。

客户端在第二个战斗连接里玩小游戏，这里不模拟，只提交与客户端一致的结算（``sceneBuffScore=100``）；
如果服务器校验战斗数据就会拒绝，该次不计成功、不重发。次数 = ``dailyCountMax + VIP 额外次数 - dailyCount``
（每个星各自独立，VIP3 起各多 1 次），读不到就不创建。选项与主线相同：``levels`` + ``times``（没有默认关卡）。
界面顺序放在日常组最前面（旧版执行器也把福禄寿放在最前）。
"""

from __future__ import annotations

from .. import dungeon, state
from . import level_pick
from ..core import Choices, TaskContext, TaskResult, task

ID = "fulushou"
SCORE = 100
MAX_TIMES = 50
PREFIX = "福禄寿—"


def levels() -> list[dungeon.Level]:
    out = []
    for key in sorted(int(k) for k in state.table("Dungeon")):
        row = state.row("Dungeon", key)
        if str(row.get("name", "")).startswith(PREFIX) and (lv := dungeon.plain_level(key, dungeon.SPECIAL, score=SCORE)):
            out.append(dungeon.Level(lv.mid, lv.kind, lv.name[len(PREFIX):], lv.rooms, lv.score))
    return out


def _level(raw) -> dungeon.Level | None:
    return {lv.mid: lv for lv in levels()}.get(state.num(raw))


@task(ID, "福禄寿", writes=dungeon.ROUTES[dungeon.SPECIAL], first=True,
      choices=Choices(lambda: [(lv.mid, lv.name) for lv in levels()], "关", times=True, times_max=MAX_TIMES))
def run(ctx: TaskContext) -> TaskResult:
    return level_pick.run_levels(ctx, ID, MAX_TIMES, _level, " 不是福星/禄星/寿星关卡，未执行任何关卡")
