"""日志页的表格（自绘，只画看得见的行）。"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QWidget

from . import core
from .widgets import ACCENT, BG, LINE, STATUS_COLOR, STATUS_LABEL, SUB, TEXT, PopupMenu, fit, font, vbox


class _Head(QWidget):
    def __init__(self, table: "LogTable") -> None:
        super().__init__()
        self.table = table
        self.setFixedHeight(30)

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setFont(font(9))
        p.setPen(QColor(SUB))
        for (title, _), x in zip(self.table.COLS, self.table.xs):
            p.drawText(QRectF(x, 0, 200, 28), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setPen(QColor(LINE))
        p.drawLine(0, 29, self.width(), 29)


class _Body(QAbstractScrollArea):
    """表格的行区：滚动条用 Qt 的（内容没超出时自己藏起来），画和点击都交给 LogTable。"""

    def __init__(self, table: "LogTable") -> None:
        super().__init__()
        self.table = table
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.ClickFocus)  # 拿到焦点后 Ctrl+C 才会送到这里
        self.verticalScrollBar().setSingleStep(table.rh // 2)

    def resizeEvent(self, e: Any) -> None:
        self.table._resize()

    def paintEvent(self, e: Any) -> None:
        self.table._paint(QPainter(self.viewport()))

    def wheelEvent(self, e: Any) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + (-1 if e.angleDelta().y() > 0 else 1) * self.table.rh)

    def mousePressEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self.table._click(e.position().y())
        elif e.button() == Qt.RightButton:
            self.table._popup(e.position().y(), e.globalPosition().toPoint())

    def keyPressEvent(self, e: Any) -> None:
        if e.key() == Qt.Key_C and e.modifiers() & Qt.ControlModifier:
            self.table.copy_row()
        else:
            super().keyPressEvent(e)


class LogTable(QWidget):
    """日志表。行 = (时间, 账号, 任务, 状态key, 详情)。"""

    COLS = (("时间", 84), ("账号", 120), ("任务", 170), ("状态", 110), ("详情", 0))
    PINNED = ("竞技场", "斗宠大会", "日常任务", "限时任务")  # 没做完（进行中）时始终排在表格最底部，彼此按这个顺序；做完后落在当时的最后一行

    def __init__(self, on_select: Callable[[tuple | None], None]) -> None:
        super().__init__()
        self.on_select = on_select
        self.rows: list[tuple[str, str, str, str, str]] = []
        self.watchers: list[Callable[[list], None]] = []  # 完整日志窗口：哪一行的记录有新增就通知（传那一行的记录表）
        self.done: dict[tuple[str, str], tuple[int, str]] = {}  # (账号, 任务) -> 最终结果那一行的（下标, 详情）；收尾的结果并入同一行
        self.live: dict[tuple[str, str], int] = {}  # (账号, 任务) -> 进行中那一行的下标
        self.trails: list[list[tuple[str, str, str]]] = []  # 与 rows 平行：这一行（任务的这一次执行）的全部 (时间, 状态, 详情)，不被原地更新覆盖
        self.order: list[int] = []  # 与 rows 平行的排序号（越大越靠后）；固定在底部的行做完时重新取号
        self.sel: int | None = None  # rows 里的绝对下标
        self.filter: str | None = None  # 只看某个账号（系统行始终显示，数据更新除外）；None = 全部
        self._view: list[int] = []  # 当前可见行（rows 的下标），见 view
        self._stale = False  # 可见行要重排（下次用到 view 时才排，一批消息只排一次）
        self._next = 0  # 下一个排序号
        self.on_accounts: Callable[[list[str]], None] | None = None  # 出现新账号名时通知筛选框
        self.accounts: list[str] = []
        self.rh = 34
        self.f = font(10)
        self.xs, x = [], 12
        for _, width in self.COLS:
            self.xs.append(x)
            x += width
        self.head, self.cv = _Head(self), _Body(self)
        lay = vbox(self)
        lay.addWidget(self.head)
        lay.addWidget(self.cv, 1)
        self.menu = PopupMenu(self)
        self.menu.add_command("复制本行", self.copy_row)
        self.menu.add_command("复制详情", lambda: self.copy_row(detail_only=True))
        self.menu.add_separator()
        self.menu.add_command("复制全部", self.copy_all)

    # ---- 复制
    def _line(self, row: tuple[str, str, str, str, str]) -> str:
        stamp, acc, task, status, detail = row
        return "\t".join((stamp, self.shown_name(acc), task, STATUS_LABEL.get(status, status), " ".join(detail.split())))

    @staticmethod
    def _copy(text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)

    def copy_row(self, detail_only: bool = False) -> None:
        if self.sel is not None:
            self._copy(self.rows[self.sel][4] if detail_only else self._line(self.rows[self.sel]))

    def copy_all(self) -> None:
        self._copy("\n".join(self._line(self.rows[i]) for i in self.view))

    def _popup(self, y: float, at: Any) -> None:  # 右键：先选中指针下的行，再弹菜单
        self.cv.setFocus()
        i = self._at(y)
        if i is not None and i != self.sel:
            self.select(i)
        on_row = "normal" if i is not None else "disabled"
        self.menu.entryconfigure(0, on_row)
        self.menu.entryconfigure(1, on_row)
        self.menu.entryconfigure(3, "normal" if self.view else "disabled")
        self.menu.popup(at)

    # ---- 画
    def _resize(self) -> None:
        """滚动范围跟上行数（新增行后要先设好，才能滚到底）。"""
        bar, h = self.cv.verticalScrollBar(), self.cv.viewport().height()
        bar.setPageStep(h)
        bar.setRange(0, max(0, len(self.view) * self.rh - h))

    def _bottom(self) -> None:
        bar = self.cv.verticalScrollBar()
        bar.setValue(bar.maximum())

    def redraw(self) -> None:
        """排一次重画：一批进度消息、连续滚动都只画一次（Qt 自己合并），而且只画看得见的那几行。"""
        self._resize()
        self.cv.viewport().update()

    def _ensure_visible(self, i: int) -> None:
        """详情卡片展开、表格变矮后，把选中行滚回可视区。"""
        if self.sel != i:
            return
        try:
            pos = self.view.index(i)
        except ValueError:
            return
        self._resize()
        bar, height = self.cv.verticalScrollBar(), self.cv.viewport().height()
        top, bottom = pos * self.rh, (pos + 1) * self.rh
        if top < bar.value():
            bar.setValue(top)
        elif bottom > bar.value() + height:
            bar.setValue(bottom - height)

    def _paint(self, p: QPainter) -> None:
        port = self.cv.viewport()
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(port.rect(), QColor(BG))
        p.setFont(self.f)
        view, top, w, h, rh = self.view, self.cv.verticalScrollBar().value(), port.width(), port.height(), self.rh
        if not view:
            p.setPen(QColor("#B8B8BD"))
            p.drawText(port.rect(), Qt.AlignCenter, "暂无记录")
            return
        for pos in range(max(0, top // rh), min(len(view), (top + h) // rh + 1)):
            self._draw_row(p, pos * rh - top, view[pos], w)

    def _draw_row(self, p: QPainter, y: int, i: int, w: int) -> None:
        stamp, acc, task, status, detail = self.rows[i]
        sel, rh, xs = i == self.sel, self.rh, self.xs
        if sel:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(0, y + 1, w, rh - 2), 8, 8)
        else:
            p.setPen(QColor(LINE))
            p.drawLine(xs[0], y + rh - 1, w, y + rh - 1)
        fg, dim = ("#FFFFFF", "#D3E3FF") if sel else (TEXT, SUB)

        def text(x: int, room: int, s: str, color: str) -> None:
            p.setPen(QColor(color))
            p.drawText(QRectF(x, y, max(1, room), rh), Qt.AlignLeft | Qt.AlignVCenter, fit(self.f, " ".join(s.split()), room))
        text(xs[0], xs[1] - xs[0], stamp, dim)
        text(xs[1], xs[2] - xs[1] - 10, self.shown_name(acc), fg)
        text(xs[2], xs[3] - xs[2] - 10, task, fg)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFFFFF" if sel else STATUS_COLOR.get(status, SUB)))
        p.drawEllipse(QRectF(xs[3], y + rh / 2 - 4, 8, 8))
        text(xs[3] + 15, xs[4] - xs[3] - 15, STATUS_LABEL.get(status, status), fg)
        text(xs[4], w - xs[4] - 12, detail, fg if sel else dim)

    def _at(self, y: float) -> int | None:  # 行区里的纵坐标 -> rows 下标（点在空白处返回 None）
        pos = int((y + self.cv.verticalScrollBar().value()) // self.rh)
        return self.view[pos] if 0 <= pos < len(self.view) else None

    def _click(self, y: float) -> None:
        i = self._at(y)
        self.select(i if i is not None and i != self.sel else None)

    def select(self, i: int | None) -> None:
        self.sel = i
        self.redraw()
        self.on_select(self.rows[i] if i is not None else None)
        if i is not None:
            # 详情回调会展开底部卡片，等父布局缩小表格后再调整滚动位置。
            QTimer.singleShot(0, lambda selected=i: self._ensure_visible(selected))

    SEP = " · "  # 账号名 · 存档 n

    @classmethod
    def base(cls, acc: str) -> str:  # 日志里的名字 -> 账号（去掉存档部分）
        return acc.split(cls.SEP)[0]

    def shown_name(self, acc: str) -> str:  # 只看单个账号时，账号列只写“存档 n”
        return acc.split(self.SEP, 1)[1] if self.filter is not None and self.base(acc) == self.filter and self.SEP in acc else acc

    def _shown(self, row: tuple[str, str, str, str, str]) -> bool:
        if row[2] == "数据更新":
            return self.filter is None
        return self.filter is None or row[1] == "—" or self.base(row[1]) == self.filter

    @property
    def view(self) -> list[int]:
        """可见行按（是否没做完的固定行，排序号）排：竞技场/斗宠没做完时在最底部。"""
        if self._stale:
            self._stale = False
            pinned = {i: self.PINNED.index(key[1]) for key, i in self.live.items() if key[1] in self.PINNED}  # 下标 -> 固定行内的先后
            self._view = sorted((i for i, r in enumerate(self.rows) if self._shown(r)), key=lambda i: (i in pinned, pinned.get(i, 0), self.order[i]))
        return self._view

    def _rebuild_view(self) -> None:
        self._stale = True

    def set_filter(self, acc: str | None) -> None:
        self.filter = acc
        self._rebuild_view()
        if self.sel is not None and self.sel not in self.view:
            self.select(None)
        else:
            self.redraw()
        self._bottom()

    def add(self, row: tuple[str, str, str, str, str]) -> None:
        if row[1] not in ("", "—") and self.base(row[1]) not in self.accounts:
            self.accounts.append(self.base(row[1]))
            if self.on_accounts:
                self.on_accounts(self.accounts)
        self.rows.append(row)
        self.trails.append([])
        self.order.append(self._next)
        self._next += 1
        if self._stale or any(key[1] in self.PINNED for key in self.live):  # 有固定在底部的行：新行要插到它们前面
            self._rebuild_view()
        elif self._shown(row):  # 普通情况：新行就是最后一行，不用重排
            self._view.append(len(self.rows) - 1)
        self.redraw()
        self._bottom()

    def put(self, acc: str, task: str, status: str, detail: str) -> None:
        """写入一条记录。同一账号的同一任务只占一行：进行中的行原地更新，结束时原地变成最终结果。"""
        row = (time.strftime("%H:%M:%S"), acc, task, status, detail)
        key = (acc, task)
        done = self.done.get(key)
        merged = self._merges(key, status, detail)
        shown = detail[len(done[1]):].lstrip("；") if merged else detail  # 收尾并入同一行：完整日志里只记新增的一段
        i = done[0] if merged else (self.live.pop(key, None) if status != core.PROGRESS else self.live.get(key))
        fresh = i is None or i >= len(self.rows)  # 这个任务新的一次执行：另起一行，完整日志也另起一份
        if fresh:
            if status == core.PROGRESS:
                self.live[key] = len(self.rows)
            else:
                self.done[key] = (len(self.rows), detail)
            self.add(row)
            i = len(self.rows) - 1
        self._extend_trail(self.trails[i], row[0], status, shown)
        if not fresh:
            self._update_row(i, key, row)

    def _merges(self, key: tuple[str, str], status: str, detail: str) -> bool:
        """没有进行中行时，仅把“严格追加了文字”的结果视为收尾补充；完全相同的是再次执行，必须另起一行。"""
        done = self.done.get(key)
        return (status != core.PROGRESS and key not in self.live and done is not None
                and done[0] < len(self.rows) and bool(done[1])
                and detail.startswith(done[1]) and detail != done[1])

    def _extend_trail(self, trail: list[tuple[str, str, str]], stamp: str, status: str, shown: str) -> None:
        if not trail or trail[-1][1:] != (status, shown):
            trail.append((stamp, status, shown))
            for notify in list(self.watchers):
                notify(trail)

    def _update_row(self, i: int, key: tuple[str, str], row: tuple[str, str, str, str, str]) -> None:
        status, detail = row[3], row[4]
        if status != core.PROGRESS:
            self.done[key] = (i, detail)
        self.rows[i] = row
        if key[1] in self.PINNED:  # 做完了：落在当前最后一行，不跳回它最初的位置
            self.order[i] = self._next
            self._next += 1
            if status != core.PROGRESS or sum(k[1] == key[1] for k in self.live) > 1:  # 只是更新进度：位置不变，不用重排（几个账号同时在做同一项时，刚更新的排后面）
                self._rebuild_view()
        self.redraw()
        if self.sel == i:
            self.on_select(row)

    def log_line(self, entry: tuple[str, str, str]) -> str:
        stamp, status, text = entry
        return f"{stamp}  [{STATUS_LABEL.get(status, status)}]  {' '.join(text.split())}"

    def full_log(self, i: int | None) -> str:
        """这一行（任务的这一次执行）的完整日志，一条记录一行，最早的在前。"""
        if i is None:
            return ""
        if not self.trails[i]:
            return " ".join(self.rows[i][4].split())
        return "\n".join(self.log_line(e) for e in self.trails[i])

    def clear(self) -> None:
        self.rows.clear()
        self.order.clear()
        self._view.clear()
        self._stale, self._next = False, 0
        self.live.clear()
        self.done.clear()
        self.trails.clear()
        self.accounts.clear()
        if self.on_accounts:
            self.on_accounts(self.accounts)
        self.select(None)
