"""师徒试炼（周常页）：师傅和徒弟两个**不同登录账号**同时在线，进同一个房间打师徒副本 1200103，再领奖励。

两个任务共用一套流程，只是领奖的人不同：
* ``teacher_trial``：师傅奖励（每周 ``Teacher.teacherChallengeWeekNum`` = 18 次），只提交师傅这一端的 ``battleExit``；
* ``teacher_trial_student``：徒弟奖励（每周 ``studentChallengeWeekNum`` = 2 次），两端各提交一份 ``battleExit``，奖励在徒弟会话上领。
勾了任务的这个存档就是师傅（房间由它创建）；界面“选择”里选徒弟（另一个登录账号的存档）和次数（留空不执行）。

流程（旧版真机验证过的顺序，客户端 TeamOperateCmd / SocketTeamProxy / BattleMapBase / ExitBattleServerCmd / MissonCompleteCmd）：
1. 师傅 ``getApprenticeshipData`` 取师徒关系的 masterId → ``createWaitingRoom {dungeonId:1200103, type:"master", masterId}``；
   徒弟取自己的 masterId → ``joinWaitingRoom`` → ``changePlayerStatus {status:1}``；
2. 师傅 ``prepareCreateOnlineCopy``；两端等 ``onPrepareCreateOnlineCopy``，各自上报加载进度 5/50/100，再各自等 ``onCreateOnlineCopy``
   （一次性的战斗服务器地址、实例号、口令和 ``playerInfos``；两端必须是同一个实例，座位 0/1 必须互相一致）；
3. 两端各连战斗服务器 ``enter`` → ``loadComplete`` → 等 ``onAllLoadComplete`` → ``keyOperation`` 报 ready（``uid``/``teamPs`` = 座位号）；
4. 两端都等到第一个 onSendFrame（战斗开了），再落稳一会儿（不 ping：战斗服务器只认客户端的 copy.copyHandler.ping 路由且会拒绝纯 Python 玩家，十绝阵也不用）（不发任何攻击/移动按键，旧版的按键脉冲是测试残留，已去掉）；
5. 结算：``frameNum`` = 各自收到的 ``onSendFrame`` 数 × ``sendKeyFrameNum``（**收不到帧就不提交**），
   ``battleResult:5``，``playerInfo`` 里 ``isme`` 相对提交的那条连接；等 0.6 秒，关掉战斗连接；
6. 领奖励的那一端依次 ``getMasterCopyRewardData`` → ``getMasterCopyReward``（空参数，各发一次、不重发），以 ``give`` 为准。

次数：已用 = 角色信息 / 刷新 / 师徒关系 / ``getMasterCopyRewardData``（只读探测）里的对应周计数；都没有但 ``everyWeekSetting`` 对象存在 = 0（服务器不下发 0 值），连对象都没有才不开房；每次按领奖端的剩余次数封顶。
和旧版的区别：旧版要求“已准备好怪物自杀 SWF 补丁”才放行，但那个检查只看文件在不在、对纯 Python 流程没有任何作用，新版去掉。
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from typing import Any, Callable, Mapping

from .. import data, dungeon, api, online, protocol as p, state
from . import team_room
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, UncertainOutcome, task
from ..online import R_ENTER, R_EXIT_BATTLE, R_EXIT_ROOM, R_JOIN, R_KEYS, R_LOAD_DONE, R_LOADING, R_PREPARE, R_ROOM, R_STATUS

R_INFO = api.R_APPRENTICESHIP_DATA
R_REWARD_DATA = "scene.copyHandler.getMasterCopyRewardData"
R_REWARD = "scene.copyHandler.getMasterCopyReward"
DUNGEON = 1200103
SIDES = (("master", "师傅"), ("student", "徒弟"))
START_DELAY, SETTLE_DELAY, REWARD_DELAY = 0.6, 1.4, 1.0
MAX_TIMES = 18
MAX_MISSES = 3  # 连续几场没有奖励就停


class NoReward(p.AllianceError):
    """打完了、奖励查询没有可领的奖励（服务器没说是 boss 没判击败还是次数到顶）。"""


DIRECTORY: Callable[[], list[tuple[str, str]]] = lambda: []  # 界面注入：[("登录名|存档", "显示名")]，徒弟下拉框用


class TrialTrace:
    """只记步骤名和耗时，不记录战斗口令或账号凭据。"""

    def __init__(self):
        self.started = time.monotonic()
        self.steps: list[tuple[str, float]] = []
        self.failed = ""

    def call(self, label, fn, *args, **kwargs):
        began = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception:
            self.failed = label
            raise
        finally:
            self.steps.append((label, time.monotonic() - began))

    def note(self) -> str:
        slowest = sorted(self.steps, key=lambda step: step[1], reverse=True)[:5]
        parts = "，".join(f"{label} {elapsed:.2f}s" for label, elapsed in slowest)
        failed = f"；失败步骤：{self.failed}" if self.failed else ""
        return f"本场 {time.monotonic() - self.started:.2f}s{failed}（主要耗时：{parts}）"


def student_items(_acc: Mapping[str, Any]) -> list[tuple[str, str]]:
    return DIRECTORY()


def parse_ref(value: Any) -> tuple[str, int] | None:
    user, _, slot = str(value or "").rpartition("|")
    return (user, int(slot)) if user and slot.isdigit() and 1 <= int(slot) <= 8 else None


def week_field(role: str) -> str:
    return "studentChallengeWeekNum" if role == "student" else "teacherChallengeWeekNum"


def week_limit(role: str) -> int | None:
    n = state.num((data.row("Teacher", 1) or {}).get(week_field(role)))
    return n if n and n > 0 else None


def week_used(source: Any, role: str) -> int | None:
    n = state.num(state.find(source, week_field(role), depth=8))
    return n if n is not None and n >= 0 else None


def read_used(session, role: str) -> int | None:
    """角色信息 → 刷新 → 师徒关系 → 奖励资格（只读探测，绝不领奖）。"""
    if (n := week_used(session.info, role)) is not None:
        return n
    for fetch in (session.refresh, lambda: api.apprenticeship_data(session.request), lambda: session.request(R_REWARD_DATA, {})):
        try:
            got = fetch()
        except p.AllianceError:
            continue
        if (n := week_used(got, role)) is not None or (n := week_used(session.drain_pushes(), role)) is not None:
            return n
    # 服务器不下发值为 0 的周计数（客户端 EveryWeekSetting 默认 0）：周设置对象在、只缺这个键 = 本周还没打过
    return 0 if state.weekly(session.info) is not None else None


def master_id(resp: Mapping[str, Any]) -> int:
    """CreateTeamProxy 附带的 teacherUid（没有师父就是 0）。"""
    for source in (resp, resp.get("info") if isinstance(resp.get("info"), Mapping) else {}):
        for key in ("masterUid", "teacherUid", "master_id", "teacher_id"):
            n = state.num(source.get(key))
            if n is not None and n >= 0:
                return n
    master = state.find(resp, "master", depth=7)
    if isinstance(master, Mapping):
        for key in ("uid", "userId", "playerId"):
            n = state.num(master.get(key))
            if n is not None and n > 0:
                return n
    return 0


def exit_body(frame_num: int, seats: Mapping[str, online.Seat], order: tuple[str, ...], local: str) -> dict[str, Any]:
    """一端的 battleExit。isme 相对提交它的那条连接；order 是 playerInfos 的顺序。"""
    return {"frameNum": max(1, frame_num), "battleResult": 5, "data": [],
            "playerInfo": [{"playerId": seats[r].uid, "hp": seats[r].hp, "from": "gate", "campId": 1, "exit": False,
                            "isme": r == local, "dead": False} for r in order]}


def frames(team, per_push: int) -> int:
    """本端收到的帧数 × 每帧逻辑帧数；fight 里等到的第一帧已被取走，要加回来。"""
    n = 1 + sum(getattr(m, "route", None) == "onSendFrame" for m in team.drain_pushes())
    return n * per_push


def fight(mt, st, wait_seconds: float, trace: TrialTrace) -> None:
    """双方 ready 后不需要任何操作：等两端都收到第一个 onSendFrame（战斗开了），落稳再结算。"""
    for name, team in (("师傅", mt), ("徒弟", st)):
        trace.call(f"{name}首帧", team.wait_push, "onSendFrame", wait_seconds)
    trace.call("战斗落稳", time.sleep, START_DELAY + SETTLE_DELAY)  # 两条战斗连接开着，不能被“停止”打断


def _fill_room(master, student, room_id: str, wait: float, trace: TrialTrace) -> dict[str, Any]:
    """徒弟入房 → 开战 → 上报加载 → 等战斗服务器地址。返回两端的 onCreateOnlineCopy。"""
    call = trace.call
    student_wait = max(student.config.timeout, 15.0)
    student_teacher = master_id(call("徒弟关系", api.apprenticeship_data, student.request))
    call("入房", student.request, R_JOIN, {"roomId": room_id, "dungeonId": DUNGEON, "type": "master", "masterId": student_teacher})
    call("徒弟准备", student.request, R_STATUS, {"roomId": room_id, "status": 1, "masterId": student_teacher})
    call("准备开战", master.request, R_PREPARE, {"roomId": room_id})
    call("师傅准备广播", master.wait_push, "onPrepareCreateOnlineCopy", wait)
    call("徒弟准备广播", student.wait_push, "onPrepareCreateOnlineCopy", student_wait)
    for percent in (5, 50, 100):  # 两端同步上报，服务器等双方都到 100 才开战
        for name, who in (("师傅", master), ("徒弟", student)):
            call(f"{name}加载{percent}%", who.notify, R_LOADING, {"roomId": room_id, "percent": percent, "version": 1})
    return {"master": call("师傅战斗地址", master.wait_push, "onCreateOnlineCopy", wait),
            "student": call("徒弟战斗地址", student.wait_push, "onCreateOnlineCopy", student_wait)}


def _seat_both(master, student, created: Mapping[str, Any]) -> tuple[online.Endpoint, dict[str, online.Seat]]:
    """核对两端是同一个战斗实例、座位 0/1 互相一致。返回 (战斗服务器地址, 两端座位)。"""
    where = {r: online.endpoint(v) for r, v in created.items()}
    if None in where.values():
        raise p.AllianceError("onCreateOnlineCopy 没有给出完整的 ip、port、instId")
    key = lambda e: (e.host, e.port, e.inst_id)
    if key(where["master"]) != key(where["student"]):
        raise p.AllianceError("师徒两端收到的战斗服务器实例不一致")
    entry = {"master": online.role_stats(master.info), "student": online.role_stats(student.info)}
    if None in entry.values():
        raise p.AllianceError("两端的进角响应缺少角色 uid/hpMax")
    seats = {r: online.seat(created[r], entry[r][0], entry[r][1]) for r in created}
    if None in seats.values():
        raise p.AllianceError("onCreateOnlineCopy 没有给出本角色在 playerInfos 里唯一的位置")
    other = {"master": online.seat(created["student"], entry["master"][0], entry["master"][1]),
             "student": online.seat(created["master"], entry["student"][0], entry["student"][1])}
    if (None in other.values() or other["master"].slot != seats["master"].slot or other["student"].slot != seats["student"].slot
            or {seats["master"].slot, seats["student"].slot} != {0, 1}):
        raise p.AllianceError("两端 playerInfos 的座位顺序不一致")
    return where["master"], seats


def _start_battle(master, student, teams: Mapping[str, Any], where: online.Endpoint, seats: Mapping[str, online.Seat],
                  cg: str, wait: float, trace: TrialTrace) -> dict[str, int]:
    """两端进战斗服务器 → 报加载完 → 报就绪 → 等战斗开始。返回两端每条帧推送代表的帧数。"""
    call = trace.call
    per = {}
    for side, label in SIDES:
        per[side] = team_room.frame_step(call(f"{label}进战斗", team_room.enter_battle, teams[side], where, seats[side].uid, cg))
    for side, label in SIDES:
        call(f"{label}加载完成上报", teams[side].notify, R_LOAD_DONE, {})
    call("师傅全员加载广播", teams["master"].wait_push, "onAllLoadComplete", max(master.config.timeout, 30.0))
    call("徒弟全员加载广播", teams["student"].wait_push, "onAllLoadComplete", max(student.config.timeout, 30.0))
    for side, label in SIDES:
        call(f"{label}就绪", team_room.send_ready, teams[side], seats[side].slot)
    fight(teams["master"], teams["student"], wait, trace)
    return per


def one_battle(ctx: TaskContext, student, role: str, trace: TrialTrace) -> None:
    """建房 → 开战 → 提交 battleExit（成功返回即已提交，战斗连接已关闭）。"""
    master = ctx.session
    call = trace.call
    wait = max(master.config.timeout, 15.0)
    # 客户端无条件发自己的 CurrCg（两端是同一个版本）；回放要求录像 cg 与之一致。
    # 开房前就读好：第一次要联网，可能要好几秒，不能等战斗服务器地址下来了才去读
    cg = call("客户端版本", online.client_cg, master.now)
    teacher_uid = master_id(call("师傅关系", api.apprenticeship_data, master.request))
    room = call("建房", master.request, R_ROOM, {"dungeonId": DUNGEON, "type": "master", "masterId": teacher_uid})
    room_id = team_room.created_room_id(room)
    if not room_id:
        raise p.AllianceError("创建师徒房间的响应没有 roomId")
    submitted = False
    try:
        created = _fill_room(master, student, room_id, wait, trace)
        where, seats = _seat_both(master, student, created)
        order = ("master", "student") if seats["master"].slot == 0 else ("student", "master")
        with ExitStack() as stack:
            mt = call("师傅战斗服TLS/Pomelo连接", stack.enter_context, master.open_team(where.host, where.port))
            st = call("徒弟战斗服TLS/Pomelo连接", stack.enter_context, student.open_team(where.host, where.port))
            teams = {"master": mt, "student": st}
            per = _start_battle(master, student, teams, where, seats, cg, wait, trace)
            counts = {side: frames(teams[side], per[side]) for side in teams}
            for side, label in SIDES if role == "student" else SIDES[:1]:
                call(f"{label}结算", teams[side].request, R_EXIT_BATTLE, exit_body(counts[side], seats, order, side))
                submitted = True
        # 战斗连接已关，只剩主连接：用 ctx.sleep 等（照常应答心跳）；不让竞技场/斗宠的手打插进来，也不因“停止”跳过——这一场已经提交了，奖励照查
        call("领奖前等待", ctx.sleep, REWARD_DELAY, background=False)
    finally:
        if not submitted:
            team_room.leave_room(master, room_id)


def claim(owner, role: str, trace: TrialTrace) -> tuple[dungeon.Gains, int | None]:
    """在领奖励的那一端领。两个请求都是空参数、各发一次。"""
    first = trace.call("奖励查询", owner.request, R_REWARD_DATA, {})
    if not isinstance(first.get("give"), list):
        # 把服务器的完整响应带出去：“未击败boss或奖励次数已达上限”分不清是哪一种，要靠周计数等字段判断
        raise NoReward(f"{first.get('error') or '师徒试炼奖励查询没有返回可领取的奖励'}（服务器响应：{str(first)[:300]}）")
    reward = trace.call("领奖", owner.request, R_REWARD, {})
    pushes = owner.drain_pushes()
    gains = dungeon.Gains()
    gains.add(reward, pushes)
    seen = [week_used(x, role) for x in (reward, first, *pushes)]
    return gains, next((n for n in seen if n is not None), None)


def _rounds(ctx: TaskContext, student, owner, role: str, goal: int, used: int, limit: int) -> tuple[dungeon.Gains, int, int, str | None]:
    """一场接一场打到 goal 次。返回 (奖励, 完成次数, 最新已用次数, 停止原因)。"""
    gains, done, misses = dungeon.Gains(), 0, 0
    while done < goal:
        if ctx.stopped:
            return gains, done, used, "已被用户停止"
        trace = TrialTrace()
        try:
            one_battle(ctx, student, role, trace)
            got, seen = claim(owner, role, trace)
        except NoReward as exc:
            # 真机 2026-10-04：连打 5 次后第 6 次被判“未击败boss”，同一版本马上再跑又成功、周计数没动 → 偶发。
            # 这是一场新的战斗（不是重发同一个请求）；连续 MAX_MISSES 场没有奖励就停
            misses += 1
            ctx.log(f"师徒试炼 {done + 1}/{goal} 这一场没有奖励；{trace.note()}")
            if misses >= MAX_MISSES:
                return gains, done, used, f"第 {done + 1} 次连续 {misses} 场没有奖励：{exc}"
            continue
        except p.AllianceError as exc:
            return gains, done, used, f"第 {done + 1} 次失败：{exc}；{trace.note()}"
        except UncertainOutcome as exc:
            raise UncertainOutcome(f"已完成 {done} 次；第 {done + 1} 次；{trace.note()}；{exc}") from exc
        misses = 0
        gains.items.update(got.items)
        done += 1
        used = seen if seen is not None else used + 1
        ctx.log(f"师徒试炼 {done}/{goal}；{trace.note()}")
        if used >= limit:
            break
    return gains, done, used, None


def run_role(ctx: TaskContext, tid: str, role: str) -> TaskResult:
    times = dungeon.read_times(ctx, tid, MAX_TIMES, default=0)
    if isinstance(times, TaskResult):
        return times
    ref = parse_ref(ctx.options.get("student"))
    if ref is None:
        return TaskResult(tid, SKIPPED, "没有选择徒弟")
    if times == 0:
        return TaskResult(tid, SKIPPED, "次数留空，不执行")
    owner_name = "徒弟" if role == "student" else "师傅"
    limit = week_limit(role)
    try:
        with ctx.open_peer(*ref) as student:
            owner = student if role == "student" else ctx.session
            used = read_used(owner, role)
            if used is None or limit is None:
                return TaskResult(tid, BLOCKED, f"{owner_name}奖励次数信息不完整（缺少{'服务器已用次数' if used is None else '客户端上限'}），未开房")
            left = max(0, limit - used)
            if left == 0:
                return TaskResult(tid, SKIPPED, f"{owner_name}奖励本周剩余 0/{limit} 次")
            gains, done, used, why = _rounds(ctx, student, owner, role, min(times, left), used, limit)
    except p.AllianceError as exc:
        return TaskResult(tid, BLOCKED, str(exc))
    note = f"师徒试炼（{owner_name}奖励）完成 {done} 次，本周剩余 {max(0, limit - used)}/{limit}" + dungeon.gain_note(gains)
    if why:
        return TaskResult(tid, BLOCKED, f"{note}；{why}" if done else why)
    return TaskResult(tid, SUBMITTED, note)


def auto_for(role: str):
    def auto(ctx: TaskContext) -> int | None:
        """界面“自动配置”：每周上限 - 本周已用（读不到已用就不改）。
        徒弟奖励的次数记在徒弟那个存档上：登录“选择”里选的徒弟去读；没选徒弟就不改。"""
        limit = week_limit(role)
        if role == "student":
            ref = parse_ref(ctx.options.get("student"))
            if ref is None:
                return None
            with ctx.open_peer(*ref) as student:
                used = read_used(student, role)
        else:
            used = read_used(ctx.session, role)
        return None if limit is None or used is None else limit - used
    return auto


ROUTES = dict(
    reads={R_INFO, R_REWARD_DATA},
    writes={R_ROOM, R_JOIN, R_STATUS, R_PREPARE, R_LOADING, R_EXIT_ROOM, R_ENTER, R_LOAD_DONE, R_KEYS, R_EXIT_BATTLE, R_REWARD},
    group="周常", needs=(("student",),),
    times_max=MAX_TIMES, times_default=0,  # 没存过次数 = 0 不执行：没选徒弟时自动配置不改次数框，不能显示成 1
    choices=Choices(None, "",
                    picks=(Pick("student", "徒弟（另一个登录账号的存档）", student_items),)),
)


@task("teacher_trial", "师徒试炼·师傅", auto=auto_for("master"), **ROUTES)
def run_master(ctx: TaskContext) -> TaskResult:
    return run_role(ctx, "teacher_trial", "master")


@task("teacher_trial_student", "师徒试炼·徒弟", auto=auto_for("student"), **ROUTES)
def run_student(ctx: TaskContext) -> TaskResult:
    return run_role(ctx, "teacher_trial_student", "student")
