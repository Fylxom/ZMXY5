"""宠物捕捉：周常页里的独立模块（捕捉、买葫芦、筛选、刷益灵花）。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import core
from ..tasks import pet_catch
from ..widgets import LINE, SUB, Card, Field, RButton, Switch, ask_confirm, elide_label, hbox, hint, label, vbox
from .parts import Fill


FIELD_WIDTH = 84  # 本卡片数量及范围输入框统一尺寸
DETAILS_WIDTH = 160  # 名字后面“选了什么 + 选择”的宽度：三行的次数框上下对齐，又不被推到最右边


class PetCardMixin:
    def _build_pet_catch(self, page: QVBoxLayout) -> None:
        """独立操作行统一数量列；筛选范围按两列排列，窄窗口也能完整显示。"""
        head = label("宠物捕捉", 9, SUB)
        head.setContentsMargins(2, 0, 0, 3)
        page.addWidget(head)
        card = Card(padx=16, pady=10)
        col = card.box()
        col.addLayout(self._pet_task_row(self.specs[pet_catch.ID], "捕捉次数"))
        col.addLayout(self._pet_task_row(self.specs[pet_catch.star_seal.ID], "挑战次数"))
        self.pet_buy = Field("", width=FIELD_WIDTH, height=26, on_change=self.save_pet_buy)
        self.exec_btns[pet_catch.BUY_ID] = RButton("执行", lambda: self.start(only=pet_catch.BUY_ID), "side", height=26, size=9, width=50)
        material = hbox()
        material.addWidget(label("木灵葫芦", 9, SUB))
        material.addStretch(1)
        col.addLayout(self._pet_action_row(label("购买葫芦"), "购买数量", self.pet_buy, self.exec_btns[pet_catch.BUY_ID], material))
        self.pet_note = label("筛选只对天马、灵花开放", 9, SUB)
        col.addSpacing(8)
        col.addWidget(self.pet_note)
        col.addWidget(self._build_pet_filter())
        self._build_pet_harvest(col)
        page.addWidget(card)
        page.addSpacing(8)
        self._sync_pet_filter()

    def _pet_task_row(self, spec: core.TaskSpec, quantity: str) -> QHBoxLayout:
        target = hbox()
        self.summaries[spec.id] = elide_label("", 9, SUB)  # 放不下省略，鼠标停上去看全文
        target.addWidget(self.summaries[spec.id], 1)
        target.addWidget(RButton("选择", lambda s=spec: self.choose(s), "side", height=26, size=9, width=50))
        self.count_vars[spec.id] = Field(str(spec.times_default), width=FIELD_WIDTH, height=26, on_change=lambda s=spec: self.save_count(s))
        self.exec_btns[spec.id] = RButton("执行", lambda t=spec.id: self.start(only=t), "side", height=26, size=9, width=50)
        return self._pet_action_row(self._name_label(spec.id, spec.title, clickable=False), quantity,
                                    self.count_vars[spec.id], self.exec_btns[spec.id], target)

    def _build_pet_filter(self) -> QWidget:
        self.pet_box = QWidget()
        box = vbox(self.pet_box, spacing=6)
        rule = Fill(LINE)
        rule.setFixedHeight(1)
        box.addWidget(rule)
        line = hbox(spacing=0)
        line.addWidget(label("筛选", 10, bold=True))
        line.addSpacing(8)
        line.addWidget(hint("开哪一星只收哪一星，按那一星的范围筛；两个都不开 = 不筛。范围留空 = 不限，两头都含。\n"
                            "天马、灵花各存一份，按存档各记。\n"
                            "放生不合格的：只放刚捕到、1 级没改名没培养的 1 / 2 星；放不掉就停，不再捕。\n"
                            "读不到要比的字段就停下，不放生。"))
        line.addSpacing(20)
        for n in pet_catch.STARS:  # 收哪一星：开关并在这一行，开了的那一星下面显示范围
            line.addWidget(label(f"{n}星", 9))
            line.addSpacing(6)
            self.pet_stars[n] = Switch(False, self._toggle_pet_star)
            line.addWidget(self.pet_stars[n])
            line.addSpacing(16)
        line.addSpacing(8)
        line.addWidget(label("捕到合格停止", 9))
        line.addSpacing(6)
        self.pet_stop = Switch(False, self.save_pet_filter)
        line.addWidget(self.pet_stop)
        line.addSpacing(16)
        line.addWidget(label("放生不合格的", 9))
        line.addSpacing(6)
        self.pet_release = Switch(False, self._toggle_pet_release)
        line.addWidget(self.pet_release)
        line.addStretch(1)
        box.addLayout(line)
        for n in pet_catch.STARS:  # 开了的那一星才显示：资质一行、天赋一行
            self.pet_groups[n] = QWidget()
            group = vbox(self.pet_groups[n], spacing=6)
            group.addWidget(label(f"{n}星范围", 9, bold=True))
            box.addWidget(self.pet_groups[n])
            for name, cells in (("资质", pet_catch.STAR_ROWS), ("天赋", pet_catch.TALENT_ROWS)):
                group.addLayout(self._pet_range_grid(n, name, cells))
        return self.pet_box

    def _pet_range_grid(self, star: int, name: str, cells: Any) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.addWidget(label(name, 9, SUB), 0, 0, (len(cells)+1)//2, 1, Qt.AlignTop)
        for index, (k, _field, text) in enumerate(cells):
            pair = hbox(spacing=4)
            cap = label(text.replace("资质", ""), 9, SUB)
            cap.setFixedWidth(38)
            pair.addWidget(cap)
            for part in (f"s{star}.{k}", f"s{star}.{k}_max"):  # 下限 ~ 上限，灰字见 _sync_pet_filter
                self.pet_fields[part] = Field("", width=FIELD_WIDTH, height=26, on_change=self.save_pet_filter)
                pair.addWidget(self.pet_fields[part])
                if not part.endswith("_max"):
                    sep = label("~", 9, SUB)
                    sep.setFixedWidth(12)
                    sep.setAlignment(Qt.AlignCenter)
                    pair.addWidget(sep)
            grid.addLayout(pair, index//2, index%2+1)
        grid.setColumnStretch(3, 1)
        return grid

    def _pet_action_row(self, title: QLabel, quantity: str, field: Field,
                        execute: RButton, details: QHBoxLayout) -> QHBoxLayout:
        row = hbox(spacing=8)
        title.setFixedWidth(90)
        row.addWidget(title)
        holder = QWidget()  # 固定宽度：次数框紧跟着，几行之间对齐
        holder.setFixedWidth(DETAILS_WIDTH)
        details.setContentsMargins(0, 0, 0, 0)
        holder.setLayout(details)
        row.addWidget(holder)
        count = label(quantity, 9, SUB)
        count.setFixedWidth(56)
        row.addWidget(count)
        row.addWidget(field)
        row.addWidget(execute)
        row.addStretch(1)
        return row

    def _build_pet_harvest(self, col: QVBoxLayout) -> None:
        """刷益灵花直接放在宠物捕捉卡片内，不经过设置对话框。"""
        spec = self.specs["pet_cultivation"]
        col.addSpacing(10)
        rule = Fill(LINE)
        rule.setFixedHeight(1)
        col.addWidget(rule)
        details = hbox()
        details.addWidget(hint(spec.choices.note))
        details.addStretch(1)
        self.pet_harvest_count = Field("", width=FIELD_WIDTH, height=26, on_change=self.save_pet_harvest)
        self.pet_harvest_count.setPlaceholderText("留空不刷")
        self.exec_btns[spec.id] = RButton("执行", lambda: self.start(only=spec.id), "side", height=26, size=9, width=50)
        row = self._pet_action_row(self._name_label(spec.id, spec.title, clickable=False),
                                   "刷取次数", self.pet_harvest_count, self.exec_btns[spec.id], details)
        col.addLayout(row)
        flags = hbox(spacing=6, margins=(98, 0, 0, 0))  # 开关另起一行，和上面“说明”对齐（接在执行后面窗口窄时放不下）
        for key, title in spec.choices.toggles:
            flags.addWidget(label(title, 9))
            self.pet_harvest_flags[key] = Switch(False, self.save_pet_harvest)
            flags.addWidget(self.pet_harvest_flags[key])
            flags.addSpacing(12)
        flags.addStretch(1)
        col.addLayout(flags)

    def save_pet_harvest(self) -> None:
        if self.current is None or self._loading_pet:
            return
        cfg = self.store.save_cfg(self._acc(), self.slot)
        opts = cfg.setdefault("options", {}).setdefault("pet_cultivation", {})
        opts["harvest_times"] = self.pet_harvest_count.get().strip()
        for key, switch in self.pet_harvest_flags.items():
            opts[key] = switch.get()
        self.store.save()

    def _load_pet_harvest(self, sv: dict) -> None:
        opts = sv.get("options", {}).get("pet_cultivation", {})
        self._loading_pet = True
        self.pet_harvest_count.set(str(opts.get("harvest_times") or ""))
        for key, switch in self.pet_harvest_flags.items():
            switch.set(opts.get(key) is True)
        self._loading_pet = False

    def _picked_pet(self) -> int | None:
        if self.current is None:
            return None
        picked = self.store.save_view(self._acc(), self.slot).get("options", {}).get(pet_catch.ID, {}).get("levels") or []
        return picked[0] if picked else None

    def _sync_pet_filter(self) -> None:
        """选的是天马/灵花才显示筛选，其他宠物（和没选）显示一行说明。"""
        picked = self._picked_pet()
        on = picked in pet_catch.FILTERABLE
        self.pet_box.setVisible(on)
        self.pet_note.setVisible(not on)
        try:
            spans = pet_catch.ranges(picked) if on else {}
        except Exception:  # 数据表对不上：只是没有灰字提示
            spans = {}
        for star in pet_catch.STARS:  # 天马和灵花的范围不一样，换了宠物灰字跟着换
            self.pet_groups[star].setVisible(self.pet_stars[star].get())  # 没开的那一星不显示范围框
            for k, _field, _name in pet_catch.ALL_ROWS:
                lo, hi = spans.get(star, {}).get(k, ("", ""))
                self.pet_fields[f"s{star}.{k}"].setPlaceholderText(str(lo))
                self.pet_fields[f"s{star}.{k}_max"].setPlaceholderText(str(hi))

    def _pet_filter_value(self) -> dict[str, Any]:
        flt: dict[str, Any] = {"stars": [n for n, sw in self.pet_stars.items() if sw.get()], "release": self.pet_release.get(), "stop": self.pet_stop.get()}
        for key, field in self.pet_fields.items():
            star, _, name = key.rpartition(".")
            flt.setdefault(star, {})[name] = field.get().strip()
        return flt

    def _toggle_pet_star(self) -> None:
        for n, sw in self.pet_stars.items():
            self.pet_groups[n].setVisible(sw.get())
        self.save_pet_filter()

    def _toggle_pet_release(self) -> None:
        """打开自动放生前先确认一次（放生不能撤销）；点了“不开”就拨回去。"""
        if self.pet_release.get() and not ask_confirm(self, "自动放生", "开了以后，筛选不合格的天马/灵花会在捕到后立刻放生，放生不能撤销。\n确定要打开吗？", "打开", "不开"):
            self.pet_release.set(False)
        self.save_pet_filter()

    def save_pet_filter(self) -> None:
        if self.current is None or self._loading_pet:
            return
        picked = self._picked_pet()
        if picked not in pet_catch.FILTERABLE:
            return
        cfg = self.store.save_cfg(self._acc(), self.slot)
        opts = cfg.setdefault("options", {}).setdefault(pet_catch.ID, {})
        opts.setdefault("filters", {})[str(picked)] = self._pet_filter_value()  # 筛选按存档、按宠物各记各的（天马一份、灵花一份）
        self.store.save()

    def save_pet_buy(self) -> None:
        if self.current is None or self._loading_pet:
            return
        cfg = self.store.save_cfg(self._acc(), self.slot)
        cfg.setdefault("options", {}).setdefault(pet_catch.BUY_ID, {})["buy"] = self.pet_buy.get().strip()
        self.store.save()

    def _load_pet_filter(self, sv: dict) -> None:
        opts = sv.get("options", {}).get(pet_catch.ID, {})
        picked = (opts.get("levels") or [None])[0]
        filters = opts.get("filters")
        flt = filters.get(str(picked)) if isinstance(filters, dict) else None  # 选的是哪种宠物就显示哪一份
        flt = flt if isinstance(flt, dict) else {}
        self._loading_pet = True
        self.pet_buy.set(str(sv.get("options", {}).get(pet_catch.BUY_ID, {}).get("buy") or ""))
        wanted = pet_catch.stars_wanted(flt)
        self.pet_release.set(flt.get("release") is True)  # 没存过 = 关
        self.pet_stop.set(flt.get("stop") is True)
        for n, sw in self.pet_stars.items():
            sw.set(n in wanted)
        for key, field in self.pet_fields.items():
            star, _, name = key.rpartition(".")
            value = flt.get(star)
            field.set(str(value.get(name, "")) if isinstance(value, dict) else "")
        self._loading_pet = False
        self._sync_pet_filter()
