"""定时的执行端：Windows 后台登记、到点排队、等联网、执行、网络失败重试，以及“日常和活动”“周常”两个定时项怎么组执行项。
设置和到点规则在 globalcfg / timeplan，页面在 globalpage。"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import core, globalcfg, netcheck, scheduling, servertime, timeplan, timerlog
from ..taskdef import unpicked
from ..tasks import last_rewards, time_boat
from ..widgets import STATUS_LABEL, SUB, WARN, RButton, hbox, label, set_fg, set_tip, vbox
from .parts import bring_front, copy_all_button, text_view
from .rules import WEEKLY, weekly_auto_run_ids
from .running import read_error

NET_TRUST = 120  # 秒：确认联网后这么久内不再查
NET_RETRY = 15  # 秒：没联网时隔多久再看一次
NET_GIVE_UP = 60 * 60  # 秒：一直没联网，等这么久就放弃这一轮

Queued = tuple[dict[str, Any], "int | None", str]  # (账号条目, 存档（账号级 / 按时刻的定时项 = None）, 任务 id 或 global:定时项)


class TimersMixin:
    # ---- Windows 后台登记
    def _sync_schedule(self) -> None:
        """系统登记在后台做；只在启动或总开关状态变化时同步，不阻塞界面。"""
        if not self._system_timers or self._schedule_busy:
            return
        active = scheduling.enabled(self.store)
        if self._schedule_applied == active:
            return
        self._schedule_busy = True
        path = self.store.path  # 只传是否开启和设置路径，不让工作线程读主线程正在编辑的配置

        def work() -> None:
            error = ""
            try:
                command, directory = scheduling.launch_command() if active else ([], path.parent)
                scheduling.register(scheduling.task_name(path), scheduling.task_xml(command, directory) if active else None)
            except Exception as exc:
                error = str(exc)
            self.events.put(("schedule", (active, error)))
        threading.Thread(target=work, name="daily-schedule", daemon=True).start()

    def _on_schedule(self, data: tuple[bool, str]) -> None:
        active, error = data
        self._schedule_busy, self._schedule_applied = False, active
        self._schedule_text = (f"后台定时登记失败，关闭程序后无法执行：{error}" if error else
                               "后台定时已启用，关闭程序后仍会执行" if active else self._idle_reason())
        self._schedule_error = bool(error)
        self._show_schedule()
        if error:
            self._timed_note("—", "error", self._schedule_text)
        elif active != scheduling.enabled(self.store):  # 登记期间又改了开关 / 勾选：那次同步被跳过了，补一次
            self._sync_schedule()

    def _idle_reason(self) -> str:
        names = globalcfg.unpicked_items(self.store)
        if names:
            return f"后台定时未启用：{'、'.join(names)} 开着，但下面的表里没勾存档"
        return "后台定时未启用：没有开着的定时项"

    def _show_schedule(self) -> None:
        if self.schedule_status is None:
            return
        self.schedule_status.setText(self._schedule_text)
        set_fg(self.schedule_status, WARN if self._schedule_error else SUB)
        if self._schedule_applied and not self._schedule_error:
            command, _directory = scheduling.launch_command()
            set_tip(self.schedule_status, f"运行路径：{command[0]}\n移动程序后重新打开一次以更新登记。")
        else:
            set_tip(self.schedule_status, "")

    # ---- 记录
    def _timed_note(self, name: str, status: str, detail: str) -> None:
        """定时自己的说明（到点、排队、没执行）：日志页和定时记录各记一条。"""
        self._add_log(name, "定时", status, detail)
        timerlog.write(name, "定时", STATUS_LABEL.get(status, status), detail)

    def _on_timed_note(self, data: tuple[str, str, str]) -> None:
        self._timed_note(*data)

    def open_timer_log(self) -> None:
        """“记录”：最近几天定时执行的全部记录（文本，可以划选复制）。已经开着就提到最前并刷新。"""
        win = self.timer_log_win
        if win is None:
            win = self.timer_log_win = QWidget(self, Qt.Window)
            win.setWindowTitle(f"定时记录 · 最近 {timerlog.KEEP_DAYS} 天")
            win.resize(820, 500)
            col = vbox(win)
            win.text = text_view()
            col.addWidget(win.text, 1)
            foot = hbox(margins=(10, 6, 10, 6))
            where = label(str(timerlog.path()), 9, SUB)
            where.setTextInteractionFlags(Qt.TextSelectableByMouse)
            where.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            foot.addWidget(where, 1)
            foot.addWidget(RButton("刷新", self.open_timer_log, "ghost", height=24, size=9))
            foot.addWidget(copy_all_button(win.text))
            col.addLayout(foot)
        win.text.setPlainText(timerlog.read() or "还没有定时执行的记录")
        bring_front(win)
        win.text.verticalScrollBar().setValue(win.text.verticalScrollBar().maximum())

    # ---- 到点
    def _tick_timers(self) -> None:
        """看一眼有没有到点的定时项：一个登录账号一次只开始一个，账号正在执行的留在队列里等它结束。"""
        items = globalcfg.due(self.store)
        retries = globalcfg.retries_due(self.store)
        if items or retries:
            self.store.save()  # 记下已到点：重开程序不会再做一遍
        due: list[Queued] = []
        for item in items:
            if (item in globalcfg.GROWTH_ITEMS and item in globalcfg.CLOCK_ITEMS and globalcfg.section(self.store, item).get("growth") is True
                    and self._growth_wait(item)):
                continue
            due += self._item_due(item)
        for item, key in retries:
            due += self._retry_due(item, key)
        for acc, _slot, tid in due:
            if self._busy(acc.get("username", "")):
                self._timed_note(self.store.label(acc), "info", f"「{self._title(tid)}」到点了，账号正在执行，结束后补做")
        self.timer_queue += due
        if self.timer_queue and not self._net_ready():
            return  # 开机刚登录时网络常常还没连上：等联网再登录，不然必然失败、当天也不会再做
        waiting, started = [], set()
        for queued in self.timer_queue:
            acc, slot, tid = queued
            user = acc.get("username", "")
            if not any(a is acc for a in self.store.accounts):  # 等的时候账号被删了
                continue
            if user in started or not self._run_timed(acc, slot, tid):
                waiting.append(queued)
            else:
                started.add(user)
        self.timer_queue = waiting

    def _item_due(self, item: str) -> list[Queued]:
        """一个定时项到点：按时刻的每个账号排一项（同账号的几个存档在一次执行里依次做）；按开放时段的每个存档排一项（账号级每账号一项）。"""
        if item in globalcfg.CLOCK_ITEMS:
            return [(acc, None, globalcfg.queue_id(item)) for acc, _slots in globalcfg.targets(self.store, item)]
        return [(acc, slot, item) for acc, slots in globalcfg.targets(self.store, item) for slot in slots]

    def _retry_due(self, item: str, key: str) -> list[Queued]:
        user, slot = globalcfg.split_key(key)
        return [(acc, slot, item) for acc, slots in globalcfg.targets(self.store, item)
                if acc.get("username") == user and slot in slots]

    def _net_ready(self) -> bool:
        """网络通了没有（不阻塞界面）：最近确认过就算通；否则在后台查，查到之前先不开始。等太久就放弃这一轮。"""
        now = time.monotonic()
        if now < self._net_ok_until:
            return True
        if self._net_wait_since is None:
            self._net_wait_since = now
        elif now - self._net_wait_since > NET_GIVE_UP:
            for acc, _slot, tid in self.timer_queue:
                item = globalcfg.kind_of(tid)
                if item is not None:  # 按时刻的当天做完就行：不算到过点，之后再试
                    globalcfg.unmark(self.store, item)
                self._timed_note(self.store.label(acc), "error", f"「{self._title(tid)}」等了 {NET_GIVE_UP // 60} 分钟网络一直没连上，"
                                 + ("这次先不做，之后再试" if item else "这次不做了"))
            self.store.save()
            self.timer_queue, self._net_wait_since, self._net_noted = [], None, False
            return False
        if not self._net_checking and now - self._net_checked >= NET_RETRY:
            self._net_checking, self._net_checked = True, now
            threading.Thread(target=lambda: self.events.put(("net", netcheck.reachable())), name="net-check", daemon=True).start()
        return False

    def _on_net(self, ok: bool) -> None:
        self._net_checking = False
        if not ok:
            if not self._net_noted:
                self._net_noted = True
                self._timed_note("—", "info", f"定时任务到点了，但网络还没连上；联网后再补做（每 {NET_RETRY} 秒看一次）")
            return
        self._net_ok_until = time.monotonic() + NET_TRUST
        if self._net_noted:
            self._timed_note("—", "info", "网络已连上，开始补做")
        self._net_wait_since, self._net_noted = None, False
        self._tick_timers()

    def _timed_failed(self, name: str, r: core.TaskResult) -> None:
        """按开放时段的定时登录时网络失败：在同一开放时段里给这个存档约好下一次（timeplan.schedule_retry），记进定时记录。"""
        queued = self.timed_items.get(name)
        if queued is None or r.task != "login" or r.status != core.ERROR or not timeplan.network_failure(r.detail or ""):
            return
        acc, slot, tid = queued
        if tid not in timeplan.TIMER_IDS:
            return
        cfg = globalcfg.section(self.store, tid)
        key = globalcfg.retry_key(acc.get("username", ""), slot)
        at = timeplan.schedule_retry(tid, cfg, key, servertime.clock())
        self.store.save()
        n = ((cfg.get("retry") or {}).get(key) or {}).get("n")
        self._timed_note(self.store.label(acc), "info", f"「{self._title(tid)}」连不上服务器，" +
                         (f"{at} 第 {n} 次重试" if at else "重试次数用完或这个时段来不及了，这次不做了"))

    # ---- 按养成周（周常、上古蛮荒）：新的养成周开始那天等游戏更新
    def _growth_wait(self, item: str) -> bool:
        """新的养成周照表该开始了、游戏还没更新：先不做。8 点以前不看（约到 8 点），之后在后台读线上版本再定。返回 True = 这次先不排。"""
        clock, title = servertime.clock(), globalcfg.TITLES[item]
        if not globalcfg.waiting_update(servertime.now(), None, globalcfg.growth_week_of(item)):
            return False
        if clock.hour * 60 + clock.minute < globalcfg.CHECK_FROM:
            at = globalcfg.schedule_check(self.store, item, clock)
            self.store.save()
            self._timed_note("定时", "info", f"{title}：新的养成周今天开始，游戏一般早上更新，先不做；{at} 起每 {globalcfg.RETRY_MINUTES} 分钟看一次")
            return True
        self._check_growth(item)
        return True

    def _check_growth(self, item: str) -> None:
        """后台重读线上版本，看游戏更新到新的养成周没有。"""
        if item in self._growth_checking:
            return
        self._growth_checking.add(item)
        self._global_checking = True

        def work() -> None:
            from .. import online
            waiting = True
            try:
                give_up = time.monotonic() + NET_GIVE_UP
                while not netcheck.reachable() and time.monotonic() < give_up:  # 开机刚登录还没联网：等联网再读线上版本
                    time.sleep(NET_RETRY)
                online.expire()  # 等更新时每次都要读新的，不用一小时内的缓存
                now = int(servertime.now())
                waiting = globalcfg.waiting_update(now, online.growth_week(now), globalcfg.growth_week_of(item))
            finally:
                self.events.put(("growth_check", (item, waiting)))
        threading.Thread(target=work, name="growth-check", daemon=True).start()

    def _on_growth_check(self, data: tuple[str, bool]) -> None:
        item, waiting = data
        self._growth_checking.discard(item)
        self._global_checking = bool(self._growth_checking)
        if waiting:
            at = globalcfg.schedule_check(self.store, item)
            self.store.save()
            self._timed_note("定时", "info", f"{globalcfg.TITLES[item]}：新的养成周今天开始，游戏还没更新，先不做；" + (f"{at} 再看一次" if at else "今天不再看"))
            self.refresh_global_status()
            return
        self.timer_queue += self._item_due(item)
        self._tick_timers()

    # ---- 执行
    def _run_timed(self, acc: dict[str, Any], slot: int | None, tid: str) -> bool:
        """到点执行一项，不弹任何框。返回 False = 账号正在执行，下次再试。"""
        name, title, user = self.store.label(acc), self._title(tid), acc.get("username", "")
        item = globalcfg.kind_of(tid)
        try:
            if item is not None:  # 按时刻的定时项：存档用定时里选的，任务和选项用页面上的
                found = dict((a.get("username"), s) for a, s in globalcfg.targets(self.store, item))
                if user not in found:  # 等的时候被关掉了
                    return True
                if item == globalcfg.WEEKLY:
                    return self._run_weekly_timed(acc, found[user])
                accounts = (self._daily_accounts(acc, found[user]) if item == globalcfg.DAILY
                            else self._ancient_accounts(acc, found[user]) if item == globalcfg.ANCIENT
                            else self.store.wide_accounts([acc], self.wide, self._cookie_saver, globalcfg.ALLIANCE))
                if not accounts:
                    self._timed_note(name, "info", f"「{title}」选中的存档都没有勾选任务，没有执行")
                    return True
            elif slot is None:  # 账号级任务：用它自己在页面上选的存档
                if self._unpicked(acc, None, tid):
                    self._timed_note(name, "info", f"「{title}」该选的没选（{self._pick_word(tid)}），没有执行")
                    return True
                accounts = self.store.wide_accounts([acc], self.wide, self._cookie_saver, tid)
                if tid == timeplan.WORLD and globalcfg.section(self.store, tid).get("growth") is True:  # 按养成周：不看次数框，进角后现算
                    accounts = [replace(a, options={**a.options, tid: {**{k: v for k, v in a.options.get(tid, {}).items() if k != "times"},
                                                                       "plan": "growth"}}) for a in accounts]
            else:
                accounts = [replace(self.store.to_account(acc, self._cookie_saver(acc), slot), tasks=(tid,), tidy=None)]
        except Exception as exc:
            self._timed_note(name, "error", f"「{title}」没有执行：{exc}")
            return True
        accounts = self._with_peers(accounts)
        if self._busy(user):  # 在跑的是可以插任务的执行（护送经书等船）就插进去
            if len(accounts) != 1 or core.hosting(user) is None or core.inject(accounts[0]) is not None:
                return False
            self.timed_inserted.add((accounts[0].name, tid))
            self._timed_note(name, "info", f"「{title}」到点，已插入正在进行的执行")
            return True
        self._index_targets()
        if item is None:  # 登录时网络失败要知道是哪个定时，好约重试
            self.timed_items.update({a.name: (acc, slot, tid) for a in accounts})
        timerlog.write(name, "定时", STATUS_LABEL["info"], f"「{title}」到点，开始执行")
        self._launch(accounts, f"定时「{title}」", stay=True, timed=frozenset(a.name for a in accounts))  # 不抢页面：留在用户正在看的那一页
        return True

    def _daily_accounts(self, acc: dict[str, Any], slots: list[int]) -> list:
        """“日常和活动”：和对这个账号点“执行”一样（账号级任务先做、每账号一次，各存档做日常 / 活动页的勾选），只是存档换成定时里选的。
        跳过周常、有自己定时项的任务（斗神、通天河等），联盟贡献归“联盟贡献”定时管的也跳过。"""
        saver = self._cookie_saver(acc)
        out = []
        for item in self.store.wide_accounts([acc], self.wide, self._cookie_saver):
            tasks = tuple(t for t in item.tasks if self._daily_keep(acc, t) and not self._unpicked(acc, None, t))
            if tasks:
                out.append(replace(item, tasks=tasks))
        for slot in slots:
            missing = self.missing_picks(globalcfg.DAILY, acc, slot)
            if missing:
                self._timed_note(self.store.slot_name(self.store.label(acc), slot), "info", f"「日常和活动」该选的没选，不做：{'、'.join(missing)}")
            base = self.store.to_account(acc, saver, slot)
            tasks = tuple(t for t in base.tasks if t not in self.wide and self._daily_keep(acc, t) and not self._unpicked(acc, slot, t))
            if tasks:
                out.append(replace(base, tasks=tasks))
        return out

    def _daily_keep(self, acc: dict[str, Any], t: str) -> bool:
        """“日常和活动”管的任务：不是周常、没有自己的定时项，联盟贡献没归“联盟贡献”定时管。"""
        return (getattr(self.specs.get(t), "group", None) != WEEKLY and t not in timeplan.TIMER_IDS
                and not globalcfg.covers(self.store, acc, t))

    # ---- 该选的没选（规则 1）：定时不做这些任务，定时页表里把这一格标出来
    def _unpicked(self, acc: dict[str, Any], slot: int | None, tid: str) -> bool:
        """slot = None：账号级任务（选项按登录账号存）。"""
        spec = self.specs.get(tid)
        return spec is not None and unpicked(spec, self._task_opts(acc, self.store.save_view(acc, slot or 1), tid))

    def _weekly_on(self, acc: dict[str, Any], slot: int) -> set[str]:
        """这个存档周常页上打开了开关的任务（账号级的没有开关，算打开）。"""
        return set(self.store.save_view(acc, slot).get("weekly", ())) | set(self.wide)

    def _pick_word(self, tid: str) -> str:
        ch = self.specs[tid].choices
        return f"{self.specs[tid].title}没选{ch.noun}" if ch is not None and ch.items is not None and ch.noun else f"{self.specs[tid].title}没选"

    def missing_picks(self, item: str, acc: dict[str, Any], slot: int) -> list[str]:
        """这个存档参加这个定时项、到点却不做的：该选的没选的任务名；周常一个开关都没开也算。"""
        if item == globalcfg.WEEKLY:
            on = [t for t in self.weekly_run_ids if t not in self.wide and t not in timeplan.TIMER_IDS and t in self._weekly_on(acc, slot)
                  and not globalcfg.covers(self.store, acc, t, slot)]
            if not on:
                return ["周常页一个任务都没打开"]
            return [self._pick_word(t) for t in on if self._unpicked(acc, slot, t)]
        if item == globalcfg.DAILY:
            sv = self.store.save_view(acc, slot)
            tids = [t for t in sv.get("tasks", ()) if t in self.specs and t not in self.wide and self._daily_keep(acc, t)]
        elif item == globalcfg.ANCIENT or (item in globalcfg.WIDE_ITEMS and item in timeplan.TIMER_IDS):
            tids = [item]
        else:
            return []
        wide = item in self.wide
        return [self._pick_word(t) for t in tids if self._unpicked(acc, None if wide else slot, t)]

    def _ancient_accounts(self, acc: dict[str, Any], slots: list[int]) -> list:
        """“上古蛮荒”：选中的存档只做上古蛮荒，关卡用周常页上选的，次数进角后现算（plan：有几次打几次 / 按养成周留）。"""
        saver = self._cookie_saver(acc)
        plan = "growth" if globalcfg.section(self.store, globalcfg.ANCIENT).get("growth") is True else "all"
        out = []
        for slot in slots:
            if self._unpicked(acc, slot, globalcfg.ANCIENT):
                self._timed_note(self.store.slot_name(self.store.label(acc), slot), "info", "「上古蛮荒」周常页上没选关卡，这个存档不做")
                continue
            base = self.store.to_account(acc, saver, slot)
            options = {**base.options.get(globalcfg.ANCIENT, {}), "plan": plan}
            options.pop("times", None)  # 次数现算，不用页面上的
            out.append(replace(base, tasks=(globalcfg.ANCIENT,), options={globalcfg.ANCIENT: options}, tidy=None))
        return out

    def _run_weekly_timed(self, acc: dict[str, Any], slots: list[int]) -> bool:
        """“周常”到点：选中的存档依次做“自动配置并执行”（先只读次数，再执行次数大于 0 的周常、两个商店、没领的上届奖励）；
        有自己定时项的（世界Boss、通天河等）跳过。返回 False = 账号正在执行，等它结束。"""
        name, user = self.store.label(acc), acc.get("username", "")
        if self._busy(user):
            return False
        saver = self._cookie_saver(acc)
        run_ids = [t for t in self.weekly_run_ids if t not in timeplan.TIMER_IDS]
        bases = self._with_peers([self.store.to_account(acc, saver, slot) for slot in slots])
        wide_ids = [t for t in run_ids if t in self.wide]
        wides = self._with_peers(self._merged_wide([acc], wide_ids)) if wide_ids else []
        specs = [self.specs[t] for t in [*self.auto_ids, time_boat.ID] if t not in timeplan.TIMER_IDS]
        growth = globalcfg.section(self.store, globalcfg.WEEKLY).get("growth") is True
        skip = {slot: frozenset(t for t in run_ids if t not in self._weekly_on(acc, slot) or globalcfg.covers(self.store, acc, t, slot)
                                or self._unpicked(acc, None if t in self.wide else slot, t)) for slot in slots}
        plan = WeeklyPlan(acc, bases, wides, specs, growth, run_ids, frozenset(self.wide), skip)
        self._index_targets()
        stop = threading.Event()
        names = frozenset([a.name for a in bases] + [a.name for a in wides])
        self._add_log("—", "系统", "info", "定时「周常」执行开始")
        timerlog.write(name, "定时", STATUS_LABEL["info"], "「周常」到点，先自动配置再执行")
        self._jump_to_log(stay=True)
        self._spawn(lambda: self._weekly_work(plan, stop), {user}, stop, names)
        return True

    def _weekly_work(self, plan: "WeeklyPlan", stop: threading.Event) -> None:
        """工作线程：每个存档先自动配置（只读），再一起执行。读到的次数交给主线程存（auto_store）。"""
        runs, wide_times = [], {}
        for base in plan.bases:
            if stop.is_set():
                return
            slot = base.config.record_index + 1
            try:
                value = core.read_auto(base, plan.specs, plan.reserve)
            except Exception as exc:
                self.events.put(("timed_note", (base.name, "error", f"「周常」自动配置失败，这个存档不执行：{read_error(exc)}")))
                continue
            self.events.put(("auto_store", (plan.acc, slot, value)))
            ids, times = weekly_plan_ids(plan, slot, value)
            for t in ids:
                if t in plan.wide:
                    wide_times.setdefault(t, times.get(t))
            regular = [t for t in ids if t not in plan.wide]
            if regular:
                options = deepcopy(base.options)
                for t in regular:
                    if t in times:
                        options.setdefault(t, {})["times"] = times[t]
                runs.append(replace(base, tasks=tuple(regular), options=options))
            elif not ids:
                self.events.put(("timed_note", (base.name, "info", "「周常」没有需要执行的任务")))
        wides = []
        for item in plan.wides:
            tasks = tuple(t for t in item.tasks if t in wide_times)
            if tasks:
                options = deepcopy(item.options)
                for t in tasks:
                    if wide_times[t] is not None:
                        options.setdefault(t, {})["times"] = wide_times[t]
                wides.append(replace(item, tasks=tasks, options=options))
        if (runs or wides) and not stop.is_set():
            self._work(wides + runs, stop)


@dataclass(frozen=True)
class WeeklyPlan:
    """“周常”定时一个账号这次要做的（主线程里备好，工作线程只读）。"""
    acc: dict[str, Any]
    bases: list            # 各存档的执行项
    wides: list            # 账号级周常的执行项（按它自己选的存档）
    specs: list            # 自动配置要读的任务
    reserve: bool          # 按养成周留次数
    run_ids: list[str]     # 要考虑的任务（周常页“自动配置并执行”的，去掉有自己定时项的）
    wide: frozenset[str]   # 账号级任务
    skip: dict = field(default_factory=dict)  # 存档 → 不做的任务：归别的定时项管的（上古蛮荒定时勾了的）、该选的没选的


def weekly_plan_ids(plan: WeeklyPlan, slot: int, value: dict) -> tuple[list[str], dict[str, int]]:
    """一个存档自动配置的结果 → (要执行的任务, 各任务次数)；规则同周常页“自动配置并执行”，跳过 plan.skip 里的。"""
    times = {t: n for t, n in value.items() if isinstance(n, int) and not isinstance(n, bool)}
    state = value.get(last_rewards.ID)
    pending = state[1] if isinstance(state, tuple) and len(state) > 1 else None
    ids = weekly_auto_run_ids(plan.run_ids, {t: str(n) for t, n in times.items()}, pending is not False)
    return [t for t in ids if t not in plan.skip.get(slot, ())], times
