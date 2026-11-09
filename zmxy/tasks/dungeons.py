"""主线 / 隐藏 / 精英 / 噩梦关卡。三个任务共用 ``zmxy/dungeon.py`` 的流程。

选项（账号的 options[任务 id]）：``levels`` 关卡编号列表，``times`` 每关几次；精英另有 ``stones_only``（只捡强化石）；主线另有 ``no_clear``（失败结算，通关次数不增加）、``surface_only``（只打小怪房，不进 Boss 房）。
没选关卡或次数留空 = 不执行；没有默认关卡。主线/精英从第一关往后打（不按勾选顺序）；后一关靠刚打通的前一关解锁时，重新进入角色确认后照打。噩梦只能选 1 关（``Choices.limit``）。次数上限：主线 999，隐藏/精英/噩梦 99。
精英/噩梦的次数还受服务器给的免费次数限制，用完即止（噩梦所有关合计共用一个次数池）。
"""

from __future__ import annotations

from .. import dungeon
from ..core import BLOCKED, Choices, TaskContext, TaskResult, task
from .events import festival


def _register(kind: str) -> None:
    task_id = f"dungeon_{kind}"
    choices = Choices(lambda: dungeon.catalog(kind), "关", times=True,
                      times_max=dungeon.MAX_TIMES[kind], times_default=dungeon.DEFAULT_TIMES,
                      toggles=(("stones_only", "只捡强化石"),) if kind == dungeon.ELITE
                      else (("surface_only", "只刷表层"), ("no_clear", "不要通关")) if kind == dungeon.MAIN
                      else (("no_clear", "不要通关"),) if kind == dungeon.HIDDEN
                      else (("key_gem_box", "宝石盒概率≥2/3用钥匙"),) if kind == dungeon.NIGHTMARE else (),
                      caption="每日万能强化石：勾日常任务即可" if kind == dungeon.ELITE else "",
                      note=("只刷表层：不进 Boss 房，照样算通关。\n" if kind == dungeon.MAIN else "")
                           + ("不要通关：按失败结算，通关次数不涨，没解锁的下一关不会因此解锁。" if kind in (dungeon.MAIN, dungeon.HIDDEN) else ""),
                      limit=1 if kind == dungeon.NIGHTMARE else 0)  # 噩梦只能选 1 关（用户指定）

    writes = dungeon.ROUTES[kind] | ({dungeon.R_USE_ITEM} if kind in (dungeon.MAIN, dungeon.ELITE) else set())

    @task(task_id, f"{dungeon.KIND_NAME[kind]}关卡", writes=writes, group="副本", choices=choices)
    def run(ctx: TaskContext) -> TaskResult:
        if choices.limit and len(set(ctx.options.get("levels") or ())) > choices.limit:
            return TaskResult(task_id, BLOCKED, f"{dungeon.KIND_NAME[kind]}关卡最多选 {choices.limit} 关，请重新选择；未执行任何关卡")
        stats, items, issue = festival.dungeon_bonuses(ctx, task_id)
        if issue:
            ctx.log(f"盛典关卡道具未执行：{issue}")
        return dungeon.run_kind(ctx, task_id, kind, battle_stats=stats, battle_items=items)


for _kind in (dungeon.MAIN, dungeon.HIDDEN, dungeon.ELITE, dungeon.NIGHTMARE):  # 注册顺序 = 界面顺序
    _register(_kind)
