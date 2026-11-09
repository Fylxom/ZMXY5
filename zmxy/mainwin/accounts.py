"""左侧账号栏：账号增删改、存档选择、切换账号时把设置读回界面，以及记住上次的选择。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QWidget

from ..dialogs import AccountDialog
from ..widgets import (SIDEBAR, SUB, AccountRow, RButton, Segmented,
                       StatusPill, ask_confirm, clear_layout, hbox, label, notice, vbox)
from .parts import Fill


class AccountsMixin:
    def _build_sidebar(self) -> QWidget:
        side = Fill(SIDEBAR)
        side.setFixedWidth(210)
        col = vbox(side, margins=(0, 18, 0, 12))
        head = hbox(margins=(20, 0, 12, 6))
        head.addWidget(label("账号", 9, SUB))
        head.addStretch(1)
        head.addWidget(RButton("设置", self.open_settings, "ghost", height=22, size=9))  # 程序更新、数据文件夹（mainwin/settings.py）
        col.addLayout(head)
        area = QScrollArea()  # 账号多了可以滚
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setFrameShape(area.Shape.NoFrame)
        inner = Fill(SIDEBAR)  # 滚动区自己会刷底色，这里直接画成侧栏的颜色
        area.setWidget(inner)
        self.acc_list = vbox(inner, spacing=2, margins=(10, 0, 10, 0))
        col.addWidget(area, 1)

        foot = vbox(margins=(12, 6, 12, 0))
        tag = self.slot_tag = label("存档选择", 9, SUB)
        tag.setContentsMargins(14, 0, 0, 5)
        foot.addWidget(tag)
        self.slot_segs = []
        for lo in (1, 5):  # 一行放不下，分两行：1-4 / 5-8；两条合起来是一个选择
            seg = Segmented([(str(i), str(i)) for i in range(lo, lo + 4)], "1", seg_width=40, size=10)
            seg.command = lambda s=seg: self._slot_clicked(s)
            self.slot_segs.append(seg)
            foot.addWidget(seg, 0, Qt.AlignHCenter)
            foot.addSpacing(4)
        foot.addSpacing(6)
        for texts, width in (((("添加", self.add), ("编辑", self.edit), ("删除", self.delete)), 58),
                             ((("重置存档配置", self.reset_save), ("重置账号配置", self.reset_account)), 88)):  # 重置：左边只重置当前存档，右边重置整个账号
            bar = hbox()
            for i, (text, cmd) in enumerate(texts):
                if i:
                    bar.addStretch(1)
                bar.addWidget(RButton(text, cmd, "side", width=width, height=28, size=9))
            foot.addLayout(bar)
            foot.addSpacing(10)
        self.pill = StatusPill()
        foot.addWidget(self.pill)
        self.update_note = label("", 8, SUB)  # 正在下载新版本时的进度（settings.pill_note）
        self.update_note.setContentsMargins(14, 4, 0, 0)
        self.update_note.hide()
        foot.addWidget(self.update_note)
        col.addLayout(foot)
        return side

    def _restore_ui(self) -> None:
        ui = self.store.data.get("ui")
        if not isinstance(ui, dict):
            return
        slots = ui.get("slots")
        if isinstance(slots, dict):
            self.acc_slots = {k: v for k, v in slots.items() if isinstance(v, int) and 1 <= v <= 8}
        if ui.get("scope") in ("slot", "current", "all"):
            self.scope.set(ui["scope"])
        keys = [self._acc_key(a) for a in self.store.accounts]
        if ui.get("account") in keys:
            self.current = keys.index(ui["account"])

    def _save_ui(self) -> None:
        acc = self._acc()
        self.store.data["ui"] = {"account": self._acc_key(acc) if acc else "", "slots": self.acc_slots,
                                 "scope": self.scope.get(), "reserve": self.reserve_var.get(),
                                 "remind": self.remind_var.get(), "jump": self.jump_var.get()}
        self.store.save()

    def refresh_accounts(self, select: int | None = None) -> None:
        if self.store.accounts:
            self.current = min(select if select is not None else (self.current or 0), len(self.store.accounts) - 1)
        else:
            self.current = None
        self.on_select()

    def _render_accounts(self) -> None:
        clear_layout(self.acc_list)
        self.acc_list.addWidget(AccountRow("定时", "", self.global_view, self.show_global, self.show_global))
        self.acc_list.addSpacing(6)
        if not self.store.accounts:
            self.acc_list.addSpacing(30)
            self.acc_list.addWidget(label("还没有账号", 9, "#B0B0B5"), 0, Qt.AlignHCenter)
        for i, a in enumerate(self.store.accounts):
            self.acc_list.addWidget(AccountRow(self.store.label(a), f"存档 {self.acc_slots.get(self._acc_key(a), 1)}", i == self.current and not self.global_view,
                                               lambda i=i: self.select(i), self.edit))
        self.acc_list.addStretch(1)

    def _acc(self) -> dict[str, Any] | None:
        """当前选中的账号条目；没选 = None。"""
        return self.store.accounts[self.current] if self.current is not None else None

    def _require_account(self) -> bool:
        if self.current is None:
            notice(self, "提示", "请先添加并选择账号")
            return False
        return True

    def _other_saves(self) -> list[tuple[str, str]]:
        """徒弟选择：其他登录账号的 1-8 号存档（排除当前账号的所有存档）。"""
        mine = self._acc()
        return [(f"{a.get('username', '')}|{n}", f"{a.get('name') or a.get('username', '?')} · 存档 {n}")
                for a in self.store.accounts if mine is None or a.get("username") != mine.get("username")
                for n in range(1, 9)]

    def select(self, idx: int) -> None:
        if idx != self.current or self.global_view:
            self.current, self.global_view = idx, False
            self.on_select()

    @staticmethod
    def _acc_key(acc: dict[str, Any]) -> str:
        return acc.get("username") or acc.get("name", "")

    def reset_save(self) -> None:
        """把当前查看的存档的任务勾选、选项、不拾取恢复默认，读取到的角色信息也清掉（不影响账号级任务和其他存档）。"""
        if self.current is None or self.global_view or self._busy():
            return
        acc = self._acc()
        if not ask_confirm(self, "重置存档配置", f"将「{self.store.label(acc)}」存档 {self.slot} 的任务勾选、选项、不拾取设置和断点重置为默认，\n读取到的信息（羽毛、时光之舟、幻兽等）也一起清掉，要用时重新读取。\n\n"
                                           "其他存档和账号级任务的设置不受影响。继续？", "重置", "取消"):
            return
        self.store.reset_save(acc, self.slot)
        self.store.save()
        self.on_select()

    def reset_account(self) -> None:
        """把当前账号的全部存档和账号级任务的设置恢复默认（登录信息、备注名不动）。"""
        if self.current is None or self.global_view or self._busy():
            return
        acc = self._acc()
        if not ask_confirm(self, "重置账号配置", f"将「{self.store.label(acc)}」的全部存档（1–8）和账号级任务的设置都重置为默认，\n各存档读取到的信息（羽毛、时光之舟、幻兽等）也一起清掉。\n\n"
                                           "登录信息和备注名不受影响。此操作不能撤销，继续？", "重置", "取消"):
            return
        self.store.reset_account(acc)
        self.store.save()
        self.on_select()

    def _slot_clicked(self, seg: Segmented) -> None:
        self._set_slot(int(seg.get()))
        self.on_slot()

    def _set_slot(self, n: int) -> None:
        self._slot = n
        for seg in self.slot_segs:
            seg.set(str(n))  # 不在这一条里的值 = 这一条没有选中的

    def on_slot(self) -> None:
        """点了存档选择：只记在当前账号上，其他账号的存档不变。"""
        if self.current is not None:
            self.acc_slots[self._acc_key(self._acc())] = self.slot
        self.on_select()

    def on_select(self) -> None:
        acc = self._acc()
        self._set_slot(self.acc_slots.get(self._acc_key(acc), 1) if acc else 1)  # 每个账号各记各的存档
        if self._ended_ids() != self._limited_sig:  # 换账号/存档后云梯是否已结束不同：活动页重分块
            self._build_limited()
        self._render_accounts()
        sv = self.store.save_view(acc, self.slot) if acc else {}
        chosen = set(sv.get("tasks", ()))
        for tid, var in self.task_vars.items():
            var.set(self._wide_cfg(acc, tid)["on"] if tid in self.wide and acc else tid in chosen)
        weekly = set(sv.get("weekly", ()))
        for tid, var in self.weekly_vars.items():
            var.set(tid in weekly)
        for key, chip in self.pickup_chips.items():
            chip.set(key in sv.get("no_pickup", ()))
        self._load_tidy(sv)
        self._load_pet_filter(sv)
        self._load_pet_harvest(sv)
        self._load_counts(acc, sv)
        self._load_modes(acc, sv)
        self.refresh_summaries()
        self.title_lbl.setText(acc.get("name", "?") if acc else "未选择账号")
        self.subtitle.setText("任务勾选后自动保存" if acc else "请先在左侧添加账号")
        self._apply_global_view()

    def _task_opts(self, acc: dict[str, Any] | None, sv: dict, tid: str) -> dict[str, Any]:
        """这个任务存着的选项：每个存档各记各的；账号级任务按登录账号记。"""
        cfg = self._wide_cfg(acc, tid) if acc and tid in self.wide else sv
        return cfg.get("options", {}).get(tid, {})

    def _load_counts(self, acc: dict[str, Any] | None, sv: dict) -> None:
        self._loading_counts = True
        for tid, cvar in self.count_vars.items():
            saved = self._task_opts(acc, sv, tid).get("times", self.specs[tid].times_default)
            cvar.set("" if saved in (None, "") else str(saved))  # 0 也显示 0（自动配置得 0、师徒试炼没存过），不留空
        self._loading_counts = False

    def _load_modes(self, acc: dict[str, Any] | None, sv: dict) -> None:
        for tid, mvar in self.mode_vars.items():
            known = [v for v, _ in self.specs[tid].modes]
            saved = self._task_opts(acc, sv, tid).get("mode")
            mvar.set(saved if saved in known else self.specs[tid].default_mode)

    @property
    def slot(self) -> int:
        return self._slot

    def add(self) -> None:
        acc: dict[str, Any] = {"saves": {"1": {"tasks": [s.id for s in self.specs.values() if s.default and not s.account_wide]}}}
        dlg = AccountDialog(self, self.store, acc)
        dlg.run()
        if dlg.saved:
            self.store.accounts.append(acc)
            self.store.save()
            self.refresh_accounts(len(self.store.accounts) - 1)

    def edit(self) -> None:
        if self.current is None or self.global_view:
            return
        if self._busy():
            notice(self, "提示", "该账号正在执行任务，请等任务结束后再编辑账号资料")
            return
        dlg = AccountDialog(self, self.store, self.store.accounts[self.current])
        dlg.run()
        if dlg.saved:
            self.store.save()
            self.refresh_accounts(self.current)

    def delete(self) -> None:
        if self.current is None or self.global_view:
            return
        if self._busy():
            notice(self, "提示", "该账号正在执行任务，请等任务结束后再删除")
            return
        if ask_confirm(self, "删除账号", "确定删除该账号？", "删除", "取消"):
            del self.store.accounts[self.current]
            self.store.save()
            self.refresh_accounts()

    def _user(self) -> str:
        """当前选中账号的登录名。"""
        return self._acc().get("username", "") if self.current is not None and self.current < len(self.store.accounts) else ""

    def _wide_cfg(self, acc: dict[str, Any], tid: str) -> dict[str, Any]:
        return self.store.wide_cfg(acc.get("username", ""), tid, self.wide[tid])
