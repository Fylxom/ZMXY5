"""日志页：记录表、选中行的详情、完整日志窗口、UID 查询结果弹窗。"""

from __future__ import annotations

import time
from typing import Any, Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPlainTextEdit, QSizePolicy, QWidget

from ..logtable import LogTable
from ..widgets import STATUS_LABEL, Card, Dialog, RButton, Segmented, font, hbox, label, vbox
from .parts import DetailText, bring_front, copy_all_button, text_view


class LogPageMixin:
    def _build_log_page(self) -> None:
        """运行记录：账号筛选、复制全部、清空、记录表，下面是选中某行才出现的详情卡片。"""
        col = vbox(self.pages["log"], margins=(0, 2, 0, 0))
        head = hbox()
        self.table = LogTable(self._show_detail)
        self.log_filter = Segmented([("全部账号", "")], "", lambda: self.table.set_filter(self.log_filter.get() or None), height=24, size=9)  # "" = 全部账号
        head.addWidget(self.log_filter)
        head.addStretch(1)
        head.addWidget(RButton("复制全部", lambda: self.table.copy_all(), "ghost", height=24, size=9))
        head.addWidget(RButton("清空", lambda: self.table.clear(), "ghost", height=24, size=9))
        col.addLayout(head)
        col.addSpacing(2)
        col.addWidget(self.table, 1)

        self.table.on_accounts = self._log_accounts_changed
        self._build_detail_card(col)

    def _log_accounts_changed(self, names: list[str]) -> None:  # 并行执行时按账号分开看日志
        listed = [self.store.label(a) for a in self.store.accounts]  # 按账号列表的顺序排，不按谁先出日志（并行时先后不定）
        names = sorted(names, key=lambda n: listed.index(n) if n in listed else len(listed))
        if self.log_filter.get() and self.log_filter.get() not in names:
            self.log_filter.set("")
            self.table.set_filter(None)
        self.log_filter.set_options([("全部账号", ""), *((n, n) for n in names)])

    def _build_detail_card(self, col: Any) -> None:
        self.detail = Card(padx=16, pady=12)
        box = self.detail.box()
        title_row = hbox()
        self.detail_title = label("", 10, bold=True)
        self.detail_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.detail_title.setMinimumHeight(26)  # 和“完整日志”按钮一样高：按钮藏起来时这一行不变矮
        title_row.addWidget(self.detail_title, 1)
        self.full_log_btn = RButton("完整日志", self._open_full_log, "plain", height=26, size=9)
        title_row.addWidget(self.full_log_btn)
        box.addLayout(title_row)
        box.addSpacing(4)
        self.detail_text = DetailText()
        box.addWidget(self.detail_text)
        self.detail_gap = QWidget()
        self.detail_gap.setFixedHeight(10)
        col.addWidget(self.detail_gap)
        col.addWidget(self.detail)
        self.detail.hide()
        self.detail_gap.hide()

    def _add_log(self, acc: str, task: str, status: str, detail: str) -> None:
        self.table.add((time.strftime("%H:%M:%S"), acc, task, status, detail))

    def _show_detail(self, row: tuple | None) -> None:
        self.detail.setVisible(row is not None)
        self.detail_gap.setVisible(row is not None)
        if row is None:
            return
        stamp, acc, task, status, detail = row
        self.detail_title.setText(f"{task} · {STATUS_LABEL.get(status, status)} · {acc} · {stamp}")
        self.detail_text.show_text(detail or "（无详情）")
        sel = self.table.sel
        self.full_log_btn.setVisible(sel is not None and len(self.table.trails[sel]) > 1)  # 只有一条记录：详情里已经是全部，不用再开完整日志

    def _show_uid_popup(self, popup: Mapping[str, Any]) -> None:
        """UID 结果交给专用弹窗，主窗口不再按原始字段创建页签。"""
        from ..uid.ui import UidProfileDialog

        payload = popup.get("payload")
        title = str(popup.get("title") or "UID资料查询结果")
        if isinstance(payload, Mapping) and "pages" in payload:
            UidProfileDialog(self, payload, title).run()
            return
        text = popup.get("text")
        if not isinstance(text, str) or not text:
            return
        dlg = Dialog(self, title)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(font(9))
        view.setFixedSize(900, 540)
        dlg.body.addWidget(view)
        dlg.buttons("关闭", dlg.accept, cancel=None)
        dlg.run()

    def _open_full_log(self) -> None:
        sel = self.table.sel
        if sel is None:
            return
        row, trail = self.table.rows[sel], self.table.trails[sel]  # 一行 = 任务的一次执行：只看这一次的记录
        key = id(trail)
        shown_win = self.log_windows.get(key)
        if shown_win is not None:  # 这一行的日志窗口已经开着：不再开新的，把它提到最前
            bring_front(shown_win)
            return
        win = QWidget(self, Qt.Window)
        win.setAttribute(Qt.WA_DeleteOnClose)
        self.log_windows[key] = win
        win.setWindowTitle(f"完整日志 · {row[2]} · {row[1]} · {trail[0][0] if trail else row[0]}")
        win.resize(720, 460)
        col = vbox(win)
        text = text_view()
        text.setPlainText(self.table.full_log(sel))
        col.addWidget(text, 1)
        foot = hbox(margins=(10, 6, 10, 6))
        foot.addStretch(1)
        foot.addWidget(copy_all_button(text))
        col.addLayout(foot)
        shown = [len(trail)]

        def live(changed: list) -> None:  # 实时追加新记录；停在底部时跟随滚动，往上翻看时不打扰
            if changed is not trail or len(trail) <= shown[0]:
                return
            bar = text.verticalScrollBar()
            follow = bar.value() >= bar.maximum() - 2
            for entry in trail[shown[0]:]:
                text.appendPlainText(self.table.log_line(entry))
            shown[0] = len(trail)
            if follow:
                bar.setValue(bar.maximum())

        def closed() -> None:
            self.log_windows.pop(key, None)
            if live in self.table.watchers:
                self.table.watchers.remove(live)

        self.table.watchers.append(live)
        win.destroyed.connect(closed)
        win.show()
        text.verticalScrollBar().setValue(text.verticalScrollBar().maximum())
