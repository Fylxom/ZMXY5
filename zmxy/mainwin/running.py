"""开始和停止执行：后台线程、各账号的忙闲、断点继续、验证码和 Cookie 的线程交接。"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Callable, Iterable

from .. import core, login, protocol as p
from ..dialogs import CaptchaDialog
from ..widgets import ask_choice, ask_confirm, notice
from .rules import READERS, WEEKLY, Job, shown_name


SCOPE_NAMES = {"slot": "当前存档", "current": "当前账号", "all": "全部账号"}


def read_error(exc: Exception) -> str:
    """只读登录失败的说明：登录/网络错误翻译成用户能看懂的话。"""
    return login.explain(exc) if isinstance(exc, p.AllianceError) else f"{type(exc).__name__}: {exc}"


class RunningMixin:
    def _busy(self, user: str | None = None) -> bool:
        """这个登录账号（默认当前选中的）有没有执行在跑。别的账号在跑不算：各账号互不影响。"""
        user = self._user() if user is None else user
        return any(user in job.users for job in self.jobs)

    def _can_task(self) -> bool:
        """当前账号能不能点任务的“执行”：没在跑，或者在跑的是可以插任务的执行（护送经书等船）。"""
        return not self._busy() or core.hosting(self._user()) is not None

    def _read_target(self) -> tuple[dict[str, Any], int, Any] | None:
        """只读按钮（自动配置、时光之舟“读取”）共同的开头：能读就返回（账号条目, 存档, 执行项）。"""
        if self._busy() or not self._require_account():
            return None
        acc, slot = self._acc(), self.slot
        try:
            return acc, slot, self.store.to_account(acc, self._cookie_saver(acc), slot)
        except Exception as exc:
            notice(self, "账号配置错误", str(exc))
            return None

    def _spawn(self, work: Callable[[], None], users: Iterable[str], stop: threading.Event | None = None, timed: frozenset[str] = frozenset()) -> None:
        """开一个后台线程跑 work，占着 users 这些登录账号直到它结束（结束时发 jobdone）。timed：见 Job.timed。"""
        job = Job(frozenset(users), stop, timed)
        self.timed_names |= timed

        def body() -> None:
            try:
                work()
            finally:  # 无论怎么结束都要通知主线程，否则按钮会一直灰着
                self.events.put(("jobdone", job))

        self.jobs.append(job)
        self._refresh_busy()
        threading.Thread(target=body, daemon=True).start()

    def _stop_or_resume(self) -> None:
        """停止只管当前选中账号所在的那次执行；别的账号各自在跑的不受影响。"""
        jobs = [job for job in self.jobs if self._user() in job.users and job.stop is not None]
        if jobs:
            live = [self.specs[t] for t in sorted({t for job in jobs for t in core.engaged(job.stop)}) if t in self.specs]
            if live:  # 正打着一场（世界Boss、联盟Boss、斗神大会、灵宠乱斗）：问打完这场再停还是立即停。还在匹配、等房间的不问，直接停
                self._ask_stop(jobs, live)
                return
            for job in jobs:
                job.stop.set()
        elif not self._busy() and self.store.has_resume():
            self.resume()

    def _ask_stop(self, jobs: list[Job], live: list[core.TaskSpec]) -> None:
        names = "、".join(s.title for s in live)
        notes = "\n".join(f"· {s.title}：{s.force_warning}" for s in live if s.force_warning)
        answer = ask_choice(
            self, "停止", f"「{names}」正打着一场。\n\n打完这场再停：这一场照常打完、结算，然后停下，后面的任务不再做。\n"
            f"立即停止：马上中断。" + (f"\n{notes}" if notes else ""), (("立即停止", "stop"), ("打完这场再停", "primary")), "取消")
        if answer is None:
            return
        for job in jobs:
            if job not in self.jobs:  # 弹窗期间任务可能已经自己结束
                continue
            if answer == 1:
                self._add_log("—", "系统", "info", "用户选了打完这场再停")
                core.stop_after(job.stop)
            elif any(t in self.specs and self.specs[t].long_running for t in core.engaged(job.stop)):
                self._add_log("—", "系统", "info", "用户选了立即停止")  # 斗神大会、灵宠乱斗一场要等几分钟才响应普通停止：强制断开
                core.force_stop(job.stop)
            else:
                job.stop.set()

    def _sync_stop_btn(self, busy: bool = False) -> None:
        """运行中 = 停止；空闲且有断点 = 继续；否则停止（灰）。"""
        if not busy and self.store.has_resume():
            self.btn_stop.set_text("继续", "primary")
            self.btn_stop.set_enabled(True)
        else:
            self.btn_stop.set_text("停止", "stop")
            self.btn_stop.set_enabled(busy)

    def _index_targets(self) -> None:
        """执行项名字 → (账号条目, 存档)：日志写回、暂停断点都靠它找回存档。"""
        self.targets = {}
        for a in self.store.accounts:
            for s in range(1, 9):
                self.targets[self.store.slot_name(self.store.label(a), s)] = (a, s)
                self.targets[self.store.wide_name(self.store.label(a), s)] = (a, s)

    def resume(self) -> None:
        """从各存档的断点接着做。"""
        if self.jobs:  # 断点是全部账号一起接着做的：有账号还在跑就先等它
            notice(self, "提示", "还有账号在执行，结束后再点「继续」")
            return
        try:
            accounts = self.store.resume_accounts(self.wide, self._cookie_saver)
            self._index_targets()
        except Exception as exc:
            notice(self, "账号配置错误", str(exc))
            return
        accounts = self._with_peers(accounts)
        if not accounts:
            self.store.clear_resumes()
            self.store.save()
            self._sync_stop_btn()
            return
        self.store.clear_resumes()  # 再次暂停时会重新记下
        self.store.save()
        self._launch(accounts, "继续（从断点）")

    def _jump_to_log(self, stay: bool = False) -> None:
        """开始执行（或把任务插进正在进行的执行）自动切到日志；时光之舟只兑换时留在周常页（stay），兑换并挑战照常跳，日常页底部的“跳转日志”可关。"""
        if not stay and self.jump_var.get():
            self.page.set("log")
            self._show_page()

    def _launch(self, accounts: list, label_: str, stay: bool = False, timed: frozenset[str] = frozenset()) -> None:
        stop = threading.Event()  # 每次执行各有各的停止标志
        self._add_log("—", "系统", "info", f"{label_}执行开始")
        self._jump_to_log(stay)
        self._spawn(lambda: self._work(accounts, stop), {a.config.username for a in accounts}, stop, timed)

    def _with_peers(self, accounts: list) -> list:
        """师徒试炼要同时登录另一个账号。"""
        peers = self.store.peer_resolver(self._cookie_saver)
        return [replace(a, peers=peers) for a in accounts]

    def start(self, only: str | None = None, here: bool = False, what: str = "", stay: bool = False, tasks: list[str] | None = None) -> None:
        """only：只执行这一个任务（忽略勾选状态）；None = 执行已勾选的全部任务。
        单个任务（only）只对当前账号的当前存档或账号级目标存档，不看范围；here：其他情况也只对当前账号；what：确认框里的说明；tasks：只执行这几个任务（普通任务按当前存档，账号级任务按目标存档，不看勾选，不弹确认）。
        正在执行的账号不再另开一次：它在跑的是可以插任务的执行（护送经书等船）就把任务插进去，否则跳过；其他账号照常开始。"""
        picked = self._picked_accounts(all_scope=self.scope.get() == "all" and not here and not only)
        if not picked:
            notice(self, "提示", "请先添加并选择账号")
            return
        specific = bool(only or tasks)  # 单个/指定任务：忽略勾选
        try:
            accounts = self._save_accounts(picked, specific)
            self._index_targets()
            wide = self._merged_wide(picked, tasks) if tasks else self.store.wide_accounts(picked, self.wide, self._cookie_saver, only)
        except Exception as exc:
            notice(self, "账号配置错误", str(exc))
            return
        accounts = self._with_peers(wide + [a for a in self._narrow_tasks(accounts, only, tasks) if a.tasks])  # “其他”账号级任务先于各存档日常任务
        if not accounts:
            notice(self, "提示", "所选账号没有勾选任务")
            return
        if (what or not specific) and not self._confirm_start(what):  # 单个任务只对当前账号的当前存档，不再确认
            return
        self._begin(accounts, specific, stay)

    def _picked_accounts(self, all_scope: bool) -> list[dict[str, Any]]:
        if all_scope:
            return list(self.store.accounts)
        return [self._acc()] if self.current is not None else []

    def _begin(self, accounts: list, specific: bool, stay: bool) -> None:
        """空闲的账号开始执行；在跑的账号插任务或跳过。"""
        fresh = [a for a in accounts if not self._busy(a.config.username)]
        refused, inserted = self._insert_busy([a for a in accounts if a not in fresh])
        if refused and specific and not fresh:  # 点的是单个任务却没做成：弹出来说明原因
            notice(self, "提示", "\n".join(refused))
        if not fresh:
            if inserted:
                self._jump_to_log(stay)
            return
        self.store.clear_resumes()  # 重新执行 = 作废旧断点
        self.store.save()
        self._launch(fresh, "", stay)

    def _save_accounts(self, picked: list[dict[str, Any]], one_save: bool) -> list:
        """各存档的执行项。one_save：单个/指定任务，只要当前查看的存档（忽略勾选）。"""
        if one_save:
            return [self.store.to_account(a, self._cookie_saver(a), self.slot) for a in picked]
        if self.scope.get() == "slot":  # 当前存档：只跑正在查看的这个存档的勾选
            return [x for a in picked for x in self.store.run_accounts(a, self._cookie_saver(a), [self.slot])]
        return [x for a in picked for x in self.store.run_accounts(a, self._cookie_saver(a))]  # 每个勾选了任务的存档各登录一次

    def _merged_wide(self, picked: list[dict[str, Any]], tasks: list[str]) -> list:
        """指定的账号级任务：落在同一个存档上的并成一次登录。"""
        merged = {}
        for tid in tasks:
            if tid not in self.wide:
                continue
            for item in self.store.wide_accounts(picked, self.wide, self._cookie_saver, only=tid):
                key = (item.config.username, item.config.record_index)
                previous = merged.get(key)
                merged[key] = item if previous is None else replace(
                    previous,
                    tasks=tuple(dict.fromkeys((*previous.tasks, *item.tasks))),
                    options={**previous.options, **item.options},
                )
        return list(merged.values())

    def _narrow_tasks(self, accounts: list, only: str | None, tasks: list[str] | None) -> list:
        """各存档的执行项只留这次要做的任务。"""
        if tasks:
            regular_tasks = tuple(t for t in tasks if t not in self.wide)
            return [replace(a, tasks=regular_tasks) for a in accounts]  # 账号级周常走其选定存档，普通周常按当前存档及归库设置执行
        if only:  # 单个任务：账号级任务只走 wide（用它自己选的存档），普通任务对每个选中存档执行
            return [] if only in self.wide else [replace(a, tasks=(only,), tidy=None) for a in accounts]  # 单个任务不归库
        return [replace(a, tasks=tuple(t for t in a.tasks if t not in self.wide and getattr(self.specs.get(t), "group", None) != WEEKLY)) for a in accounts]

    def _confirm_start(self, what: str) -> bool:
        if what:
            return ask_confirm(self, "确认兑换", f"{what}，继续？", "兑换", "取消")
        return ask_confirm(self, "确认执行", f"将对{SCOPE_NAMES[self.scope.get()]}执行，继续？", "执行", "取消")

    def _insert_busy(self, busy: list) -> tuple[list[str], bool]:
        """正在执行的账号：能插就插，不能插记下原因。返回（没做成的说明, 有没有插进去的）。"""
        refused, inserted = [], False
        for a in busy:
            name, titles = shown_name(a.name), "、".join(self._title(t) for t in a.tasks)
            why = core.inject(a) if core.hosting(a.config.username) is not None else "这个账号正在执行任务，结束后再试"
            if why is None:
                inserted = True
                self._add_log(name, "系统", "info", f"已插入正在进行的执行：{titles}")
            else:
                refused.append(f"{name}：{why}")
                self._add_log(name, "系统", "info", f"没有执行「{titles}」：{why}")
        return refused, inserted

    def _ask_captcha(self, image: bytes) -> str | None:
        """在工作线程里调用：请主线程弹出验证码框，阻塞到用户回答（或超时放弃）。"""
        if self.background:
            return None  # 无人值守不弹验证码，也不等待五分钟或盲目重试
        box: dict[str, Any] = {"done": threading.Event(), "answer": None}
        self.events.put(("captcha", (image, box)))
        box["done"].wait(300)
        return box["answer"]

    def _show_captcha(self, image: bytes, box: dict[str, Any]) -> None:
        dialog = CaptchaDialog(self, image)
        dialog.run()
        box["answer"] = dialog.answer
        box["done"].set()

    def _cookie_saver(self, acc: dict[str, Any]) -> Callable[[str], None]:
        def save(cookie: str) -> None:  # 在工作线程里调用，只入队，真正写盘在主线程
            self.events.put(("cookie", (acc, cookie)))
        return save

    def _work(self, accounts: list, stop: threading.Event) -> None:
        try:
            results = core.run_batch(accounts, stop=stop,
                                     emit=lambda name, r: self.events.put(("result", (name, r))))
            if any(r.status == core.PAUSED for rs in results.values() for r in rs):
                self.events.put(("paused", core.pending(accounts, results)))
        except core.ForceStop:  # 强制停止的异常恰好落在任务之外：照常收尾
            pass
        except Exception as exc:
            self.events.put(("fatal", str(exc)))

    def _busy_now(self) -> tuple:
        return self._user(), self._busy(), self._can_task(), bool(self.jobs)

    def _refresh_busy(self) -> None:
        """按当前选中账号的状态刷按钮：它在执行就变灰（别的账号在跑不管）；在跑的执行可以插任务时，任务的“执行”照样能点。"""
        self._busy_sig = sig = self._busy_now()
        _user, busy, can, any_job = sig
        self.btn_run.set_enabled(not busy)
        self.btn_one.set_enabled(can and self.picked_task is not None)
        for key, btn in self.exec_btns.items():
            btn.set_enabled(not busy if key in READERS else can)
        self._sync_stop_btn(busy)
        self.pill.set_busy(any_job)
