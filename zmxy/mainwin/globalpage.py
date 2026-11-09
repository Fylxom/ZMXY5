"""定时页：账号栏最上面的“定时”。所有定时项对全部账号 / 存档统一设置（规则见 globalcfg、timeplan），
只管什么时候做、哪些存档做；做什么、怎么做照各存档在日常 / 活动 / 周常页上的设置。

上面“时间”：每个定时项一行（时间设置、开关、立即执行）。下面“参加的存档”：一张表，一行一个存档、一列一个定时项打勾；
只列出参加了定时的存档和手动加的行（小号通常只有一行），点表头整列全勾 / 全不勾。新账号默认什么都不勾。
这一页不属于哪个存档：进来时收起页签、执行范围和存档选择，点任何一个账号就回到原来的页面。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QVBoxLayout, QWidget

from .. import globalcfg, online, servertime, state, timeplan, timerlog
from . import rules
from ..store import WEEKDAYS, parse_clock
from ..widgets import (SUB, Card, Check, Chip, ClockField, Field, RButton, Segmented, Select, Switch, clear_layout, hbox, hint, label,
                       set_tip, tip, vbox)

NAME_WIDTH = 108  # 时间设置里定时项名字的宽度
ROW_NAME_WIDTH = 150  # 表里“账号 · 存档 n”的宽度
CELL = 46  # 表里每列的宽度


class GlobalMixin:
    # ---- 进出定时页
    def show_global(self) -> None:
        if not self.global_view:
            self.global_view = True
            self.on_select()

    def _apply_global_view(self) -> None:
        """on_select 最后调用：按 global_view 切换主区域。先切页再搭内容：搭的时候出错也不会停在别的页上。"""
        on = self.global_view
        for w in (self.page, self.btn_one, self.pick_hint, self.scope, self.btn_run, self.slot_tag, *self.slot_segs):
            w.setVisible(not on)
        if on:
            self.title_lbl.setText("定时")
            self.subtitle.setText("对全部账号统一设置，改了自动保存；做什么照各存档页面上的设置")
            self.stack.setCurrentWidget(self.pages["global"])
            self._build_global()
        else:
            self._show_page()

    # ---- 页面
    def _build_global(self) -> None:
        page: QVBoxLayout = self.page_lays["global"]
        clear_layout(page)
        self._loading_global = True
        top = hbox(margins=(2, 0, 2, 8))
        top.addWidget(hint(self._timers_note()))
        top.addSpacing(10)
        self.schedule_status = label("", 9, SUB)
        self.schedule_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tip(self.schedule_status)  # 每次重建都是新的标签：先装上说明浮层，_show_schedule 才能改它的文字
        top.addWidget(self.schedule_status, 1)
        top.addWidget(RButton("记录", self.open_timer_log, "side", height=22, size=9, width=50))
        page.addLayout(top)
        self._show_schedule()
        self._build_times(page)
        head = hbox(margins=(2, 0, 2, 3))
        head.addWidget(label("参加的存档", 9, SUB))
        head.addSpacing(8)
        head.addWidget(hint("一行一个存档，要定时做的那一列打勾。只列打了勾的和手动加的行（最下面“添加”）；行尾 × 去掉这一行所有的勾。\n"
                            "联盟贡献、世界Boss、通天河是账号级：每个账号做一次，勾在哪一行就用那个存档（页面上“选存档”跟着改）。\n"
                            "橙色格子：勾了但有任务到点不做（鼠标停上去看是哪几个）——该选的没选（关卡、祖巫、船只或材料），"
                            "或者周常页一个开关都没开。\n"
                            "周常只做周常页上开着开关的任务。"))
        head.addSpacing(10)
        head.addWidget(label("点表头可整列全勾 / 全不勾", 9, SUB))  # 整页说一次，表头各自只提示全名
        head.addStretch(1)
        page.addLayout(head)
        card = Card(padx=12, pady=6)
        self.timer_table = QWidget()
        vbox(self.timer_table, margins=(0, 0, 0, 0))
        card.box().addWidget(self.timer_table)
        page.addWidget(card)
        page.addWidget(self._clock_note())
        page.addStretch(1)
        self._render_table()
        self._loading_global = False
        self.refresh_global_status()

    def _timers_note(self) -> str:
        lines = ["这里只管什么时候做、哪些存档做；做什么、怎么做照各存档日常 / 活动 / 周常页的设置。",
                 "",
                 "· 日常和活动：到点做日常页、活动页勾选的任务，同顶部“执行”；“其他”里的账号级任务每个账号一次。",
                 "  开着联盟贡献定时的账号跳过联盟贡献；有开放时间的（斗神、通天河等）各有一列，这里不做。",
                 "· 上古蛮荒：关卡用周常页选的，次数进角后现算，有几次打几次；开着“按养成周”就给装备宝石周留次数，开始那天攒满。",
                 "· 周常：选中的星期到点，做一遍周常页的“自动配置并执行”。世界Boss、通天河、报名各有一列，不在里面；"
                 "上古蛮荒那一列勾了的存档，这里跳过上古蛮荒。",
                 "  开着“按养成周”：养成周开始那天、最后一天也做；平时少用几次，开始那天攒满。"
                 "妖塔、十绝阵等最多攒 5 次，选的星期隔超过 5 天会浪费。",
                 "  养成周开始那天游戏没更新就先不做，早上 8 点起每 30 分钟看一次，更新了再做。",
                 "· 有开放时间的：选中午场、晚上场或都做，开场后几分钟开始。世界Boss周末 9–21 点按 15:00 分两场，“都做”= 中午做了晚上不做。",
                 "· 护送经书一条船 15 分钟：时段里能发几条发几条；发完或下一条回来时已关门就不等，经书留到下次领。",
                 "· 报名：报名期里开机联网就报，报过了跳过。",
                 "",
                 "时间按服务器时间。到点后不同账号同时做，同一账号依次做；账号正忙就等它结束。",
                 "电脑要开机并登录当前 Windows 用户。错过了：按时刻的当天开机联网后补做，有开放时间的在时段里补做；"
                 "连不上服务器时同一时段里过 5、15、30 分钟各再试一次。",
                 f"关了程序由 Windows 后台执行；移动程序后重新打开一次。定时结果留 {timerlog.KEEP_DAYS} 天，点“记录”看。"]
        return "\n".join(lines)

    def _clock_note(self) -> Any:
        if not servertime.known():
            text = "还没登录过游戏，暂时按本机时间；登录一次后自动换成服务器时间"
        else:
            off = round(servertime.offset())
            text = "本机时间和服务器一致" if off == 0 else f"服务器比本机{'快' if off > 0 else '慢'} {abs(off)} 秒"
        note = label(text, 9, SUB)
        note.setContentsMargins(2, 4, 0, 0)
        return note

    # ---- 时间：每个定时项一行
    def _build_times(self, page: QVBoxLayout) -> None:
        head = hbox(margins=(2, 0, 2, 3))
        head.addWidget(label("时间", 9, SUB))
        head.addStretch(1)
        page.addLayout(head)
        card = Card(padx=12, pady=4)
        grid = card.grid()  # 一项一行，各列上下对齐：名字 | 说明或每周 | 时间（靠右） | 开关 | 立即执行
        grid.setHorizontalSpacing(8)
        grid.setColumnStretch(1, 1)
        for r, item in enumerate(globalcfg.ITEMS):
            self._time_row(grid, r, item)
        page.addWidget(card)
        self.global_status = label("", 9, SUB)
        self.global_status.setWordWrap(True)
        self.global_status.setContentsMargins(2, 4, 2, 0)
        page.addWidget(self.global_status)
        page.addSpacing(8)

    def _time_row(self, grid: QGridLayout, r: int, item: str) -> None:
        cfg = globalcfg.section(self.store, item)
        grid.setRowMinimumHeight(r, 28)
        name = label(globalcfg.TITLES[item])
        name.setFixedWidth(NAME_WIDTH)
        if item in timeplan.TIMER_IDS:
            tip(name, f"开放时间：{timeplan.describe(item)}")
        grid.addWidget(name, r, 0)
        growth = self._growth_switch(item, cfg) if item == timeplan.WORLD else []  # 世界Boss那一行时间那一列放不下：按养成周放在星期后面
        if item in (globalcfg.WEEKLY, timeplan.WORLD):  # 选星期
            days = self._pack([label("每周", 9, SUB), 2, *self._day_chips(item, cfg)], stretch_end=True)
            for w in growth:
                days.addSpacing(w) if isinstance(w, int) else days.addWidget(w)
            days.setSpacing(3)
            grid.addLayout(days, r, 1)
        elif item in timeplan.TIMER_IDS:
            when = label(timeplan.describe(item), 9, SUB)
            when.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # 窗口窄时挤的是这段说明
            tip(when, timeplan.describe(item))
            grid.addWidget(when, r, 1)
        grid.addLayout(self._pack(self._item_controls(item, cfg), stretch_start=True), r, 2)
        grid.addWidget(Switch(cfg.get("on") is True, lambda k=item: self._toggle_global(k)), r, 3)
        grid.addWidget(RButton("立即执行", lambda k=item: self.run_global(k), "side", height=22, size=9, width=64), r, 4)

    @staticmethod
    def _pack(widgets: list[Any], stretch_start: bool = False, stretch_end: bool = False) -> Any:
        """一格里的几个控件排成一排（int = 间距）。"""
        box = hbox(spacing=6)
        if stretch_start:
            box.addStretch(1)
        for w in widgets:
            if isinstance(w, int):
                box.addSpacing(w)
            else:
                box.addWidget(w)
        if stretch_end:
            box.addStretch(1)
        return box

    def _growth_switch(self, item: str, cfg: dict[str, Any]) -> list[Any]:
        return [label("按养成周", 9, SUB), 2, Switch(cfg.get("growth") is True, lambda k=item: self._toggle_growth(k))]

    def _item_controls(self, item: str, cfg: dict[str, Any]) -> list[Any]:
        """时间设置（int = 间距）。上古蛮荒、周常的按养成周在执行时间前面。"""
        if item in globalcfg.CLOCK_ITEMS:
            growth = [*self._growth_switch(item, cfg), 12] if item in globalcfg.GROWTH_ITEMS else []
            clock = ClockField(on_change=lambda k=item: self._save_global_clock(k))
            clock.set(str(cfg.get("at")) if parse_clock(cfg.get("at")) is not None else globalcfg.DEFAULT_AT[item])
            self.global_clocks[item] = clock
            return [*growth, label("执行时间", 9, SUB), clock]
        if item in timeplan.SESSION_TASKS:
            seg = Segmented([(text, key) for key, text in timeplan.SESSIONS], timeplan.session_of(cfg) or timeplan.DEFAULT_SESSION,
                            lambda: None, seg_width=38, height=22, size=9)
            seg.command = lambda k=item, s=seg: self._save_session(k, s.get())
            box: dict[str, Field] = {}
            delay = box["f"] = Field(str(timeplan.delay_of(cfg)), width=34, height=22, on_change=lambda k=item: self._save_delay(k, box["f"].get()))
            return [seg, 6, label("开场后", 9, SUB), delay, label("分钟", 9, SUB)]
        return [label("报名期里开机就报", 9, SUB)]

    def _day_chips(self, item: str, cfg: dict[str, Any]) -> list[Chip]:
        picked = set(globalcfg.weekdays(cfg))
        chips = []
        for day, day_name in enumerate(WEEKDAYS):
            chip = Chip(day_name[1:], day in picked, lambda: None, width=22, height=21, on_card=True)
            chip.command = lambda c=chip, d=day, k=item: self._toggle_global_day(k, c, d)
            tip(chip, day_name)  # Qt 自带的 setToolTip 在这个窗口里不弹，用自己画的说明浮层
            chips.append(chip)
        return chips

    # ---- 参加的存档：一张表
    def _render_table(self) -> None:
        """表重画（加行、删行、整列、账号级的勾换行时）；页面其他部分不动，滚动位置不跳。"""
        lay = self.timer_table.layout()
        clear_layout(lay)
        self.table_chips: dict[tuple[str, str, int], Check] = {}
        accounts = list(globalcfg.users(self.store))
        if not accounts:
            lay.addWidget(label("还没有账号", 9, SUB))
            return
        grid = QGridLayout()
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(4)
        grid.setContentsMargins(0, 0, 0, 0)
        for c, item in enumerate(globalcfg.COLUMNS, start=1):
            btn = RButton(globalcfg.SHORT[item], lambda k=item: self._toggle_column(k), "ghost", height=22, size=9, width=CELL - 4)
            tip(btn, globalcfg.TITLES[item])
            grid.addWidget(btn, 0, c, Qt.AlignCenter)
            grid.setColumnMinimumWidth(c, CELL)
        rows = globalcfg.rows(self.store)
        for r, (acc, slot) in enumerate(rows, start=1):
            user = acc["username"]
            name = label(f"{self.store.label(acc)} · 存档 {slot}")
            name.setFixedWidth(ROW_NAME_WIDTH)
            grid.addWidget(name, r, 0)
            for c, item in enumerate(globalcfg.COLUMNS, start=1):
                chip = Check(False, lambda: None, width=CELL - 6, height=24)
                chip.command = lambda k=item, a=acc, n=slot: self._toggle_cell(k, a, n)
                tip(chip)
                self.table_chips[(item, user, slot)] = chip
                self._paint_cell(item, acc, slot)
                grid.addWidget(chip, r, c, Qt.AlignCenter)
            grid.addWidget(RButton("×", lambda u=user, n=slot: self._remove_row(u, n), "ghost", height=22, size=10, width=26),
                           r, len(globalcfg.COLUMNS) + 1, Qt.AlignCenter)
        grid.setColumnStretch(len(globalcfg.COLUMNS) + 2, 1)
        lay.addLayout(grid)
        if not rows:
            empty = label("还没有存档参加定时：在下面选账号和存档，点“添加”", 9, SUB)
            empty.setContentsMargins(0, 4, 0, 0)
            lay.addWidget(empty)
        add = hbox(spacing=6, margins=(0, 8, 0, 2))
        add.addWidget(label("添加", 9, SUB))
        names = [self.store.label(a) for a in accounts]
        self.row_account = Select(names, names[0], width=ROW_NAME_WIDTH - 40, height=24, size=9)
        add.addWidget(self.row_account)
        self.row_slot = Select([f"存档 {n}" for n in range(1, 9)], "存档 1", width=80, height=24, size=9)
        add.addWidget(self.row_slot)
        add.addWidget(RButton("添加", self._add_row, "side", height=24, size=9, width=50))
        add.addStretch(1)
        lay.addLayout(add)

    def _paint_cell(self, item: str, acc: dict[str, Any], slot: int) -> None:
        """一格的勾和颜色：勾了但该选的没选 = 橙色（规则 1：这些任务到点不做）。"""
        chip = self.table_chips[(item, acc["username"], slot)]
        on = globalcfg.ticked(self.store, item, acc["username"], slot)
        missing = self.missing_picks(item, acc, slot) if on else []
        chip.set(on)
        chip.mark(bool(missing))
        text = f"{self.store.label(acc)} · 存档 {slot} · {globalcfg.TITLES[item]}"
        set_tip(chip, text + (f"\n到点不做：{'、'.join(missing)}" if missing else ""))

    def _row_target(self) -> tuple[dict[str, Any], int] | None:
        accounts = list(globalcfg.users(self.store))
        names = [self.store.label(a) for a in accounts]
        if self.row_account.get() not in names:
            return None
        return accounts[names.index(self.row_account.get())], int(self.row_slot.get().rsplit(" ", 1)[-1])

    def _add_row(self) -> None:
        target = self._row_target()
        if target is None:
            return
        globalcfg.add_row(self.store, target[0]["username"], target[1])
        self._global_changed()
        self._render_table()

    def _remove_row(self, user: str, slot: int) -> None:
        globalcfg.remove_row(self.store, user, slot)
        self._global_changed()
        self._render_table()

    def _set_cell(self, item: str, acc: dict[str, Any], slot: int, on: bool) -> None:
        """改一格（不存盘、不重画）。账号级的勾 = 开着 + 页面上选存档改成这一行的存档。"""
        user = acc["username"]
        got = globalcfg.entry(self.store, item, user)
        if item in globalcfg.WIDE_ITEMS:
            if on:
                got["on"] = True
                page = self._wide_cfg(acc, item)  # 和页面上“选存档”是同一处设置
                page["record"], page["multi"] = slot, False  # 开着“多存档”时按星期换存档，表里一行勾不出来：改成固定这一个
            elif globalcfg.ticked(self.store, item, user, slot):
                got["on"] = False
        else:
            got["slots"] = sorted(({n for n in got["slots"] if isinstance(n, int)} - {slot}) | ({slot} if on else set()))

    def _toggle_cell(self, item: str, acc: dict[str, Any], slot: int) -> None:
        user = acc["username"]
        self._set_cell(item, acc, slot, not globalcfg.ticked(self.store, item, user, slot))
        self._global_changed()
        if item in globalcfg.WIDE_ITEMS:  # 同一账号别的行的勾跟着变；页面上的选存档摘要也变了
            for k, u, n in list(self.table_chips):
                if k == item and u == user:
                    self._paint_cell(item, acc, n)
            self.refresh_summaries()
        else:
            self._paint_cell(item, acc, slot)

    def _toggle_column(self, item: str) -> None:
        """整列：有勾就全取消，一个都没有就全勾上（账号级的每个账号勾一行：页面上选的存档那一行在表里就用它，否则用这个账号的第一行）。"""
        rows = globalcfg.rows(self.store)
        any_on = any(globalcfg.ticked(self.store, item, acc["username"], slot) for acc, slot in rows)
        done: set[str] = set()
        for acc, slot in rows:
            user = acc["username"]
            if any_on:
                self._set_cell(item, acc, slot, False)
            elif item not in globalcfg.WIDE_ITEMS:
                self._set_cell(item, acc, slot, True)
            elif user not in done:
                done.add(user)
                mine = [n for a, n in rows if a["username"] == user]
                record = globalcfg.page_record(self.store, user, item)
                self._set_cell(item, acc, record if record in mine else mine[0], True)
        self._global_changed()
        self.refresh_summaries()
        self._render_table()

    # ---- 保存
    def _global_changed(self) -> None:
        self.store.save()
        self._schedule_applied = None  # 后台定时要不要登记可能变了
        self._sync_schedule()
        self.refresh_global_status()

    def _save_global_clock(self, item: str) -> None:
        if self._loading_global:
            return
        at = parse_clock(self.global_clocks[item].get())
        if at is None:
            return
        clock = f"{at // 60:02d}:{at % 60:02d}"
        cfg = globalcfg.section(self.store, item)
        if cfg.get("at") != clock:
            cfg.pop("done", None)  # 改了时间：今天还能按新时间到点一次
            cfg.pop("check", None)
            cfg["at"] = clock
            self._global_changed()

    def _save_session(self, item: str, value: str) -> None:
        if not self._loading_global:
            globalcfg.section(self.store, item)["session"] = value
            self._global_changed()

    def _save_delay(self, item: str, raw: str) -> None:
        raw = raw.strip()
        if self._loading_global or not (raw.isascii() and raw.isdigit() and int(raw) <= timeplan.MAX_DELAY):
            return  # 写得不对（不是 0–120 的整数）先不存
        globalcfg.section(self.store, item)["delay"] = int(raw)
        self._global_changed()

    def _toggle_global(self, item: str) -> None:
        cfg = globalcfg.section(self.store, item)
        cfg["on"] = cfg.get("on") is not True
        cfg.pop("retry", None)
        cfg.pop("check", None)
        self._global_changed()

    def _toggle_growth(self, item: str) -> None:
        cfg = globalcfg.section(self.store, item)
        cfg["growth"] = cfg.get("growth") is not True
        cfg.pop("check", None)
        self._global_changed()

    def _toggle_global_day(self, item: str, chip: Chip, day: int) -> None:
        cfg = globalcfg.section(self.store, item)
        chip.set(not chip.on)
        cfg["weekdays"] = sorted((set(globalcfg.weekdays(cfg)) - {day}) | ({day} if chip.on else set()))
        self._global_changed()

    # ---- 状态和立即执行
    def refresh_global_status(self) -> None:
        """时间设置下面的一行说明：按养成周时今天怎么做（不联网，用已经读过的线上版本）。"""
        if not self.global_view or self.global_status is None:
            return
        parts = [text for item in globalcfg.GROWTH_ITEMS if (text := self._growth_text(item))]
        self.global_status.setText("\n".join(parts))
        self.global_status.setVisible(bool(parts))

    def _growth_text(self, item: str) -> str:
        cfg = globalcfg.section(self.store, item)
        if cfg.get("on") is not True or cfg.get("growth") is not True:
            return ""
        now = servertime.now()
        live = online.known_growth_week(now)
        week = globalcfg.growth_week_of(item)
        found = state.growth_reserve(now, live=live)
        day = globalcfg.growth_day(servertime.clock()) if item == globalcfg.WEEKLY else None
        title = globalcfg.TITLES[item]
        if globalcfg.waiting_update(now, live, week):
            text = (f"{title}：翅膀光环养成周今天开始，游戏更新前到点的场次不打，次数全部留着" if item == timeplan.WORLD else
                    f"{title}：新的养成周今天开始，游戏更新后再做（早上 8 点起每 30 分钟看一次），之前相关次数全部留着")
        elif found is not None and (week is None or found[0] == week) and (keep := self._keep_text(item, found)):
            text = f"{title}：{state.GROWTH_WEEKS[found[0] - 1]}养成周快开始了，今天留 {keep}，开始那天正好攒满"
        elif day == "start":
            text = f"{title}：今天养成周开始，不管选没选今天都做一次"
        elif day == "end":
            text = f"{title}：今天是养成周最后一天，不管选没选今天都做一次，把次数用完"
        else:
            return ""
        check = cfg.get("check")
        if isinstance(check, dict) and isinstance(check.get("at"), int):
            text += f"；下次查看 {check['at'] // 60:02d}:{check['at'] % 60:02d}"
        return text

    def _keep_text(self, item: str, found: tuple[int, int]) -> str:
        """今天留哪些任务的几次：“上古蛮荒 8 次、十绝阵 4 次”。周常列它做的、留给这一周的任务（世界Boss、
        单独开着定时的上古蛮荒各有一行，不重复写）；上古蛮荒 / 世界Boss 就是它自己。没有要留的 = 空串。"""
        week, days = found
        if item != globalcfg.WEEKLY:
            ids = [item]
        else:
            own = {timeplan.WORLD} | ({globalcfg.ANCIENT} if globalcfg.armed(self.store, globalcfg.ANCIENT) else set())
            ids = [s.id for s in self.specs.values() if s.group == rules.WEEKLY and s.reserve == week and s.id not in own]
        return "、".join(f"{self.specs[t].title} {days * self.specs[t].reserve_gain} 次"
                         for t in ids if t in self.specs and self.specs[t].reserve == week)

    def run_global(self, item: str) -> None:
        """“立即执行”：不等时间，现在就对打了勾的存档做一遍（有开放时间的，不在时间里任务自己会说明后结束）。"""
        if not globalcfg.targets(self.store, item):
            from ..widgets import notice
            notice(self, "提示", f"{globalcfg.TITLES[item]}还没有存档打勾")
            return
        self.timer_queue += self._item_due(item)
        self._tick_timers()
