"""同物归库：把背包里和仓库已有的同种道具合并存进仓库（仓库界面的“同物归库/同步”按钮，operateType=1）。

不是界面上的任务（不用 ``@task`` 注册，没有开关）：日常页底部的“同物归库”开关打开后，由 ``runner._AccountRun`` 在任务之间调用。
服务器返回 ``moved``（合并的道具 [{id, count}]），为空 = 没有相同道具。
"""

from __future__ import annotations

from collections.abc import Mapping

from .. import dungeon
from ..core import SKIPPED, SUBMITTED, TaskContext, TaskResult, TaskSpec

ID = "同物归库"
ROUTE = "scene.recordHandler.syncBagAndWarehouse"
BAG_TO_WAREHOUSE = 1  # operateType：1 = 背包合并至仓库，2 = 仓库合并至背包


def run(ctx: TaskContext) -> TaskResult:
    resp = ctx.request(ROUTE, {"operateType": BAG_TO_WAREHOUSE})
    moved = resp.get("moved") if isinstance(resp, Mapping) else None
    if not moved:
        return TaskResult(ID, SKIPPED, "无相同道具")
    parts = []
    for item in moved:
        if not isinstance(item, Mapping):
            continue
        parts.append(f"{dungeon.item_name(item.get('id')) or '物品' + str(item.get('id'))}x{item.get('count')}")
    return TaskResult(ID, SUBMITTED, "已将下列道具合并至仓库：" + "、".join(parts))


SPEC = TaskSpec(ID, ID, run, writes=frozenset({ROUTE}))
