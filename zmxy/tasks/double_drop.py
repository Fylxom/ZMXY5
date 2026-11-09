"""活动页：开始时领取双倍掉宝率首档和当前开放的节日双倍掉宝 buff。

这里在开始时领取 Activity 230 的首档双倍掉宝率，供后续在线奖励解锁；
在线福利任务会在队列末尾补领仍未领取的首档并领取后续奖励。节日 buff 则按 Activity 表扫描并检查今日领取次数。
"""

from __future__ import annotations

from .. import dungeon, protocol as p, quest, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

ID = "double_drop"
ROUTE_HOLIDAY = "scene.activityHandler.getResetActivityReward"
ONLINE_ACTIVITY = 230
HOLIDAY_MARK = "手动领取buff"
ONLINE_CLAIMED = "double_drop.online_first_claimed"
ONLINE_NAME = "在线福利双倍掉宝率"


def weekdays(row) -> set[int]:
    return {n for x in str((row or {}).get("weekDay", "")).split(";") if (n := state.num(x)) is not None}


def online_quest_id() -> int | None:
    """Activity 230 的首档在线奖励：双倍掉宝率 buff。"""
    rows = [r for r in state.table_rows("OnlineReward")
            if state.num(r.get("activityId")) == ONLINE_ACTIVITY and str(r.get("buffDescribe", "-1")) not in ("-1", "")]
    return min((qid for r in rows if (qid := state.num(r.get("id"))) is not None), default=None)


def _is_buff(row) -> bool:
    return HOLIDAY_MARK in str(row.get("name", "")) and str(row.get("buffSceneId", "-1")) not in ("-1", "")


def holiday_buffs(now: int) -> list[tuple[int, str, int]]:
    """当前开放、今天可领的节日 buff：[(活动 id, 名称, 可领次数)]。"""
    today, out = state.week_index(now), []
    for r in state.table_rows("Activity"):
        aid = state.num(r.get("id"))
        if aid is None or not _is_buff(r) or not state.activity_open(aid, now):
            continue
        if today in weekdays(r):
            out.append((aid, str(r["name"]), state.num(r.get("receiveNum")) or 1))
    return out


def claimed(info, aid: int) -> int | None:
    """EveryDaySetting.resetActivityReward 记录节日 buff 已领取次数；读不到就不重领。"""
    daily = state.daily(info)
    if daily is None:
        return None
    raw = daily.get("resetActivityReward", {})
    if not isinstance(raw, dict):
        return None
    count = state.num(raw.get(str(aid), raw.get(aid, 0)))
    return count if count is not None and count >= 0 else None


def is_open(now: int) -> bool:
    """活动页“识别”用：在线福利活动或任一节日 buff 活动在档期内。"""
    return state.activity_open(ONLINE_ACTIVITY, now) or any(
        _is_buff(r) and state.activity_open(r.get("id"), now) for r in state.table_rows("Activity"))


class _Run:
    """一次领取：got 本次领到、done 之前领过、waiting 留给在线福利、failed 被拒绝或读不准。"""

    def __init__(self, ctx: TaskContext, holidays: list[tuple[int, str, int]]) -> None:
        self.ctx, self.holidays = ctx, holidays
        self.gains, self.got, self.done, self.waiting, self.failed = dungeon.Gains(), [], [], [], []
        self.unprocessed = [name for _, name, _ in holidays]
        self.stopped_early = False

    def uncertain_text(self, name: str, exc: UncertainOutcome) -> str:
        details = [f"已确认领取 {'、'.join(self.got) or '无'}"]
        if self.done:
            details.append(f"此前已领取：{'、'.join(self.done)}")
        if self.waiting:
            details.append(f"待后续确认：{'、'.join(self.waiting)}")
        details.append(f"{name}领取结果未知，未重发")
        if self.unprocessed:
            details.append(f"尚未处理：{'、'.join(self.unprocessed)}")
        return f"双倍掉宝：{'；'.join(details)}；{exc}"

    def claim(self, name: str, send) -> bool:
        """领一项，返回 True = 领到了或服务器说已领过。"""
        try:
            self.gains.add(send(), self.ctx.session.drain_pushes())
        except UncertainOutcome as exc:
            raise UncertainOutcome(self.uncertain_text(name, exc)) from exc
        except p.AllianceError as exc:
            if "已领取" in str(exc):
                self.done.append(name)
                return True
            self.failed.append(f"{name}({exc})")
            return False
        self.got.append(name)
        return True

    def online_first(self) -> bool:
        """领在线福利首档。返回 False = 用户停止，什么都没领。"""
        ctx = self.ctx
        if not state.activity_open(ONLINE_ACTIVITY, ctx.now) or state.week_index(ctx.now) not in weekdays(state.row("Activity", ONLINE_ACTIVITY)):
            return True
        qid, statuses = online_quest_id(), quest.status(ctx.info)
        if qid is None or statuses is None or qid not in statuses:
            # 在线福利末尾任务会刷新并重新确认状态；这里不能冒险重领，也不应因此拦住其他独立的节日 buff。
            self.waiting.append("在线福利双倍掉宝率状态缺失，留到在线福利任务重新确认")
        elif statuses[qid] == quest.DONE:
            self.done.append(ONLINE_NAME)
        elif statuses[qid] in (quest.DOING, quest.READY):
            if ctx.stopped:
                return False
            if self.claim(ONLINE_NAME, lambda: quest.commit(ctx.request, qid)):
                ctx.session.carry[ONLINE_CLAIMED] = "yes"
        return True

    def holiday_all(self) -> None:
        ctx = self.ctx
        for index, (aid, name, limit) in enumerate(self.holidays):
            if ctx.stopped:
                self.unprocessed = [n for _, n, _ in self.holidays[index:]]
                self.stopped_early = True
                return
            used = claimed(ctx.info, aid)
            if used is None:
                self.failed.append(f"{name}（无法确认已领取次数，未发送请求）")
            elif used >= limit:
                self.done.append(name)
            else:
                self.unprocessed = [n for _, n, _ in self.holidays[index + 1:]]
                self.claim(name, lambda: ctx.request(ROUTE_HOLIDAY, {"activeId": aid}))

    def result(self) -> TaskResult:
        left = self.unprocessed if self.stopped_early else []
        parts = [x for x in (
            f"已领取 {'、'.join(self.got)}{dungeon.gain_note(self.gains)}" if self.got else "",
            f"今日已领过：{'、'.join(self.done)}" if self.done else "",
            f"待后续确认：{'、'.join(self.waiting)}" if self.waiting else "",
            f"用户停止，尚未领取：{'、'.join(left)}" if left else "",
            f"领取被拒绝：{'、'.join(self.failed)}" if self.failed else "",
        ) if x]
        if self.failed or (left and self.got):
            return TaskResult(ID, BLOCKED, "；".join(parts))
        return TaskResult(ID, SUBMITTED if self.got else SKIPPED, "；".join(parts) or "今天没有可领取的双倍掉宝奖励")


@task(ID, "双倍掉宝", writes={ROUTE_HOLIDAY, quest.R_COMMIT}, group="活动", first=True, window=is_open)
def run(ctx: TaskContext) -> TaskResult:
    if ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少服务器时间，无法确认节日 buff 是否开放，未发送请求")
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未领取双倍掉宝奖励")
    job = _Run(ctx, holiday_buffs(ctx.now))
    if not job.online_first():
        return TaskResult(ID, SKIPPED, "已停止，未领取双倍掉宝奖励")
    job.holiday_all()
    return job.result()
