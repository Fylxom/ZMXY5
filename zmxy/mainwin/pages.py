"""主区域的骨架和任务行：工具栏、页签、按分组排的任务表、日常页底部的同物归库和不拾取。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from .. import core, pickup
from ..tasks import boss_raid, last_rewards, pet_catch, refine, time_boat, warehouse_sync
from ..widgets import (ACCENT, SUB, TEXT, WARN, Card, Chip, Field, FlowLayout, RButton, Segmented, Switch, elide_label, font, hbox, hint, label, scroll_page, set_fg, tip, vbox)
from .parts import TaskRow
from .rules import DISPLAY_ORDER, DISPLAY_RANK, GROUP_ORDER, HIDDEN, LIMITED, OPEN_HOURS, TONGTIAN, WEEKLY, WEEKLY_BOTTOM


SHORT_TITLES = {"teacher_trial": "师徒·师傅", "teacher_trial_student": "师徒·徒弟", "pet_arena_apply": "斗宠报名",
                "arena_apply": "竞技场报名", "uid_lookup": "UID查询"}  # 周常页上写短一点的任务名（日志里照旧用全名）
GROUP_NOTES = {  # 组名旁边“说明”里的文字
    "其他": "账号级任务：每个账号做一次，用行上“选存档”选的存档，不看上面选的范围。",
    "已结束": "档期已过，或这个存档已经做完（云梯、玄女战记、登录有礼）。识别时开关已关掉，要做可以手动再开。",
}
WEEKLY_LAYOUT = (  # 周常那一块的摆放顺序（两个一行，同类挨着）：师徒 / 留次数的塔 / 副本 / 商店；摘要长的独占一行放最后
    "teacher_trial", "teacher_trial_student", "demon_tower", "ten_jue", "ancient_wilderness", "tree_king_nightmare",
    "limited_time_shop", "fairy_flag_shop", "star_maze", "last_rewards", "world_boss")
WEEKLY_WIDE = ("world_boss",)  # 摘要长（存档·档位·祖巫），半行放不下：独占一行；上届奖励的摘要缩短后和星斗幻境并排（autoconfig.short_reward）
WEEKLY_MODULES = (time_boat.ID, pet_catch.ID, pet_catch.BUY_ID, pet_catch.star_seal.ID, "pet_cultivation")  # 周常页里有自己的模块，不进通用任务表


def two_columns(card: Card) -> QGridLayout:
    """卡片里两列等宽的网格。"""
    grid = card.grid()
    grid.setHorizontalSpacing(28)
    for c in range(2):
        grid.setColumnStretch(c, 1)
    return grid


class WeeklyCard(Card):
    """周常页的一块任务：卡片够宽（TWO_UP 以上）时一行放两个任务，窄了一行一个；改窗口大小时重新摆。
    列：0 名字 | 1 选了什么 | 2 按钮 | 3 次数 | 4 执行 | 5 开关；右半在 7–12，6 是两半之间的空隙。不显示列名。
    独占一行的任务（long）：选了什么占到右半，按钮以后的格子和右半对齐。"""

    TWO_UP = 690  # 卡片宽度（像素）：够这么宽才放两列
    GAP = 24
    COLS = ("name", "middle", "buttons", "count", "exec", "switch")
    ALIGN = {"name": Qt.AlignLeft, "buttons": Qt.AlignRight, "count": Qt.AlignCenter, "exec": Qt.AlignCenter, "switch": Qt.AlignCenter}

    def __init__(self, items: list[dict[str, Any]]) -> None:
        super().__init__(padx=12, pady=2)
        self.items, self.two, self.rows = items, None, 0
        self.lay = self.grid()
        self.lay.setHorizontalSpacing(6)
        self._place(True)

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        two = self.width() >= self.TWO_UP
        if two != self.two:
            self._place(two)

    def _put(self, w: QWidget, r: int, c: int, span: int = 1) -> None:
        align = self.ALIGN.get(next((k for k, v in self._cells.items() if v is w), ""))
        if align is None:
            self.lay.addWidget(w, r, c, 1, span)
        else:
            self.lay.addWidget(w, r, c, 1, span, align)

    def _place(self, two: bool) -> None:
        g = self.lay
        while g.count():
            g.takeAt(0)  # 只拿出来，控件还在卡片上，下面重新放
        for row in range(self.rows):
            g.setRowMinimumHeight(row, 0)
        g.setColumnMinimumWidth(6, self.GAP if two else 0)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(8, 1 if two else 0)
        r = 0
        i = 0  # 两列时：这一行已经放了几个短任务
        for cells in self.items:
            self._cells = cells
            long = two and cells["long"]
            if not two or long:
                if i:
                    r, i = r + 1, 0
                base = 0
            else:
                base = 7 * i
            right = 7 if long else base  # 按钮以后的列：独占一行的和右半对齐
            self._put(cells["name"], r, base)
            self._put(cells["middle"], r, base + 1, 7 if long else 1)  # 独占一行：占 1–7 列，右半的按钮格（8–9）留给按钮
            if "buttons" in cells:
                self._put(cells["buttons"], r, right + 1 if long else base + 2, 2 if long else 1)
            for k, col in (("count", 3), ("exec", 4), ("switch", 5)):
                if k in cells:
                    self._put(cells[k], r, right + col)
            if two and not long and i == 0:
                i = 1
            else:
                r, i = r + 1, 0
        self.rows = r + (1 if i else 0)
        for row in range(self.rows):
            g.setRowMinimumHeight(row, 28)
        self.two = two


class PagesMixin:
    def _build_main(self) -> QWidget:
        main = QWidget()
        col = vbox(main, margins=(22, 14, 22, 18))
        col.addLayout(self._build_toolbar())
        col.addSpacing(10)
        col.addLayout(self._build_pages())
        col.addSpacing(8)
        col.addWidget(self.stack, 1)
        tasks = self.page_lays["tasks"]
        # 任务（按分组并排）：活动、周常各单开一页，其余在日常页
        self._build_task_page(tasks, [sp for sp in self.specs.values() if sp.group not in (WEEKLY, LIMITED)])
        self._build_tidy_bar(tasks)
        self._build_pickup_bar(tasks)
        self.limited_specs = [sp for sp in self.specs.values() if sp.group == LIMITED]
        self._build_limited()
        self._build_weekly_page(self.page_lays["weekly"])
        for key in ("tasks", "weekly"):
            self.page_lays[key].addStretch(1)
        self.refresh_reserve()
        self._build_log_page()
        self._show_page()
        return main

    def _build_weekly_page(self, page: QVBoxLayout) -> None:
        """周常页：一行两个任务，各列上下对齐（任务名 | 选了什么 | 选择按钮 | 次数 | 执行 | 自动执行开关）。
        时光之舟、宠物捕捉是自己的模块；UID 查询放最底下。"""
        specs = [sp for sp in self.specs.values() if sp.group == WEEKLY and sp.id not in WEEKLY_MODULES + WEEKLY_BOTTOM + HIDDEN + OPEN_HOURS]
        specs.sort(key=lambda sp: (WEEKLY_LAYOUT.index(sp.id) if sp.id in WEEKLY_LAYOUT else len(WEEKLY_LAYOUT),
                                   DISPLAY_RANK.get(sp.id, len(DISPLAY_ORDER))))
        self.weekly_ids = [sp.id for sp in specs if sp.times_max]
        self.auto_ids = [sp.id for sp in specs if sp.auto]  # 自动配置要读的：有次数的 + 上届奖励的领取状态
        self.weekly_run_ids = [sp.id for sp in specs]
        self._build_auto_bar(page)
        self._weekly_card(page, "周常", specs, switches=True, note=(
            "顶部“执行”不做周常；周常用“自动配置并执行”、行上“执行”或定时。\n"
            "最右边的开关（各存档各记）：关着的定时和“自动配置并执行”都不做；行上“执行”不看开关。\n"
            "次数框留空或 0 = 不做。\n"
            "名字红：养成周快到了，今天要留次数（见上面“按养成周留次数”）。\n"
            "十绝阵按 3000 分结算，上古蛮荒按多人结算。\n"
            "师徒试炼：师傅带过一个徒弟即每周 18 次。"))
        self._open_hours_card(page)
        self._build_boat(page)
        self._build_pet_catch(page)
        self._weekly_card(page, "查询", [self.specs[t] for t in WEEKLY_BOTTOM])  # 和定时无关，放最底下

    def _weekly_card(self, page: QVBoxLayout, title: str, specs: list[core.TaskSpec], switches: bool = False, note: str = "") -> None:
        """一块：够宽时一行放两个任务，各列上下对齐（任务名 | 选了什么 | 按钮 | 次数 | 执行 | 开关）；
        按钮多的任务独占一行（排在后面）。窗口窄了自动改成一行一个（WeeklyCard）。"""
        head = hbox(margins=(2, 0, 2, 3))
        head.addWidget(label(title, 9, SUB))
        if note:
            head.addSpacing(8)
            head.addWidget(hint(note))
        head.addStretch(1)
        page.addLayout(head)
        ordered = [sp for sp in specs if not self._weekly_long(sp)] + [sp for sp in specs if self._weekly_long(sp)]
        page.addWidget(WeeklyCard([self._weekly_item(sp, switches) for sp in ordered]))
        page.addSpacing(8)

    def _open_hours_card(self, page: QVBoxLayout) -> None:
        """有开放时间的：同类的放一组（斗神和乱斗、通天河三项、两个报名），不写开放时间（用户 2026-11-10：大家都知道）；
        每个任务的按钮紧跟在它的文字后面，一行放不下就折行，不为了对齐留空白。"""
        head = label("有开放时间", 9, SUB)
        head.setContentsMargins(2, 0, 2, 3)
        page.addWidget(head)
        card = Card(padx=12, pady=6)
        col = card.box()
        col.setSpacing(6)
        for title, ids in (("对战", ("pvp_match", "pet_brawl")), ("通天河", TONGTIAN), ("报名", ("arena_apply", "pet_arena_apply"))):
            row = hbox(spacing=0)
            name = label(title, 9, SUB)
            name.setFixedWidth(52)
            row.addWidget(name, 0, Qt.AlignTop)
            items = QWidget()
            flow = FlowLayout(items, hspace=28, vspace=4)
            for tid in ids:
                flow.addWidget(self._compact_item(self._weekly_item(self.specs[tid], False)))
            row.addWidget(items, 1)
            col.addLayout(row)
        page.addWidget(card)
        page.addSpacing(8)

    @staticmethod
    def _compact_item(cells: dict[str, Any]) -> QWidget:
        """一个任务紧凑地排成一条：名字、选了什么（太长省略，鼠标停上去看全文）、按钮、次数、执行，挨着放。"""
        item = QWidget()
        row = hbox(item, spacing=6)
        middle = cells["middle"]
        middle.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        middle.setMaximumWidth(260)
        for key in ("name", "middle", "buttons", "count", "exec", "switch"):
            if key in cells:
                row.addWidget(cells[key])
        return item

    @staticmethod
    def _weekly_long(spec: core.TaskSpec) -> bool:
        """按钮多（选择 + 选存档、刷新 + 选存档）或行上有档位的任务，半行放不下，独占一行。"""
        buttons = bool(spec.choices) + (spec.account_wide and spec.id != boss_raid.WORLD_ID)
        return spec.id in WEEKLY_WIDE or buttons > 1 or bool(spec.modes and not (spec.choices and spec.choices.mode_inside))

    def _weekly_item(self, spec: core.TaskSpec, switch: bool) -> dict[str, Any]:
        """一个任务的各格控件（{列名: 控件}），由 WeeklyCard 摆位置。"""
        cells: dict[str, Any] = {"long": self._weekly_long(spec)}
        roww = TaskRow(lambda t=spec.id: self.pick_task(t))  # 点名字 = 选中这个任务
        hbox(roww).addWidget(self._name_label(spec.id, SHORT_TITLES.get(spec.id, spec.title)))
        cells["name"] = roww
        if spec.reserve and spec.id not in self.reserve_ids:  # 留次数的任务：养成周开始前几天名字变红
            self.reserve_ids.append(spec.id)
        middle = QWidget()  # 选了什么（摘要），没有选择的显示开放时间 / 领取状态
        middle.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # 窄了省略摘要，不挤按钮
        mid = hbox(middle, spacing=6)
        stretched = False
        buttons = QWidget()
        btns = hbox(buttons, spacing=4)
        if spec.modes and not (spec.choices and spec.choices.mode_inside):
            self.mode_vars[spec.id] = Segmented([(text, value) for value, text in spec.modes], spec.default_mode,
                                                lambda s=spec: self.save_mode(s), seg_width=36, height=22, size=9)
            if spec.mode_note:
                tip(self.mode_vars[spec.id], spec.mode_note)
            btns.addWidget(self.mode_vars[spec.id])
        if spec.choices:
            self.summaries[spec.id] = elide_label("", 9, SUB)
            mid.addWidget(self.summaries[spec.id], 1)
            stretched = True
            btns.addWidget(RButton("刷新" if spec.id == "tongtian_escort" else spec.choices.button, lambda s=spec: self.choose(s),
                                   "side", height=22, size=9))
        if spec.account_wide and spec.id != boss_raid.WORLD_ID:  # 世界Boss的存档并入选择摘要，入口在选择框内
            self.wide_labels[spec.id] = label("", 9, SUB)
            mid.addWidget(self.wide_labels[spec.id])
            btns.addWidget(RButton("选存档", lambda s=spec: self.choose_save(s), "side", height=22, size=9))
        if spec.id == last_rewards.ID:  # 自动配置读到的领取状态
            self.reward_lbl = elide_label("都未读取", 9, SUB)
            mid.addWidget(self.reward_lbl, 1)
            stretched = True
        if not stretched:
            mid.addStretch(1)
        cells["middle"] = middle
        if btns.count():
            cells["buttons"] = buttons
        if spec.times_max:
            count = QWidget()
            box = hbox(count, spacing=2)
            box.addWidget(label("×", 9, SUB))
            self.count_vars[spec.id] = Field(str(spec.times_default), width=38, height=22, on_change=lambda s=spec: self.save_count(s))
            box.addWidget(self.count_vars[spec.id])
            cells["count"] = count
        self.exec_btns[spec.id] = cells["exec"] = RButton("执行", lambda t=spec.id: self.start(only=t), "side", height=22, size=9, width=44)
        if switch and spec.id in self.weekly_run_ids and not spec.account_wide:  # 世界Boss有自己的定时，没有开关
            self.weekly_vars[spec.id] = cells["switch"] = Switch(False, self.save_weekly)
        return cells

    def _build_toolbar(self) -> Any:
        """最上面一行：账号标题、范围（当前存档/当前账号/全部账号）、执行、停止。"""
        top = hbox()
        self.title_lbl = label("", 13, bold=True)
        top.addWidget(self.title_lbl)
        top.addSpacing(8)
        self.subtitle = label("", 9, SUB)
        self.subtitle.setContentsMargins(0, 3, 0, 0)
        top.addWidget(self.subtitle)
        top.addStretch(1)
        self.scope = Segmented([("当前存档", "slot"), ("当前账号", "current"), ("全部账号", "all")], "current")
        top.addWidget(self.scope)
        top.addSpacing(16)
        self.btn_run = RButton("执行", self.start, "primary", width=68)
        top.addWidget(self.btn_run)
        top.addSpacing(8)
        self.btn_stop = RButton("停止", self._stop_or_resume, "stop", width=60)
        self.btn_stop.set_enabled(False)
        tip(self.btn_stop, "只停当前账号；正打着一场时问打完再停还是立即停。\n"
                           "有断点时变成“继续”：各账号从断点接着做。重新执行任何任务，断点作废。")
        top.addWidget(self.btn_stop)
        return top

    def _build_pages(self) -> Any:
        """页面切换条（日常 / 活动 / 周常 / 定时 / 日志）和五个叠在一起的页面。"""
        bar = hbox()
        self.page = Segmented([("日常", "tasks"), ("活动", "limited"), ("周常", "weekly"), ("日志", "log")], "tasks", self._show_page, seg_width=88)
        bar.addWidget(self.page)
        bar.addSpacing(12)
        self.btn_one = RButton("执行此任务", self._run_picked, "plain", width=96)
        self.btn_one.set_enabled(False)
        bar.addWidget(self.btn_one)
        bar.addSpacing(10)
        self.pick_hint = label("点击下方任务名选中", 9, SUB)
        self.pick_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # 窗口窄时让它被裁，不去挤左边的页签和按钮
        bar.addWidget(self.pick_hint, 1)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.page_lays: dict[str, QVBoxLayout] = {}
        for key in ("tasks", "limited", "weekly", "global"):  # global：账号栏最上面的“定时”，不在页签里
            self.pages[key], self.page_lays[key] = scroll_page()
            self.stack.addWidget(self.pages[key])
        self.pages["log"] = QWidget()
        self.stack.addWidget(self.pages["log"])
        return bar

    def _build_pickup_bar(self, page: QVBoxLayout) -> None:
        """不拾取（按存档）：选中的掉落种类留在地上。"""
        skip = hbox(spacing=6, margins=(2, 0, 0, 6))
        title = label("不拾取", 9, SUB)
        tip(title, "按存档各自设置。副本里这几类地面掉落不捡，认不出的照常捡。")
        skip.addWidget(title)
        skip.addSpacing(4)
        for key, text in pickup.OPTIONS:
            chip = self.pickup_chips[key] = Chip(text, False, lambda k=key: self.toggle_pickup(k), width=80, height=24)
            skip.addWidget(chip)
        skip.addStretch(1)
        page.addLayout(skip)

    def _task_grid(self, page: QVBoxLayout, title: str, note: str = "", padx: int = 12, pady: int = 2, extra: QWidget | None = None) -> QGridLayout:
        """一块任务：灰色小标题 + 圆角卡片；返回卡片里两列等宽的网格。extra：放在标题行最右边的控件。"""
        head = hbox(margins=(2, 0, 2, 3))
        head.addWidget(label(title, 9, SUB))
        if note:  # 长说明收进“说明”里，鼠标停上去才显示
            head.addSpacing(8)
            head.addWidget(hint(note))
        head.addStretch(1)
        if extra is not None:
            head.addWidget(extra)
        page.addLayout(head)
        card = Card(padx=padx, pady=pady)
        grid = two_columns(card)
        page.addWidget(card)
        page.addSpacing(8)
        return grid

    def _name_label(self, tid: str, title: str, clickable: bool = True) -> QLabel:
        name = self.task_labels[tid] = label(title)
        if clickable:
            name.setCursor(Qt.PointingHandCursor)
        return name

    def _build_task_page(self, page: QVBoxLayout, specs_in: list[core.TaskSpec], note: str = "", group_of: Callable | None = None) -> None:
        """note：显示在组名（如“周常”）右边的说明。group_of：自定义分块（活动页用）。"""
        groups: dict[str, list[core.TaskSpec]] = {}
        for spec in specs_in:  # 要选存档的账号级任务单开“其他”
            groups.setdefault(group_of(spec) if group_of else "其他" if spec.account_wide else spec.group, []).append(spec)
        for g in groups:  # 展示按 DISPLAY_ORDER 排（两列按行读）；执行顺序另见 core.run_order
            groups[g].sort(key=lambda sp: DISPLAY_RANK.get(sp.id, len(DISPLAY_ORDER)))
        for g in sorted(groups, key=lambda g: GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)):
            grid = self._task_grid(page, g, note or GROUP_NOTES.get(g, ""))  # 每组占满一行，组内任务排两列
            for j, spec in enumerate(groups[g]):
                grid.addWidget(self._task_row(spec), j // 2, j % 2)

    def _task_row(self, spec: core.TaskSpec) -> TaskRow:
        roww = TaskRow(lambda t=spec.id: self.pick_task(t))  # 点任务名/空白处选中，开关和按钮不受影响
        row = hbox(roww, margins=(0, 3, 0, 3))
        row.addWidget(self._name_label(spec.id, spec.title))
        row.addStretch(1)
        if spec.reserve and spec.id not in self.reserve_ids:  # 留次数的任务：养成周开始前几天名字变红
            self.reserve_ids.append(spec.id)
        if spec.modes and not (spec.choices and spec.choices.mode_inside):  # 选择框内配置的档位不在任务行重复显示
            seg = self.mode_vars[spec.id] = Segmented([(text, value) for value, text in spec.modes], spec.default_mode,
                                                      lambda s=spec: self.save_mode(s), seg_width=40, height=22, size=9)
            if spec.mode_note:
                tip(seg, spec.mode_note)
            row.addWidget(seg)
            row.addSpacing(8)
        if spec.choices:
            if spec.id == boss_raid.WORLD_ID:
                row.addSpacing(8)
            self.summaries[spec.id] = self._row_picker(row, "刷新" if spec.id == "tongtian_escort" else spec.choices.button, lambda s=spec: self.choose(s))
        if spec.id == refine.ID:  # 自动洗练：点“选择”进去分法宝 / 羽毛 / 阵法三块
            self.refine_lbl = self._row_picker(row, "选择", self.open_refine)
        if spec.account_wide and spec.id != boss_raid.WORLD_ID:  # 世界Boss的存档并入选择摘要，入口在选择框内
            self.wide_labels[spec.id] = self._row_picker(row, "选存档", lambda s=spec: self.choose_save(s))
        self.task_vars[spec.id] = Switch(False, self.save_tasks)
        row.addWidget(self.task_vars[spec.id])
        return roww

    def _row_picker(self, row: Any, button: str, command: Callable[[], None]) -> QLabel:
        """行上的灰字摘要 + 打开选择的按钮。返回摘要（窗口窄时摘要末尾省略，按钮不被挤出去）。"""
        summary = elide_label("", 9, SUB)
        row.addWidget(summary)
        row.addSpacing(6)
        row.addWidget(RButton(button, command, "side", height=22, size=9))
        row.addSpacing(8)
        return summary

    def _build_tidy_bar(self, page: QVBoxLayout) -> None:
        card = Card(padx=12, pady=6)
        grid = two_columns(card)  # 和任务列表同样的两列：同物归库对齐右列（会员礼包那一列）
        ui = self.store.data.get("ui") or {}
        left = hbox(margins=(0, 3, 0, 3))
        left.addWidget(label("跳转日志"))
        left.addStretch(1)
        self.jump_var = Switch(bool(ui.get("jump", True)), self._save_ui)  # 全局设置，默认开（原来就是执行后跳到日志）
        left.addWidget(self.jump_var)
        grid.addLayout(left, 0, 0)
        roww = TaskRow(lambda: self.pick_task(warehouse_sync.ID))  # 和任务行一样：点名字选中，再点“执行此任务”单独执行一次
        right = hbox(roww, margins=(0, 1, 0, 1))  # 上下只留 1：间隔输入框（高 24）显示出来时这一行不变高
        name = self._name_label(warehouse_sync.ID, warehouse_sync.ID)
        roww.setMinimumHeight(name.sizeHint().height() + 6)  # 和任务行一样高（名字 + 上下各 3）
        tip(name, "背包里和仓库同种的道具并进仓库（按存档各自设置）。\n"
                  "执行中每做完 N 个任务归一次，0 = 全部做完后一次；单独执行一个任务时不归。")
        right.addWidget(name)
        right.addStretch(1)
        self.tidy_box = QWidget()  # 开了才显示，在名字和开关之间
        box = hbox(self.tidy_box, spacing=4, margins=(0, 0, 12, 0))
        box.addWidget(label("每", 9, SUB))
        self.tidy_every = Field("0", width=48, height=24, on_change=self.save_tidy)
        box.addWidget(self.tidy_every)
        box.addWidget(label("个任务一次，0=全部做完后", 9, SUB))
        right.addWidget(self.tidy_box)
        self.tidy_on = Switch(False, self._tidy_toggled)
        right.addWidget(self.tidy_on)
        grid.addWidget(roww, 0, 1)
        page.addWidget(card)
        page.addSpacing(8)
        self._tidy_toggled(save=False)

    def _tidy_toggled(self, save: bool = True) -> None:
        self.tidy_box.setVisible(self.tidy_on.get())
        if save:
            self.save_tidy()

    def save_tidy(self) -> None:
        """开关 + 间隔存进当前存档（每个存档各自的）；间隔输入非数字时先不动。"""
        if self.current is None or self._loading_tidy:
            return
        text = self.tidy_every.get().strip()
        if text and not (text.isascii() and text.isdigit()):
            return
        self.store.save_cfg(self._acc(), self.slot)["tidy"] = {"on": self.tidy_on.get(), "every": int(text) if text else 0}
        self.store.save()

    def _load_tidy(self, sv: dict) -> None:
        cfg = sv.get("tidy") if isinstance(sv.get("tidy"), dict) else {}
        self._loading_tidy = True
        self.tidy_on.set(bool(cfg.get("on")))
        self.tidy_every.set(str(cfg.get("every", 0)))
        self._loading_tidy = False
        self._tidy_toggled(save=False)

    def _show_page(self) -> None:
        self.stack.setCurrentWidget(self.pages["global" if self.global_view else self.page.get()])

    def pick_task(self, tid: str) -> None:
        self.picked_task = None if self.picked_task == tid else tid  # 再点一次取消
        for t, lbl in self.task_labels.items():
            on = t == self.picked_task
            set_fg(lbl, self._name_color(t, on))
            lbl.setFont(font(10, on))
        self.pick_hint.setText(f"已选：{self._title(self.picked_task)}" if self.picked_task else "点击下方任务名选中")
        self.btn_one.set_enabled(self.picked_task is not None and self._can_task())

    def _name_color(self, tid: str, picked: bool) -> str:
        if tid == refine.ID and tid in self.idle_red:
            return WARN
        if picked:
            return ACCENT
        if tid in self.red_tasks | self.idle_red:
            return WARN
        return SUB if tid in self._limited_sig else TEXT

    def _title(self, tid: str) -> str:
        from .. import globalcfg
        if kind := globalcfg.kind_of(tid):
            return "定时·" + globalcfg.TITLES[kind]
        return self.specs[tid].title if tid in self.specs else tid  # 同物归库不在任务表里

    def _run_picked(self) -> None:
        if self.picked_task:
            self.start(only=self.picked_task)
