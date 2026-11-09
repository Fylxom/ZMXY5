"""竞技场 / 斗宠大会：用完今日免费挑战次数。界面“选模式”：榜首 / 榜尾 / 扫荡（没有“无”，默认扫荡）。

次数 = ``Challenge[1|2].jjcChallengeNum``（"a;b;…" 按星期取值，周日=0；服务器响应里有就用响应的）
减 ``arenaSetting|petArenaSetting.week[星期]``（有效周状态中未出现该活动记录时沿用客户端默认空记录）。读不到 → 不挑战。
战斗不做本地模拟，两种模式：
* 扫荡：``sweepEnergy ≥ 4`` 时从推荐的 8 号位向 1 号位找能扫的。客户端只给“自己战力 × ``Challenge.sweepPower`` ≥ 对手战力”的对手
  显示扫荡按钮（``sweepable``），不满足的号位不拿去试；战力读不到的照旧让服务器判断，被明确拒绝（战力/报名等）就换下一位。
  都扫不了、或者扫到一半能量用完，就改手打 8 号位（斗宠没有 8 号位用 1 号位；竞技场没有 8 号位就停），冷却按手打的算；
  改手打的原因写进最终结果。这次改手打**不会改存设置**（旧版会改存）。
* 榜首 / 榜尾（都是手打）：榜首挑战前 8 位里战力最高的（``strongest``，战力读不到就 1 号位），
  榜尾挑战战力最低的（``weakest``，战力读不到就打最后一位）；设置里存的值榜首是 ``manual``（原“手打”，含义没变）、榜尾是 ``bottom``。
  ``beginArenaBattle`` → ``endArenaBattle {targetId, win:1}``；
  同一活动两次手打之间至少隔 10 秒；每次打之前先重读推荐（上一次操作后对手会变），读完马上打。
斗宠大会已报名时客户端不开放扫荡，扫荡模式也直接手打。点券入口一律没有声明。

**穿插执行**：手打的冷却时间不空等。竞技场/斗宠任务有前置步骤（本次所有任务之前，但排在其他前置步骤——接取日常任务、限时任务——之后）：读次数、把扫荡扫完（1 秒间隔，没有冷却）；
还要手打的，用 ``Session.hook`` 挂上，之后其他任务每发一个请求、每次 ``ctx.sleep`` 时 ``Scheduler.tick`` 看一眼，
哪个活动冷却到了就穿插打一次（两个活动各算各的冷却，同时开时自然交替）。所有普通任务做完后的收尾（``finale``）把没打完的打完，
最终结果并入任务行。单独运行（没经过前置步骤，如测试）时 ``run`` 里直接全部打完。

**任务行**：每做完一次就更新进度（``Job.show``）；打满最后一次当场结束并显示最终结果（``Job.finish`` → ``ctx.report``），不等收尾。
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Mapping

from .. import api, daycut, protocol as p, state
from ..core import BLOCKED, PROGRESS, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, load_tasks, task

SWEEP_COST = 4
SWEEP_GAP, CHALLENGE_GAP = 1.0, 10.0
MAX_RUNS = 50
_ID_KEYS = ("rid", "targetId", "target_id", "id")
_LIST_KEYS = ("recommend", "recommendList", "rivals", "targets", "list")
_NAME_KEYS = ("name", "nickName", "nickname", "userName")
_ENTITY_KEYS = ("entityId", "entity_id")
_SWEEP_REFUSED = ("不能扫荡", "无法扫荡", "不可扫荡", "不能速通", "无法速通", "不符合扫荡条件", "速通条件", "已报名")
_TARGET_GONE = ("不能挑战", "无法挑战", "不可挑战", "不存在", "已被挑战", "已经挑战", "已挑战过", "不在榜", "已失效", "无效目标")
_SUBJECT = ("目标", "对手", "对方", "该玩家", "玩家不存在")


@dataclass(frozen=True)
class Arena:
    id: str
    title: str
    pet: bool
    challenge_row: int
    setting: str
    info: str
    sweep: str
    details: str
    begin: str
    end: str


ARENA = Arena("arena", "竞技场", False, 1, "arenaSetting", api.R_ARENA_INFO, "scene.arenaHandler.sweep",
              "scene.arenaHandler.getArenaTargetInfo", "scene.arenaHandler.beginArenaBattle", "scene.arenaHandler.endArenaBattle")
PET_ARENA = Arena("pet_arena", "斗宠大会", True, 2, "petArenaSetting", api.R_PET_ARENA_INFO, "scene.petArenaHandler.sweep",
                  "scene.petArenaHandler.getArenaUserData", "scene.petArenaHandler.beginArenaBattle", "scene.petArenaHandler.endArenaBattle")


def target_id(row):
    if not isinstance(row, dict):
        return None
    return next((row[k] for k in _ID_KEYS if row.get(k) not in (None, "")), None)


def targets(value, depth: int = 6) -> list[dict]:
    """服务器按顺序给的推荐对手（1~8 号位），不凭空造目标。"""
    if depth < 0:
        return []
    if isinstance(value, dict):
        for key in _LIST_KEYS:
            child = value.get(key)
            if isinstance(child, dict) and target_id(child) is not None:
                return [child]
            rows = list(child.values()) if isinstance(child, dict) else child
            if isinstance(rows, list) and (found := [r for r in rows if target_id(r) is not None]):
                return found
        children = list(value.values())
    elif isinstance(value, list):
        if found := [r for r in value if target_id(r) is not None]:
            return found
        children = value
    else:
        return []
    return next((found for c in children if (found := targets(c, depth - 1))), [])


def _weekly_setting(arena: Arena, resp, info) -> tuple[Mapping | None, str | None]:
    """EveryWeekSetting.initData 仅覆盖响应中出现的活动字段，其余沿用进角状态/默认值。"""
    known_week = False
    for payload in (resp, info):
        if (isinstance(payload, Mapping) and "everyWeekSetting" in payload
                and not isinstance(payload["everyWeekSetting"], Mapping)):
            return None, "服务器每周状态格式异常"
        week = state.weekly(payload)
        known_week = known_week or week is not None
        # 兼容直接返回活动字段的响应；不从对手信息里递归搜索次数。
        for holder in (week, payload):
            if isinstance(holder, Mapping) and arena.setting in holder:
                setting = holder[arena.setting]
                return (setting, None) if isinstance(setting, Mapping) else (None, "活动次数记录格式异常")
    if known_week:
        # 客户端 EveryWeekSetting 初始化/切角时默认 {week:{},rewardTime:0}。
        return {"week": {}}, None
    return None, "服务器未返回有效的每周状态或活动次数记录"


def _free_quota(arena: Arena, resp, info, now) -> tuple[int | None, str | None]:
    if now is None:
        return None, "服务器未返回时间，无法确定今日星期"
    raw = state.find(resp, "jjcChallengeNum", "challengeNum", depth=8)
    if raw in (None, ""):
        raw = (state.row("Challenge", arena.challenge_row) or {}).get("jjcChallengeNum")
    day = state.week_index(now)
    if isinstance(raw, str) and ";" in raw:
        parts = [state.num(x) for x in raw.split(";")]
        cap = parts[day] if day < len(parts) else None
    else:
        cap = state.num(raw)
    if cap is None or cap < 0:
        return None, "今日免费次数上限无法解析"
    setting, issue = _weekly_setting(arena, resp, info)
    if setting is None:
        return None, issue
    if "week" not in setting:
        return None, "活动次数记录缺少 week 字段"
    week = setting.get("week")
    if isinstance(week, dict):
        raw_used = week.get(str(day), week.get(day, 0))
    elif isinstance(week, list):
        raw_used = week[day] if day < len(week) else 0
    else:
        return None, "今日已用次数格式异常"
    used = state.num(0 if raw_used in (None, "") else raw_used)
    if used is None or used < 0:
        return None, "今日已用次数格式异常"
    return max(0, cap - used), None


def free_left(arena: Arena, resp, info, now) -> int | None:
    return _free_quota(arena, resp, info, now)[0]


def _power(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        power = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return power if math.isfinite(power) and power >= 0 else None


def my_power(arena: Arena, me) -> float | None:
    """客户端算扫荡按钮用的“自己的战力”（ItemHome / ItemHomePet.setPower）：竞技场是出战角色的，斗宠是上阵宠物的总和。"""
    if not isinstance(me, Mapping):
        return None
    if arena.pet:
        pets = me.get("pets")
        powers = [_power(x.get("fightPower")) if isinstance(x, Mapping) else None for x in pets] if isinstance(pets, list) else []
        return sum(powers) if powers and None not in powers else None
    who = me.get("p2") if state.num(me.get("index")) and isinstance(me.get("p2"), Mapping) else me.get("p1")
    return _power(who.get("fightPower")) if isinstance(who, Mapping) else None


def target_power(arena: Arena, row) -> float | None:
    """推荐对手显示的战力（ItemHome / ItemHomePet.setData）。"""
    if not isinstance(row, Mapping):
        return None
    if arena.pet:
        total, battle = _power(row.get("totalScore")), _power(row.get("battleScore"))
        return None if total is None or battle is None else float(int(total - battle))
    who = row.get("p2") if state.num(row.get("index")) else row.get("p1")
    return _power(who.get("fightPower")) if isinstance(who, Mapping) else None


def sweepable(arena: Arena, me, row) -> bool | None:
    """客户端只给“自己战力 × Challenge.sweepPower ≥ 对手战力”的对手显示扫荡按钮。读不到任何一项 → None（不知道，让服务器判断）。"""
    rate = _power((state.row("Challenge", arena.challenge_row) or {}).get("sweepPower"))
    mine, theirs = my_power(arena, me), target_power(arena, row)
    return None if rate is None or mine is None or theirs is None else rate * mine >= theirs


def strongest(arena: Arena, recs: list[dict]) -> int | None:
    """手打挑战的号位（1 起）：前 8 位里战力最高的；同战力取靠前的；一个战力都读不到就打 1 号位。"""
    rows = recs[:8]
    if not rows:
        return None
    powers = [target_power(arena, r) for r in rows]
    known = [(pw, -i) for i, pw in enumerate(powers) if pw is not None]
    return 1 - max(known)[1] if known else 1


def weakest(arena: Arena, recs: list[dict]) -> int | None:
    """榜尾手打的号位（1 起）：前 8 位里战力最低的；同战力取靠后的；一个战力都读不到就打最后一位。"""
    rows = recs[:8]
    if not rows:
        return None
    known = [(pw, -i) for i, pw in enumerate(target_power(arena, r) for r in rows) if pw is not None]
    return 1 - min(known)[1] if known else len(rows)


def refused_sweep(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(w in text for w in _SWEEP_REFUSED) or (any(w in text for w in _SUBJECT) and any(w in text for w in _TARGET_GONE))


def _flat(resp) -> dict:
    out = dict(resp) if isinstance(resp, dict) else {}
    for key in ("userData", "data"):
        if isinstance(resp, dict) and isinstance(resp.get(key), dict):
            out.update(resp[key])
    return out


def _has_opponent(row) -> bool:
    return bool(state.find(row, *_NAME_KEYS, depth=8) and state.find(row, *_ENTITY_KEYS, depth=8))


SWEEP, MANUAL, BOTTOM = "sweep", "manual", "bottom"  # MANUAL = 榜首（沿用原“手打”存的值）
MODES = [(MANUAL, "榜首"), (BOTTOM, "榜尾"), (SWEEP, "扫荡")]
MODE_NOTE = ("榜首 / 榜尾：前 8 名里打战力最高 / 最低的，直接按胜利结算；两次间隔 10 秒，等的时候穿插别的任务。\n"
             "扫荡：从 8 号位往前找能扫的；扫不了或能量用完改打 8 号位（斗宠已报名时直接手打）。")


class Job:
    """一项活动（竞技场或斗宠）的执行状态。``step()`` 只发一步（扫荡一次 / 重读推荐后手打一次），**不等待**：
    什么时候能发由 ``ready_at()`` 给出，调用方（``Scheduler``）负责等，或者去做别的事。"""

    def __init__(self, ctx: TaskContext, arena: Arena, mode) -> None:
        self.ctx, self.a, self.mode = ctx, arena, mode
        self.swept = self.fought = self.total = 0  # total 在 begin 里算出；0 = 还没算到（没开始做）
        self.dead: set[int] = set()  # 不能扫的号位：客户端规则判出来的（见 sweepable）+ 服务器拒绝过的
        self.manual_why = ""  # 扫荡模式下改手打的原因，写进最终结果
        self.result: TaskResult | None = None
        self.reported = False  # 最终结果已经作为任务结果交出去了
        self.last: float | None = None  # 本活动上一次挑战/扫荡完成的时刻（monotonic）；两个活动各算各的冷却
        self.fresh = True  # self.recs 是不是上一次操作之后的最新推荐

    def begin(self) -> None:
        """读状态、算次数。能直接得出结果（跳过/阻止）就设置 self.result。"""
        a, ctx = self.a, self.ctx
        if self.mode not in (SWEEP, MANUAL, BOTTOM):
            self.result = TaskResult(a.id, SKIPPED, "没有选择模式（在“选模式”里选榜首、榜尾或扫荡）")
            return
        try:
            resp = api.arena_info(ctx.request, pet=a.pet)
        except p.AllianceError as exc:
            self.result = TaskResult(a.id, BLOCKED, f"{a.title}状态读取失败：{exc}")
            return
        left, quota_issue = _free_quota(a, resp, ctx.info, ctx.now)
        recs = targets(resp)
        if left is None:
            self.result = TaskResult(a.id, BLOCKED, f"无法确认{a.title}免费挑战次数，未挑战：{quota_issue}")
        elif left <= 0:
            self.result = TaskResult(a.id, SKIPPED, f"{a.title}今日免费挑战次数已用完")
        elif not recs:
            self.result = TaskResult(a.id, SKIPPED, f"{a.title}没有服务器给出的挑战目标")
        if self.result:
            return
        self.recs, self.total = recs, min(left, MAX_RUNS)
        self._plan_sweep(resp)
        self.show()

    def _plan_sweep(self, resp) -> None:
        """判断这次能不能先扫荡；不能的原因记进 manual_why。"""
        a, ctx, recs = self.a, self.ctx, self.recs
        energy = state.num(state.find(resp, "sweepEnergy", depth=8))
        self.energy = energy if energy is not None else state.num(state.find(ctx.info, "sweepEnergy", depth=8))
        me, registered = state.section(resp, "self"), False
        if a.pet:  # 斗宠已报名（self.rank >= 0）时客户端不开放扫荡
            rank = state.num((me or {}).get("rank"))
            registered = rank is not None and rank >= 0
        self.dead = {slot for slot, row in enumerate(recs[:8], start=1) if sweepable(a, me, row) is False}
        self.can_sweep = False
        if self.mode == SWEEP:
            if registered:
                self.manual_why = "已报名，不能扫荡"
            elif self.energy is None or self.energy < SWEEP_COST:
                self.manual_why = "扫荡能量不足"
            elif len(self.dead) >= min(len(recs), 8):
                self.manual_why = "对手战力都超出扫荡范围"
            else:
                self.can_sweep = True

    @property
    def done(self) -> int:
        return self.swept + self.fought

    @property
    def live(self) -> bool:
        return self.result is None

    def ready_at(self) -> float:
        """下一步最早能发的时刻（monotonic）：扫荡隔 1 秒；手打隔 10 秒。"""
        if self.last is None:
            return float("-inf")
        return self.last + (SWEEP_GAP if self.can_sweep else CHALLENGE_GAP)

    def finish(self, stop: str = "") -> None:
        why = f"：{self.manual_why}" if self.manual_why and self.fought else ""
        text = f"{self.a.title}已挑战 {self.done}/{self.total} 次（扫荡 {self.swept}，手打 {self.fought}{why}）"
        if stop == "已被用户停止":
            status = SUBMITTED if self.done else SKIPPED
        elif stop:
            status = BLOCKED
        else:
            status = SUBMITTED if self.done else BLOCKED
        self.result = TaskResult(self.a.id, status, f"{text}；{stop}" if stop else text)
        self.ctx.report(self.result)  # 任务行马上显示最终状态，不等收尾

    def take(self) -> TaskResult | None:
        """还没交出的最终结果（交出后标记，不重复交）。"""
        if self.result is None or self.reported:
            return None
        self.reported = True
        return self.result

    def show(self) -> None:
        """当前进度写到自己的任务行（两个活动共用一个调度，不能靠 ctx.log：它写的是当时那个任务的行）。"""
        self.ctx.report(TaskResult(self.a.id, PROGRESS, f"{self.a.title} {self.done}/{self.total}"))

    def _counted(self) -> None:
        """刚做完一次：次数用完就当场结束，不等下一步（下一步要等冷却，穿插时还要等别的任务发请求）；没用完就更新进度。"""
        self.fresh = False
        if self.done >= self.total:
            self.finish()
        else:
            self.show()

    def _fight(self, target: dict) -> None:
        a, ctx = self.a, self.ctx
        tid, row = target_id(target), dict(target)
        try:
            if not (a.pet and _has_opponent(row)):  # 斗宠推荐里通常已带对手信息，竞技场总要查一次
                row.update(_flat(ctx.request(a.details, {"targetId": tid})))
            if not _has_opponent(row):
                raise p.AllianceError("服务器未返回对手 name/entityId，未创建挑战")
            ctx.request(a.begin, {})
            ctx.request(a.end, {"targetId": tid, "win": 1})
        finally:
            self.last = time.monotonic()

    def _sweep_once(self) -> None:
        a, ctx = self.a, self.ctx
        slot = next((s for s in range(min(len(self.recs), 8), 0, -1) if s not in self.dead), None)
        if self.energy < SWEEP_COST or slot is None:
            # 改手打（什么时候打由 ready_at 决定，冷却按手打的算）
            self.manual_why = "扫荡能量不足" if self.energy < SWEEP_COST else "对手都不能扫荡"
            self.can_sweep = False
            return
        try:
            r = ctx.request(a.sweep, {"targetId": target_id(self.recs[slot - 1])})
        except p.AllianceError as exc:
            self.last = time.monotonic()
            if refused_sweep(exc):
                self.dead.add(slot)  # 换下一位（下一步，间隔 1 秒）
            else:
                self.finish(f"扫荡失败：{exc}")
            return
        self.last = time.monotonic()
        remaining = state.num(state.find(r, "sweepEnergy", depth=8))
        self.energy = remaining if remaining is not None else self.energy - SWEEP_COST
        self.swept += 1
        self._counted()

    def _manual_slot(self) -> int | None:
        if self.mode == MANUAL:
            return strongest(self.a, self.recs)
        if self.mode == BOTTOM:
            return weakest(self.a, self.recs)
        return 8 if len(self.recs) >= 8 else (1 if self.a.pet else None)

    def _fight_once(self) -> None:
        if not self.fresh:  # 上一次操作后对手已变，重读推荐后再挑
            try:
                self.recs = targets(api.arena_info(self.ctx.request, pet=self.a.pet))
            except p.AllianceError as exc:
                self.finish(f"刷新对手列表失败：{exc}")
                return
            self.fresh = True
        slot = self._manual_slot()
        if slot is None:
            self.finish("服务器未返回 8 号位推荐目标，未手打")
            return
        if slot > len(self.recs):  # 斗宠固定打 1 号位：刷新后列表是空的
            self.finish("服务器没有返回对手列表，未手打")
            return
        try:
            self._fight(self.recs[slot - 1])
        except p.AllianceError as exc:
            self.finish(f"手打失败：{exc}")
            return
        self.fought += 1
        self._counted()

    def step(self) -> bool:
        """发一步。返回 True = 还要继续；False = 已结束（见 self.result）。"""
        if self.ctx.stopped:
            self.finish("已被用户停止")
        elif self.done >= self.total:
            self.finish()
        elif why := daycut.reason(self.ctx.session):
            self.finish(why)
        elif self.can_sweep:
            self._sweep_once()
        else:
            self._fight_once()
        return self.live


ID_HOOK = "arena"  # Session.hook 的键


class Scheduler:
    """一个连接上的竞技场/斗宠调度，两个任务共用。手打冷却时不空等：用 ``Session.hook`` 挂上，
    其他任务每发一个请求（或 ``ctx.sleep``）时 ``tick`` 看一眼，冷却到了的活动就穿插打一步。"""

    def __init__(self, ctx: TaskContext, a: Arena) -> None:
        arenas = [x for x in (ARENA, PET_ARENA) if x is a or x.id in ctx.tasks]
        self.session = ctx.session
        self.spec = load_tasks()[ARENA.id if ARENA in arenas else a.id]  # 竞技场任务声明的路由含斗宠的，穿插时按它放行
        self.deferred = False  # 经过前置步骤 = 手打穿插在后续任务里；否则（单独运行）run 里打完
        self.busy = False
        self.jobs = {x.id: Job(ctx, x, (ctx.options if x is a else ctx.all_options.get(x.id) or {}).get("mode") or SWEEP) for x in arenas}
        with self.bound():
            for job in self.jobs.values():
                job.begin()
        self.session.hook(ID_HOOK, self.tick)

    @contextmanager
    def bound(self):
        old = self.session._spec
        self.session.bind(self.spec)
        try:
            yield
        finally:
            self.session.bind(old)

    def live(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.live]

    def run_step(self, job: Job) -> None:
        if self.busy:  # 步骤里的请求又触发穿插点：不嵌套
            return
        self.busy = True
        try:
            with self.bound():
                try:
                    job.step()
                except UncertainOutcome as exc:
                    done = "、".join(f"{j.a.title}已挑战 {j.done} 次" for j in self.jobs.values() if j.total)
                    raise UncertainOutcome(f"{done}；{exc}") from exc
                except p.AllianceError as exc:
                    job.finish(f"出错：{exc}")
        finally:
            self.busy = False
        if not self.live():
            self.session.hook(ID_HOOK, None)

    def tick(self) -> None:
        """穿插点：冷却到了的活动打一步。"""
        now = time.monotonic()
        for job in self.live():
            # “打完当前这一场再停”期间，stop 事件要等活跃战斗结束才会置位。
            # 这段时间仍会经过 Session.pump；不能因竞技场冷却到期又开一场。
            if job.ctx.winding and not job.ctx.stopped:
                continue
            if job.ctx.stopped or job.ready_at() <= now:
                self.run_step(job)

    def wait_then_step(self, job: Job, ctx: TaskContext) -> bool:
        """等到 job 能发为止（等的时候不穿插别的）再发一步。返回 False = 等待时被用户停止。"""
        wait = job.ready_at() - time.monotonic()
        if wait > 0 and not ctx.sleep(wait, background=False):
            job.finish("已被用户停止")
            return False
        self.run_step(job)
        return True

    def sweep(self, job: Job) -> None:
        """扫荡没有冷却：先扫完（1 秒间隔）。扫不动退回手打后交给穿插。"""
        while job.live and job.can_sweep:
            if not self.wait_then_step(job, job.ctx):
                return

    def drain(self, ctx: TaskContext) -> None:
        """把还没打完的全部打完（收尾；这时已经没有别的任务可穿插了）。"""
        while live := self.live():
            job = min(live, key=Job.ready_at)
            if not self.wait_then_step(job, ctx):
                for other in self.live():
                    other.finish("已被用户停止")


def scheduler(ctx: TaskContext, a: Arena) -> Scheduler:
    sched = getattr(ctx.session, "_arena_sched", None)
    if sched is None:
        sched = ctx.session._arena_sched = Scheduler(ctx, a)
    sched.jobs[a.id].ctx = ctx  # 进度显示在自己的任务行
    return sched


def prelude(ctx: TaskContext, a: Arena) -> TaskResult | None:
    """本次所有任务之前（其他前置步骤之后）：读次数、把扫荡扫完；还要手打的挂到连接上，穿插在后面的任务里打。"""
    sched = scheduler(ctx, a)
    sched.deferred = True
    job = sched.jobs[a.id]
    sched.sweep(job)
    return job.take()


def run_arena(ctx: TaskContext, a: Arena) -> TaskResult:
    sched = scheduler(ctx, a)
    job = sched.jobs[a.id]
    if not sched.deferred:  # 没经过前置步骤（单独运行）：全部打完再返回
        for each in sched.live():
            sched.sweep(each)
        sched.drain(ctx)
    return job.take() or job.result or TaskResult(a.id, SKIPPED, f"{a.title} {job.done}/{job.total}", pending=True)  # 占位行写当前进度，不留空白


def finale(ctx: TaskContext, a: Arena) -> TaskResult | None:
    """所有普通任务做完后：把没打完的打完，交出最终结果（已经交过就不再交）。"""
    sched = scheduler(ctx, a)
    sched.drain(ctx)
    return sched.jobs[a.id].take()


def _register(a: Arena) -> None:
    both = (ARENA, PET_ARENA) if a is ARENA else (a,)  # 竞技场任务也要替斗宠打，路由两边都要声明
    @task(a.id, a.title, reads={x.info for x in both} | {x.details for x in both},
          writes={r for x in both for r in (x.sweep, x.begin, x.end)},
          modes=tuple(MODES), mode_default=SWEEP, mode_note=MODE_NOTE,
          prelude=lambda ctx: prelude(ctx, a), prelude_late=True, finale=lambda ctx: finale(ctx, a))
    def run(ctx: TaskContext) -> TaskResult:
        return run_arena(ctx, a)


_register(ARENA)
_register(PET_ARENA)
