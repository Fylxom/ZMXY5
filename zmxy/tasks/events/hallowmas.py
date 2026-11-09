"""万圣活动：自动刷已解锁的最新魔女主线，只领水晶球，累计到 11 个。

魔女一场掉两个球，累计到 10 个后不再出现；要到 11 只能走奇数：累计为偶数时领 1 个，奇数时领 2 个。
每场后刷新核对累计数，对不上就停，不重发。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from ... import data, dungeon, protocol as p, quota, state
from ...core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

ID = "hallowmas"
CRYSTAL = 101098027
R_CLOSE = "scene.activityHandler.setActivityClose"
MAX_RUNS = 300


def activity_id():
    return state.num((data.row("ActivityCommon", 1) or {}).get("hallowmasId"))


def opened(now):
    try:
        ident = activity_id()
        return ident is not None and state.activity_open(ident, now)
    except data.DataError:
        return False


def catalog():
    rows = data.table("DungeonActivity")
    return [(mid, name) for mid, name in dungeon.catalog(dungeon.MAIN)
            if (row := rows.get(str(mid)) or {}).get("isDungeonActivity") == 1 and row.get("probability", 0) > 0]


def latest_level(info):
    """从最新主线往前，找可进入且支持魔女的关卡。"""
    levels = [n for key in ("p1", "p2") if isinstance(role := info.get(key), Mapping)
              if (n := state.num(role.get("level"))) is not None and n > 0]
    if not levels:
        raise dungeon.Stop("角色等级缺失或异常，无法自动选关")
    for mid, _ in sorted(catalog(), reverse=True):
        config = data.row("DungeonActivity", mid) or {}
        need = state.num(config.get("rolelevel"))
        if need is None or need < 0:
            raise dungeon.Stop("魔女活动等级配置异常")
        if max(levels) < need:
            continue
        if why := quota.locked(info, mid):
            if "未通关" in why or why.startswith("角色等级不足"):
                continue
            raise dungeon.Stop(f"无法自动确认最新关卡解锁状态：{why}")
        lv, monster = dungeon.level(mid), state.num(config.get("monsterID"))
        if lv is None or lv.kind != dungeon.MAIN or monster is None or config.get("dropId") != "5016,5017":
            raise dungeon.Stop("魔女关卡配置异常")
        return lv, monster
    raise dungeon.Stop("没有已解锁且满足活动等级要求的魔女主线关卡")


def snapshot(ctx, ident):
    """场外刷新，返回 (累计数, 是否关闭, 日期)；缺字段不推算，跨日不延用领取计划。"""
    ctx.refresh()
    if ctx.now is None or not opened(ctx.now):
        raise dungeon.Stop("活动未开放或缺少服务器时间")
    if state.num((data.row("Common", 1) or {}).get("dungeonActivityMax")) != 10:
        raise dungeon.Stop("每日水晶球上限配置已变化，未套用11个领取规则")
    count = state.used(ctx.info, "dungeonActivityCnt")
    if count is None or count > 11:
        raise dungeon.Stop("每日水晶球累计数缺失或异常")
    other = ctx.info.get("activityOther")
    if not isinstance(other, Mapping):
        raise dungeon.Stop("缺少万圣活动参与状态")
    closed = state.num(other.get(str(ident), 0))
    if closed not in (0, 1):
        raise dungeon.Stop("万圣活动参与状态异常")
    return count, closed, datetime.fromtimestamp(ctx.now, state._CN).date()


class CrystalPicker:
    """dungeon 的 keep_drop：只拾魔女水晶球，一场最多一组两个。"""

    def __init__(self, count, monster):
        self.limit = 1 if count % 2 == 0 else 2
        self.monster = monster
        self.selected = set()
        self.taken = 0
        self.seen = 0

    def prepare(self, entries):
        crystals = [drop for _, drop in entries
                    if any(state.num(drop.get(key)) == CRYSTAL for key in ("subType", "itemId", "propId"))]
        if any(str(drop.get("type")).lower() != "item" or state.num(drop.get("monsterId")) != self.monster
               or state.num(drop.get("count")) != 1 for drop in crystals):
            raise dungeon.Stop("魔女水晶球掉落格式变化，未领取")
        if crystals and (len(crystals) != 2 or self.seen):
            raise dungeon.Stop("本场魔女水晶球不是一组两个，未继续领取")
        self.seen += len(crystals)
        self.selected = {id(drop) for drop in crystals[:self.limit]}
        self.taken += len(self.selected)

    def __call__(self, drop):
        return id(drop) in self.selected


@task(ID, "万圣活动", writes=dungeon.ROUTES[dungeon.MAIN] | {R_CLOSE},
      group="活动", window=opened, first=True)
def run(ctx: TaskContext) -> TaskResult:
    runs = 0
    try:
        ident = activity_id()
        count, closed, day = snapshot(ctx, ident)
        if count == 11:
            return TaskResult(ID, SKIPPED, "今日已确认获得11个水晶球")
        if count == 10:
            raise dungeon.Stop("今日已达10个，魔女不再出现，无法补至11个")
        lv, _ = latest_level(ctx.info)
        ctx.log(f"万圣活动自动选关：{lv.name}（{lv.mid}），只领取水晶球")
        if ctx.stopped:
            return TaskResult(ID, SKIPPED, "用户停止，未开始挑战")
        if closed:
            ctx.request(R_CLOSE, {"id": ident, "state": 0})
            if snapshot(ctx, ident) != (count, 0, day):
                raise dungeon.Stop("开启魔女活动后状态未确认，未开始挑战")
        gains = dungeon.Gains()
        while count < 11 and runs < MAX_RUNS:
            if ctx.stopped or ctx.winding:
                status = BLOCKED if runs else SKIPPED
                return TaskResult(ID, status, f"用户停止；今日水晶球{count}/11，已结算{runs}场")
            lv, monster = latest_level(ctx.info)
            picker = CrystalPicker(count, monster)
            ctx.log(f"万圣活动：{lv.name}，今日水晶球{count}/11，第{runs + 1}场")
            ctx.engage(True)
            try:
                done, issue = dungeon.run_level(replace(ctx, options={}), lv, 1, gains, keep_drop=picker)
            finally:
                ctx.engage(False)
            runs += done
            if issue:
                raise dungeon.Stop(issue)
            try:
                fresh, closed, fresh_day = snapshot(ctx, ident)
            except (dungeon.Stop, p.AllianceError, data.DataError) as exc:
                raise UncertainOutcome(f"水晶球领取后的状态无法核对：{exc}；停止本账号后续任务，不重发") from exc
            if fresh_day != day or closed or fresh != count + picker.taken:
                raise UncertainOutcome(f"领取后累计数未确认：原{count}，本场计划{picker.taken}，服务器{fresh}；停止本账号后续任务，不重发")
            count = fresh
        if count != 11:
            raise dungeon.Stop(f"已达单次{MAX_RUNS}场上限，今日水晶球{count}/11；请检查{lv.name}魔女掉落")
        return TaskResult(ID, SUBMITTED, f"今日已确认获得11个水晶球；{lv.name}结算{runs}场")
    except (dungeon.Stop, p.AllianceError, data.DataError) as exc:
        return TaskResult(ID, BLOCKED, f"万圣活动停止：{exc}；已结算{runs}场")
