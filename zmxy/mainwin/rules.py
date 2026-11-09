"""主窗口的分页规则和常量：哪个任务出现在哪一页、定时页列哪些任务。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, Mapping

from .. import core, results
from ..tasks import boss_raid, fairy_flag_shop, last_rewards, limited_shop, uid_lookup


TITLE = "造梦西游5 日常助手"
GROUP_ORDER = ("日常", "副本", "其他", "活动", "已结束")  # “活动/已结束”是活动页里按“识别”结果分的两块
WEEKLY = "周常"  # 单独一页：group="周常" 的任务出现在“周常”页，group="活动" 的在“活动”页，其余在“日常”页
LIMITED = "活动"
DISPLAY_ORDER = tuple(  # 周常界面交换世界Boss与上届奖励的位置；执行顺序仍由 core 决定
    boss_raid.WORLD_ID if tid == last_rewards.ID else last_rewards.ID if tid == boss_raid.WORLD_ID else tid
    for tid in core.DISPLAY_ORDER
)
DISPLAY_RANK = {tid: i for i, tid in enumerate(DISPLAY_ORDER)}
SAVE_END = results.SAVE_END  # 存档 profile 里的“这个存档做完了”标记 → 任务 id
ALWAYS_LISTED = ("double_drop", "online_welfare")  # 周期活动常驻活动页，不因识别时档期关闭而归入“已结束”
BOSSES = ("union_boss",)  # 联盟Boss：界面隐藏（时间由盟主设定，读不到）
TONGTIAN = ("tongtian_hire", "tongtian_escort", "tongtian_consult")  # 通天河：报名雇佣、护送经书、请教
HIDDEN = ("garden",) + BOSSES  # 界面上不显示的：药园、联盟Boss
OPEN_HOURS = ("pvp_match", "pet_brawl") + TONGTIAN + ("arena_apply", "pet_arena_apply")  # 周常页“有开放时间”那一块（定时在定时页，规则见 timeplan）
WEEKLY_BOTTOM = (uid_lookup.ID,)  # 周常页最底下单独一块：和定时无关的查询
READERS = ("limited:read", "weekly:auto", "weekly:auto_run", "time_boat:read")  # exec_btns 里要自己登录的只读按钮：账号在执行时一律不能点


def shown_name(name: str) -> str:
    """执行项的内部名带“账号级”，日志里显示时去掉。"""
    return name.replace(" · 账号级", "")


def weekly_auto_run_ids(weekly_run_ids: Iterable[str], counts: Mapping[str, str], reward_pending: bool) -> list[str]:
    """自动配置后要执行的周常：次数大于 0、待领取上届奖励，以及按自身库存/余额判断的限时抢购和妖幡卷（没选商品就跳过）。"""
    ids = []
    for tid in sorted(weekly_run_ids, key=core.display_rank):
        if tid == last_rewards.ID:
            if reward_pending:
                ids.append(tid)
        elif tid in (limited_shop.ID, fairy_flag_shop.ID):  # 不设次数框；任务内部按库存、账号限购和灵魂余额决定购买量
            ids.append(tid)
        elif tid in counts:
            text = counts[tid].strip()
            if text.isascii() and text.isdigit() and int(text) > 0:  # 次数 0 / 留空 = 不执行
                ids.append(tid)
    return ids


@dataclass(eq=False)
class Job:
    """一次在后台线程里的执行（任务或只读登录）。各登录账号各跑各的：按钮只在它占着的账号上变灰。"""
    users: frozenset[str]                 # 占着的登录账号
    stop: threading.Event | None = None   # 任务执行的停止标志；None = 只读登录（识别、自动配置、读取）
    timed: frozenset[str] = frozenset()   # 每日定时开始的执行：它的执行项名字，结果另外记进定时记录
