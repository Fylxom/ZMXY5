"""运行器：一个账号的 前置步骤 → 任务本体 → 收尾，和多账号并行的批量执行。

结果未知会停止该登录账号的宿主、插入任务、后续存档和断点续跑，其他登录账号独立执行。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from . import activity, daycut, login, protocol as p, servertime, session as sessions, state
from .activity import stopping
from .session import Account, TaskContext, login_lock, wanted
from .taskdef import (BAG_FULL_HINT, BLOCKED, ERROR, FORCE_STOP_HINT, PAUSED, PROGRESS, SKIPPED, SUBMITTED, UNCERTAIN,
                      ForceStop, TaskResult, TaskSpec, UncertainOutcome, is_bag_full, is_kicked, load_tasks, run_order)

_SKIP_HALTED = "前序任务结果未知或已暂停，未继续执行"
_SKIP_STOPPED = "已被用户停止"
LATER_MAX = 3  # 同一个任务“过一会儿再来”最多等几轮（逃跑禁赛等完还被拒，说明不是等能解决的）
_SKIP_REPEAT = "同一账号的另一个存档已执行，本存档不重复"

# 界面登记：执行任务时每次进角成功，在工作线程里收到服务器时间（秒）。活动页靠它在数据表更新后的第一次执行顺带识别结束活动。
on_clock: Callable[[int], None] | None = None


def _tell_clock(now: int | None) -> None:
    listener = on_clock
    if listener is None or now is None:
        return
    try:
        listener(int(now))
    except Exception:
        pass  # 只是顺带的识别，不能影响任务


@contextmanager
def _signed_in(account: Account):
    """拿着这个登录账号的锁登录进角。"""
    with login_lock(account.config.username), sessions.Session(account.config, on_cookie=account.on_cookie) as session:
        yield session


def read_info(account: Account) -> Mapping[str, Any]:
    """登录并返回角色信息（只读，不执行任何任务）。供界面的“读取”按钮使用。"""
    with _signed_in(account) as session:
        return session.info


def read_auto(account: Account, specs: Iterable[TaskSpec], reserve: bool = False) -> dict[str, int | None | str | tuple]:
    """登录一次，对每个带 ``auto`` 的任务只读地算出这个存档现在的最大次数。
    reserve：按养成周留次数（该留的任务填“拥有的次数 - 今天要留的次数”）。返回 {任务 id: 次数}；某个任务读不到 = None，它的错误说明 = str。结果夹在 0–times_max 之间。"""
    out: dict[str, int | None | str] = {}
    with _signed_in(account) as session:
        session.peers, session.owns_login = account.peers, True  # 师徒试炼·徒弟的次数在徒弟那个账号上，要另外登录去读
        keep = None  # (养成周序号, 今天要留几次)
        if reserve:
            from . import online
            keep = state.growth_reserve(session.now, live=online.growth_week(session.now))  # 线上还没更新到那一周就一直留着
        for spec in specs:
            if spec.auto is None:
                continue
            session.bind(spec)
            try:
                n = spec.auto(TaskContext(session, account.options.get(spec.id, {})))
            except p.AllianceError as exc:
                out[spec.id] = str(exc)
                continue
            if isinstance(n, tuple):  # 不是次数的状态说明（如上届奖励的领取情况）：原样交给界面
                out[spec.id] = n
                continue
            if n is not None and reserve and spec.reserve and keep and keep[0] == spec.reserve:
                n = 0 if keep[1] >= state.RESERVE_CAP else int(n) - keep[1] * spec.reserve_gain  # 留次数：拥有的次数里留下 keep[1] 天的量攒到养成周；等更新的那天全留
            out[spec.id] = None if n is None else max(0, min(int(n), spec.times_max))
    return out


def read_clock(account: Account) -> tuple[Mapping[str, Any], int | None]:
    """登录并返回（角色信息，服务器时间秒）。只读；供活动页的“识别”按钮使用。"""
    with _signed_in(account) as session:
        return session.info, session.now


def _join(*notes: str) -> str:
    return "；".join(note for note in notes if note)


def _with_profile(result: TaskResult, profile: Mapping[str, Any]) -> TaskResult:
    """任务改过登录账号状态、结果里又没带：带上一份，界面据此保存。"""
    return replace(result, account_profile=dict(profile)) if profile and result.account_profile is None else result


class _AccountRun:
    """run_account 的一次执行：一个账号的结果列表，和 前置步骤 → 任务本体 → 收尾 三个阶段。"""

    def __init__(self, account: Account, stop: threading.Event | None, emit: Callable[[str, TaskResult], None]) -> None:
        self.account, self.stop, self.emit = account, stop, emit
        self.results: list[TaskResult] = []
        self.aborted: set[str] = set()  # 前置步骤已经给出结果的任务：本体和收尾都不再执行
        self.session: sessions.Session | None = None
        self.chosen: tuple[str, ...] = ()
        self.inbox: list[Account] = []  # 执行中插进来的任务（inject），由本线程在穿插点做掉
        self.accepting = self.nested = False
        self.halted = False  # 持续保留中止状态，收尾/关闭阶段也不能执行排队任务
        self.waiting = False  # 宿主任务正空等着（护送等船）：这时没有任务在做，界面可以再点“执行”插任务
        self.alias: str | None = None  # 正在做插进来的任务时：它自己的执行项名字（结果、日志记在它名下，可能是另一个存档）
        self.finals: dict[str, TaskResult] = {}  # 任务穿插着做完时报过的最终结果（ctx.report）：没等到本体/收尾就被停止、中断时用它
        self.reported_finals: dict[tuple[str, str], TaskResult] = {}  # 执行项、任务 -> ctx.report 已显示的最终结果
        self.later: list[tuple[float, int, TaskSpec]] = []  # 要过一会儿再重跑的任务（TaskResult.retry_in）：(到点 monotonic, 第几轮, 任务)

    @property
    def name(self) -> str:
        return self.alias or self.account.name

    @property
    def stopped(self) -> bool:
        return activity.stopped(self.stop)

    @property
    def winding(self) -> bool:
        return activity.winding(self.stop)

    @property
    def over(self) -> bool:
        """停止、打完这场再停、或者必须中止：都不再开始新的。"""
        return stopping(self.stop) or self.halted

    def record(self, r: TaskResult) -> None:
        self.results.append(r)
        self.emit_result(self.name, replace(r, status=PROGRESS) if r.pending else r)

    def emit_result(self, name: str, r: TaskResult) -> None:
        """ctx.report 已显示的同一最终结果只发一次；不同的收尾补充仍正常更新。"""
        if r.status != PROGRESS:
            reported = self.reported_finals.pop((name, r.task), None)
            if reported == r:
                return
            if reported is not None and (reported.status, reported.detail) == (r.status, r.detail):
                # 结果资料可能在 execute/merge 中补齐；仍通知 GUI 保存，不重写相同的完成日志。
                self.emit(name, replace(r, log=False))
                return
        self.emit(name, r)

    def show(self, name: str, r: TaskResult, keep: bool) -> None:
        """ctx.report：结果马上显示到任务行。最终结果另记一份（keep；插进来的任务前置、本体、收尾一口气做完，用不着）。"""
        if keep and r.status != PROGRESS:
            self.finals[r.task] = r
        self.emit_result(name, r)
        if r.status != PROGRESS:
            self.reported_finals[(name, r.task)] = r

    def execute(self, spec: TaskSpec, step: Callable[[TaskContext], TaskResult | None], owner: Account | None = None) -> tuple[TaskResult | None, bool]:
        """执行一步，返回 (结果, 是否必须停止该账号)。owner：插进来的任务用它自己的选项（结果仍记在本存档名下）。"""
        session = self.session
        owner = owner or self.account
        session.bind(spec)
        ctx = None
        ident = threading.get_ident()

        def ended(status: str, detail: str, keep_progress: bool = False) -> TaskResult:
            """任务被异常打断时的结果：登录账号状态总要带上，断点进度只有暂停时要。"""
            profile, progress = (ctx.account_profile, ctx.progress) if ctx else (None, None)
            return TaskResult(spec.id, status, detail, progress=dict(progress) if keep_progress and progress else None,
                              account_profile=dict(profile) if profile else None)

        activity.enter(ident, spec.id, self.stop)
        try:
            ctx = TaskContext(
                session, owner.options.get(spec.id, {}), stop=self.stop, no_pickup=owner.no_pickup,
                tasks=owner.tasks, all_options=owner.options, account_profile=owner.account_profile,
                log=lambda message, tid=spec.id, name=self.name: self.emit(name, TaskResult(tid, PROGRESS, message)),
                report=lambda result, name=self.name, keep=not self.nested: self.show(
                    name, _with_profile(result, owner.account_profile), keep),
                extra=None if self.nested else self.interleave,
                rest=None if self.nested else lambda on: setattr(self, "waiting", on),
            )
            session.bag_full = False
            result = self._pause_if_bag_full(spec, ctx, step(ctx))
            if result is None:
                return None, False
            result = _with_profile(result, ctx.account_profile)
            if result.status == UNCERTAIN:
                self.halted = True
            return result, result.status in (PAUSED, UNCERTAIN)
        except UncertainOutcome as exc:
            if is_kicked(exc):  # 被顶号：和背包满一样暂停，断点接着做
                return ended(PAUSED, str(exc), keep_progress=True), True
            self.halted = True
            return ended(UNCERTAIN, str(exc)), True  # 状态未知：不再继续，避免叠加副作用
        except ForceStop:
            self.halted = True
            return ended(UNCERTAIN, FORCE_STOP_HINT), True  # 被强制打断：进行中的操作结果未知
        except p.AllianceError as exc:
            if is_bag_full(exc):
                return ended(PAUSED, f"{exc}；{BAG_FULL_HINT}", keep_progress=True), True
            if is_kicked(exc):
                return ended(PAUSED, str(exc), keep_progress=True), True
            return ended(BLOCKED, str(exc)), False
        except Exception as exc:  # 任务自身 bug 不应拖垮整批
            return ended(ERROR, f"{type(exc).__name__}: {exc}"), False
        finally:
            activity.release(ident, self.stop)
            session.bind(None)

    def interleave(self) -> bool:
        """穿插点：别的账号正等着登录本账号（师徒试炼）就先把登录让给它；再把执行中插进来的任务（inject）做掉。
        在任务之间调用，也可以在别的任务等待的中途调用（护送经书等船）。"""
        if self.nested or self.halted:
            return self.halted
        user = self.account.config.username
        if not stopping(self.stop) and wanted(user):
            self.emit(self.account.name, TaskResult("login", PROGRESS, "别的账号的师徒试炼要登录本账号：先让出登录，等它做完再接着执行"))
            if self.session.lend():  # 重新登录失败会抛出：由调用处按登录失败处理
                self.emit(self.account.name, TaskResult("login", PROGRESS, "已重新登录，接着执行"))
        while not self.over:
            with _RUNS_LOCK:
                if not self.inbox:
                    return self.halted
                extra = self.inbox.pop(0)
            session, ident = self.session, threading.get_ident()
            held_task = activity.current(ident)
            held_spec, held_bag_full = session._spec, session.bag_full  # 可能正处在宿主任务的等待里：做完原样还回去
            self.nested, self.alias = True, extra.name
            try:
                self._run_extra(extra)
            finally:
                self.nested, self.alias = False, None
                session.bind(held_spec)
                session.bag_full = held_bag_full
                if held_task is not None:
                    activity.enter(ident, held_task, self.stop)
        return self.halted

    def _run_extra(self, extra: Account) -> None:
        """做一项插进来的任务。另一个存档的：同账号只能在线一个存档，先下线换过去做，做完再登录回来。"""
        session = self.session
        specs = sorted((load_tasks()[t] for t in extra.tasks), key=run_order)
        if extra.config.record_index == self.account.config.record_index:
            for spec in specs:
                self._extra(spec, extra)
            return
        slot = extra.config.record_index + 1
        mark = len(self.results)
        try:
            with session.other_save(slot) as other:
                other.peers, self.session = session.peers, other
                try:
                    for spec in specs:
                        self._extra(spec, extra)
                finally:
                    self.session = session
        except p.AllianceError as exc:
            for spec in specs:
                if not any(r.task == spec.id for r in self.results[mark:]):
                    self.record(TaskResult(spec.id, BLOCKED, f"换到存档 {slot} 失败：{login.explain(exc)}"))

    def _extra(self, spec: TaskSpec, owner: Account) -> None:
        """插进来的一个任务：前置步骤 → 本体 → 收尾一口气做完。它不进断点：暂停（背包满）按受阻记。"""
        def settled(r: TaskResult) -> TaskResult:
            return replace(r, status=BLOCKED, progress=None) if r.status == PAUSED else r

        if self.over:
            self.record(TaskResult(spec.id, SKIPPED, self._skip_why))
            return
        if late := self.past_midnight(spec):
            self.record(late)
            return
        result = self.execute(spec, spec.prelude, owner)[0] if spec.prelude else None
        if result is not None:
            self.record(settled(result))
            return
        result = self.execute(spec, spec.run, owner)[0] or TaskResult(spec.id, ERROR, "任务没有返回结果")
        self.record(settled(result))
        rounds = 0
        while result.retry_in is not None and result.pending:  # 插进来的任务没有“后面的任务”可先做：原地等完重跑
            rounds += 1
            if rounds > LATER_MAX or not self._idle(result.retry_in):
                why = f"已经等了 {LATER_MAX} 轮，不再等" if rounds > LATER_MAX else self._skip_why
                self.swap(replace(result, status=BLOCKED if rounds > LATER_MAX else SKIPPED, pending=False, retry_in=None,
                                  detail=_join(result.detail, why)))
                return
            result = settled(self.execute(spec, spec.run, owner)[0] or TaskResult(spec.id, ERROR, "任务没有返回结果"))
            self.swap(result)
        if spec.finale is not None and result.status not in (PAUSED, UNCERTAIN):
            final = self.execute(spec, spec.finale, owner)[0]
            if final is not None:
                self.merge(spec.id, settled(final))

    @property
    def _skip_why(self) -> str:
        return _SKIP_HALTED if self.halted else _SKIP_STOPPED

    def close(self) -> None:
        """本存档的任务都做完了：把还没做的插入任务做掉，之后不再接收。"""
        while True:
            if self.session is not None and not self.halted:
                self.interleave()
            with _RUNS_LOCK:
                if self.inbox and not self.over and self.session is not None:
                    continue
                self.accepting = False
                left, self.inbox = self.inbox, []
                if _RUNS.get(self.account.config.username) is self:
                    del _RUNS[self.account.config.username]
            for extra in left:
                for t in extra.tasks:
                    result = TaskResult(t, SKIPPED, self._skip_why)
                    self.results.append(result)
                    self.emit(extra.name, result)
            return

    def _pause_if_bag_full(self, spec: TaskSpec, ctx: TaskContext, result: TaskResult | None) -> TaskResult | None:
        """兜底：服务器拒绝过背包满，而任务没单独处理（可能吞成备注）→ 暂停，断点接着做；暂停结果带上断点进度。"""
        if self.session.bag_full or (result is not None and result.status == BLOCKED and is_bag_full(result.detail)):
            if result is None:
                result = TaskResult(spec.id, PAUSED, BAG_FULL_HINT)
            elif result.status not in (PAUSED, UNCERTAIN):
                detail = result.detail if BAG_FULL_HINT in result.detail else _join(result.detail, BAG_FULL_HINT)
                result = replace(result, status=PAUSED, detail=detail)
        if result is not None and result.status == PAUSED and result.progress is None and ctx.progress:
            result = replace(result, progress=dict(ctx.progress))
        return result

    def merge(self, task_id: str, new: TaskResult) -> None:
        """收尾的结果并入同一任务已有的结果。"""
        results = self.results
        prev = next((r for r in reversed(results) if r.task == task_id), None)
        if prev is None:
            self.record(new)
            return
        bad = new.status in (BLOCKED, ERROR, UNCERTAIN, PAUSED)
        if prev.pending:  # 临时结果只是占位：最终结果整个替换它
            status, detail = new.status, new.detail
        else:
            status = new.status if bad or (prev.status == SKIPPED and new.status == SUBMITTED) else prev.status
            detail = _join(prev.detail, new.detail)
        results[results.index(prev)] = merged = TaskResult(
            task_id, status, detail, prev.profile,
            todo=_join(prev.todo, new.todo),
            account_profile=new.account_profile or prev.account_profile,
            popup=new.popup or prev.popup,
        )
        self.emit_result(self.name, merged)

    def past_midnight(self, spec: TaskSpec) -> TaskResult | None:
        """这次执行已经跨过 0 点：不吃每日次数的任务以外都不再开始（见 daycut）。"""
        if spec.overnight:
            return None
        why = daycut.reason(self.session)
        return None if why is None else TaskResult(spec.id, SKIPPED, why)

    def preludes(self, chosen: list[TaskSpec]) -> None:
        """前置步骤：先于所有任务（含 first）；prelude_late 的排在其他前置步骤之后。"""
        for spec in sorted(chosen, key=lambda spec: spec.prelude_late):
            if spec.prelude is None or stopping(self.stop):
                continue
            if late := self.past_midnight(spec):
                self.record(late)
                self.aborted.add(spec.id)
                continue
            result, must_stop = self.execute(spec, spec.prelude)
            if result is not None:
                self.record(result)
                self.aborted.add(spec.id)
            if must_stop:
                self.halted = True
                return

    def finales(self, chosen: list[TaskSpec]) -> None:
        """收尾（领取）。"""
        for spec in chosen:
            if self.halted:
                return
            if spec.finale is None or spec.id in self.aborted or stopping(self.stop):
                continue
            result, must_stop = self.execute(spec, spec.finale)
            if result is not None:
                self.merge(spec.id, result)
            self.halted = self.halted or must_stop

    def bodies(self, chosen: list[TaskSpec]) -> None:
        """任务本体，按顺序；收尾插在第一个 last 任务之前，没有 last 任务就放在最后。"""
        finales_done = False
        ran = 0
        for spec in chosen:
            if self.halted or self.interleave():
                break
            if spec.last and not finales_done:
                finales_done = True
                self.finales(chosen)
                if self.halted:
                    break
            if spec.id in self.aborted:
                continue
            if stopping(self.stop):
                why = _SKIP_STOPPED if self.stopped else "已按要求打完这一场后停止"
                self.record(self.finals.get(spec.id) or TaskResult(spec.id, SKIPPED, why))
                continue
            if spec.prelude is None and (late := self.past_midnight(spec)):  # 有前置步骤的已经开始了，由任务自己在开始新一次前判断
                self.record(late)
                continue
            result, must_stop = self.execute(spec, spec.run)
            # 任务本体必须给结果；没给就是任务自身的 bug，别让整批跟着崩
            result = result or TaskResult(spec.id, ERROR, "任务没有返回结果")
            self.record(result)
            self.defer(spec, result, 0)
            self.halted = self.halted or must_stop  # 暂停也算：不领取，断点里会重跑
            ran += 1
            if self.account.tidy and ran % self.account.tidy == 0:
                self.tidy()
        self.laters()
        if not self.over:
            self.interleave()
        if not finales_done and not self.over:
            self.finales(chosen)
        if self.account.tidy == 0:
            self.tidy()

    def defer(self, spec: TaskSpec, result: TaskResult, rounds: int) -> None:
        if result.retry_in is not None and result.pending:
            self.later.append((time.monotonic() + max(0.0, result.retry_in), rounds + 1, spec))

    def laters(self) -> None:
        """后面的任务都做完了：回头重跑要过一会儿再来的任务（逃跑禁赛），没到时间就等（等的时候照常应答心跳、做插进来的任务）。
        同一个任务最多等 LATER_MAX 轮，再要等就按受阻结束。"""
        while self.later and not self.over:
            self.later.sort(key=lambda item: item[0])
            due, rounds, spec = self.later.pop(0)
            if not self._idle(due - time.monotonic()):
                break
            result, must_stop = self.execute(spec, spec.run)
            result = result or TaskResult(spec.id, ERROR, "任务没有返回结果")
            if result.retry_in is not None and result.pending and rounds >= LATER_MAX:
                result = replace(result, status=BLOCKED, pending=False, retry_in=None,
                                 detail=_join(result.detail, f"已经等了 {rounds} 轮，不再等"))
            self.swap(result)
            self.defer(spec, result, rounds)
            self.halted = self.halted or must_stop

    def swap(self, new: TaskResult) -> None:
        """重跑的结果整个换掉同一任务的占位结果（重跑会重新数场次、重读次数，不能拼接）。"""
        prev = next((i for i in range(len(self.results) - 1, -1, -1) if self.results[i].task == new.task), None)
        if prev is None:
            self.record(new)
            return
        self.results[prev] = new
        self.emit_result(self.name, replace(new, status=PROGRESS) if new.pending else new)

    def _idle(self, seconds: float) -> bool:
        """空等到点；返回 False = 停止了或必须中止。不要求一直在线：连接断了就干等。"""
        end = time.monotonic() + seconds
        while True:
            if self.interleave() or self.over:
                return False
            left = end - time.monotonic()
            if left <= 0:
                return True
            step = min(left, 0.5)
            session = self.session
            idle = getattr(session, "idle", None) if getattr(session, "live", False) is True else None
            if idle is None or not idle(step):
                if self.stop is not None and hasattr(self.stop, "wait"):
                    self.stop.wait(step)
                else:
                    time.sleep(step)

    def tidy(self) -> None:
        """同物归库（日常页底部的开关）：任务之间调用，不是任务列表里的任务。"""
        if self.over:
            return
        from .tasks import warehouse_sync

        spec = warehouse_sync.SPEC
        result, must_stop = self.execute(spec, spec.run)
        self.halted = self.halted or must_stop
        if result is not None:
            self.record(result)

    def settle(self) -> None:
        """没等到收尾的临时结果（被停止、前面出错中断）：任务自己报过最终结果（穿插着已经做完）就用那份，否则定成最终的“跳过”。
        报过最终结果、但本体还没轮到就中断了的，也补记上（界面上已经显示过）。"""
        for i, r in enumerate(self.results):
            if r.pending:
                final = self.finals.get(r.task)
                if final is None:
                    why = "未执行完（任务被停止或前面的任务中断）"
                    final = replace(r, status=SKIPPED, detail=_join(r.detail, why))
                self.results[i] = final
                self.emit_result(self.account.name, final)
        have = {r.task for r in self.results}
        self.results += [r for t, r in self.finals.items() if t in self.chosen and t not in have]


_RUNS: dict[str, "_AccountRun"] = {}  # 登录名 → 正在执行、可以插任务的那次执行（任务里有 hosts 的，如护送经书）
_RUNS_LOCK = threading.Lock()


def hosting(username: str) -> int | None:
    """这个登录账号现在是不是有执行正空等着、可以插任务：是 = 它所在的存档（界面编号 1-8），不是 = None。
    它在做任务（自己的，或者插进来还没做完的）时不算：那时界面照常锁着“执行”。"""
    with _RUNS_LOCK:
        run = _RUNS.get(username)
        idle = run is not None and run.accepting and run.waiting and not run.nested and not run.inbox and not run.halted
        return run.account.config.record_index + 1 if idle else None


def inject(account: Account) -> str | None:
    """把 account.tasks 插进同一登录账号正在跑的执行里（用 account 的选项），由那边的线程在穿插点做掉。
    返回 None = 已接收；否则是不能插的原因。别的存档的任务也能插：做的时候先下线换到那个存档，做完再登录回来。"""
    registry = load_tasks()
    with _RUNS_LOCK:
        run = _RUNS.get(account.config.username)
        if run is None or not run.accepting or run.halted:
            return "正在执行的任务已经结束"
        index = account.config.record_index
        taken = {t for extra in run.inbox if extra.config.record_index == index for t in extra.tasks}
        if index == run.account.config.record_index:
            taken |= set(run.chosen)
        tasks = tuple(t for t in account.tasks if t in registry and t not in taken)
        if not tasks:
            return "任务已经在执行或排队中"
        run.inbox.append(replace(account, tasks=tasks))
    return None


def run_account(
    account: Account,
    *,
    stop: threading.Event | None = None,
    emit: Callable[[str, TaskResult], None] = lambda name, r: None,
    day: list | None = None,
) -> list[TaskResult]:
    """顺序执行一个账号的所有任务。单个任务失败不影响后续；结果未知则停止该账号。
    day：同一次执行共用的 [开始日期]：空的就填上这次登录的服务器日期，之后的存档沿用（过 0 点保护，见 daycut）。"""
    from .tasks import warehouse_sync

    registry = {**load_tasks(), warehouse_sync.ID: warehouse_sync.SPEC}  # 同物归库不在任务列表里，但可以单独执行
    run = _AccountRun(account, stop, emit)
    chosen = [registry[t] for t in account.tasks if t in registry]
    chosen.sort(key=run_order)  # 稳定排序
    for t in account.tasks:
        if t not in registry:
            run.record(TaskResult(t, BLOCKED, "未知任务 id"))
    if not chosen:
        return run.results
    try:
        with _signed_in(account) as session:
            session.peers, session.owns_login = account.peers, True
            run.session, run.chosen = session, tuple(spec.id for spec in chosen)
            day = day if day is not None else [None]
            if day[0] is None:
                day[0] = daycut.day_of(getattr(session, "now", None))
            session.day = day[0]
            _tell_clock(getattr(session, "now", None))
            if any(spec.hosts for spec in chosen):
                with _RUNS_LOCK:
                    _RUNS[account.config.username], run.accepting = run, True
            try:
                run.preludes(chosen)
                run.bodies(chosen)
            finally:
                run.close()
    except p.AllianceError as exc:
        run.record(TaskResult("login", ERROR, f"登录/进入角色失败：{login.explain(exc)}"))
    run.settle()
    return run.results


# 排在后面的任务轮到时，同账号已经中断了的说明：{中断原因: (状态, 说明)}
_DEFERRED_HELD = {
    "paused": (PAUSED, "已顺延；同账号其他任务暂停，请点击“继续”后重试"),
    "uncertain": (SKIPPED, "同账号其他任务结果未知，本任务未执行；核对后再手动运行"),
    "stopped": (SKIPPED, "用户停止，本任务未执行"),
}
_LAST_HELD = {
    "paused": (PAUSED, "同账号其他存档任务暂停，在线任务未执行"),
    "uncertain": (SKIPPED, "同账号其他任务结果未知，在线任务未执行；核对后再手动运行"),
    "stopped": (SKIPPED, "用户停止，在线任务未执行"),
}


def _defer_deadline(spec: TaskSpec, now: datetime) -> datetime | None:
    """还没到点就返回该任务要等到的时刻（now、返回值都是服务器的北京时间，见 servertime.clock）。"""
    if spec.defer_until is None:
        return None
    hour, minute = spec.defer_until
    deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return deadline if now < deadline else None


class _LoginBatch:
    """run_batch 里同一个登录账号的全部执行项，在一个线程里按顺序做完。

    无论调用方怎样组装列表，都先完成账号级任务，再做各存档的任务；没到点的 defer_until 任务其他任务做完再等，
    after_saves 任务放到最后。"""

    def __init__(self, registry: Mapping[str, TaskSpec], stop: threading.Event | None,
                 emit: Callable[[str, TaskResult], None], collect: Callable[[str, list[TaskResult]], None]) -> None:
        self.registry, self.stop, self.emit, self.collect = registry, stop, emit, collect
        self.halt: str | None = None  # 同账号后面的任务为什么不做了：paused / uncertain / stopped
        self.day: list = [None]  # 这次执行开始那天（第一个存档登录时的服务器日期），各存档共用

    def run(self, group: list[Account]) -> None:
        last: list[tuple[Account, str]] = []
        if len(group) > 1:
            group, last = self._take_out(group, lambda spec: spec.after_saves and "等同一账号其他存档的任务完成后再等待在线时长")
        now = servertime.clock()

        def deferred_note(spec: TaskSpec) -> str | None:
            if _defer_deadline(spec, now) is None:
                return None
            hour, minute = spec.defer_until
            return f"任务已顺延至 {hour:02d}:{minute:02d}；其他任务继续执行"

        group, deferred = self._take_out(group, deferred_note)
        done = self._run_saves(group)
        for acc, tid in deferred:
            self._run_deferred(acc, tid, done)
        for acc, tid in last:
            if not self._held(acc, tid, _LAST_HELD):
                self._run_alone(acc, tid)

    def _take_out(self, group: list[Account], note: Callable[[TaskSpec], Any]) -> tuple[list[Account], list[tuple[Account, str]]]:
        """把 note 给出说明的任务从各执行项里拿出来（并显示说明）。返回 (剩下的执行项, 拿出来的任务)。"""
        kept: list[Account] = []
        taken: list[tuple[Account, str]] = []
        for acc in group:
            keep: list[str] = []
            for tid in acc.tasks:
                spec = self.registry.get(tid)
                why = note(spec) if spec is not None else None
                if why:
                    taken.append((acc, tid))
                    self.emit(acc.name, TaskResult(tid, PROGRESS, why))
                else:
                    keep.append(tid)
            if keep:
                kept.append(replace(acc, tasks=tuple(keep)))
        return kept, taken

    def _wide(self, acc: Account) -> list[str]:
        return [t for t in acc.tasks if t in self.registry and self.registry[t].account_wide]

    def _run_saves(self, group: list[Account]) -> set[str]:
        """按顺序做各执行项；返回已经做过的账号级任务（同一个登录账号只做一次）。"""
        group = sorted(group, key=lambda acc: not (acc.tasks and len(self._wide(acc)) == len(acc.tasks)))
        done: set[str] = set()
        for acc in group:
            if stopping(self.stop):
                self.halt = "stopped"
                break
            wide = self._wide(acc)
            repeated = [t for t in wide if t in done]
            done.update(wide)
            skipped = [TaskResult(t, SKIPPED, _SKIP_REPEAT) for t in repeated]
            for r in skipped:
                self.emit(acc.name, r)
            rest = replace(acc, tasks=tuple(t for t in acc.tasks if t not in repeated))
            self._finish(acc, skipped + self._attempt(rest))
            if self.halt:  # 暂停或结果未知：停止该登录账号后面的存档，其他账号不受影响
                break
        return done

    def _run_deferred(self, acc: Account, tid: str, done: set[str]) -> None:
        spec = self.registry[tid]
        if spec.account_wide:
            if tid in done:
                self._give(acc, tid, SKIPPED, _SKIP_REPEAT)
                return
            done.add(tid)
        if self._held(acc, tid, _DEFERRED_HELD):
            return
        if self._wait_for(spec):
            self._run_alone(acc, tid)
        else:
            self._give(acc, tid, SKIPPED, "等待期间用户停止，本任务未执行")

    def _wait_for(self, spec: TaskSpec) -> bool:
        """等到 defer_until 的时刻。返回 False = 等待中被停止。"""
        deadline = _defer_deadline(spec, servertime.clock())
        while deadline is not None:
            remaining = (deadline - servertime.clock()).total_seconds()
            if remaining <= 0:
                break
            if self.stop is None:
                time.sleep(min(remaining, 1.0))
            elif self.stop.wait(min(remaining, 1.0)):
                return False
        return not stopping(self.stop)

    def _held(self, acc: Account, tid: str, notes: Mapping[str, tuple[str, str]]) -> bool:
        """同账号已经中断（或用户刚停止）：这个任务不做，记下原因。"""
        halt = self.halt or ("stopped" if stopping(self.stop) else None)
        if halt is None:
            return False
        self._give(acc, tid, *notes[halt])
        return True

    def _give(self, acc: Account, tid: str, status: str, detail: str) -> None:
        result = TaskResult(tid, status, detail)
        self.emit(acc.name, result)
        self.collect(acc.name, [result])

    def _run_alone(self, acc: Account, tid: str) -> None:
        self._finish(acc, self._attempt(replace(acc, tasks=(tid,))))

    def _attempt(self, acc: Account) -> list[TaskResult]:
        """执行一项；一个账号线程崩溃不能影响其他账号。"""
        try:
            return run_account(acc, stop=self.stop, emit=self.emit, day=self.day)
        except ForceStop:  # 异步抛出时恰好不在某个任务里（登录中、任务之间）
            result = TaskResult("login", UNCERTAIN, FORCE_STOP_HINT)
        except Exception as exc:
            result = TaskResult("login", ERROR, f"{type(exc).__name__}: {exc}")
        self.emit(acc.name, result)
        return [result]

    def _finish(self, acc: Account, results: list[TaskResult]) -> None:
        self.collect(acc.name, results)
        statuses = {r.status for r in results}
        if UNCERTAIN in statuses:
            self.halt = "uncertain"
        elif PAUSED in statuses:
            self.halt = "paused"


def run_batch(
    accounts: Iterable[Account],
    *,
    stop: threading.Event | None = None,
    emit: Callable[[str, TaskResult], None] = lambda name, r: None,
) -> dict[str, list[TaskResult]]:
    """不同登录账号并行执行（每个登录账号一个线程）；同一登录账号的多个存档仍按顺序（同账号同时只能在线一个）。
    emit 会在多个线程里被调用，回调必须线程安全；结果按 emit 的账号名区分，日志天然分开。"""
    registry = load_tasks()
    groups: dict[str, list[Account]] = {}
    for acc in accounts:
        groups.setdefault(acc.config.username, []).append(acc)
    out: dict[str, list[TaskResult]] = {}
    lock = threading.Lock()

    def collect(name: str, results: list[TaskResult]) -> None:
        with lock:
            out[name] = [*out.get(name, ()), *results]

    def run_group(group: list[Account]) -> None:
        _LoginBatch(registry, stop, emit, collect).run(group)

    if len(groups) <= 1:
        for group in groups.values():
            run_group(group)
    else:
        threads = [threading.Thread(target=run_group, args=(g,), name=f"acct-{u}", daemon=True) for u, g in groups.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    order = {acc.name: i for i, acc in enumerate(a for g in groups.values() for a in g)}
    return dict(sorted(out.items(), key=lambda kv: order.get(kv[0], 0)))


def task_order(account: Account) -> list[str]:
    """该存档任务的实际执行顺序（和 run_account 一致：first 最前、账号级优先、terminal 最后）。"""
    registry = load_tasks()
    return [spec.id for spec in sorted((registry[t] for t in account.tasks if t in registry), key=run_order)]


def pending(accounts: Iterable[Account], results: Mapping[str, list[TaskResult]]) -> dict[str, list[str]]:
    """暂停后各存档还没做完的任务（含暂停的那个）。没跑到的存档 = 它的全部任务；跑完的不返回。
    跑过但不是暂停收场的存档（做完了，或因“结果未知”/登录失败中断）不进断点：后者要用户先核对，不能点“继续”就接着跑。"""
    accounts = list(accounts)
    uncertain_users = {acc.config.username for acc in accounts
                       if any(r.status == UNCERTAIN for r in results.get(acc.name, ()))}
    out: dict[str, list[str]] = {}
    for acc in accounts:
        if acc.config.username in uncertain_users:
            continue  # 同账号尚未开始的存档也须先人工核对，不能由“继续”绕过结果未知
        got = results.get(acc.name)
        order = task_order(acc)
        if got is None:
            left = order
        elif not any(r.status == PAUSED for r in got):
            continue
        else:  # 按实际完成状态判断；有收尾步骤不代表该任务仍未完成
            done = {r.task for r in got if r.status != PAUSED and not r.pending}
            left = [t for t in order if t not in done]
        if left:
            out[acc.name] = left
    return out
