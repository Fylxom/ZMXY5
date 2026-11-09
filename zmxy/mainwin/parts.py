"""主窗口专用的小控件（通用控件在 widgets.py）。"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QTextOption
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QWidget

from ..widgets import ACCENT, CARD, PopupMenu, RButton, font


def bring_front(win: QWidget) -> None:
    win.showNormal()
    win.raise_()
    win.activateWindow()


def text_view() -> QPlainTextEdit:
    """日志窗口里的只读大文本框。"""
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setFont(font(10))
    text.setWordWrapMode(QTextOption.WrapAnywhere)
    text.setStyleSheet(f"QPlainTextEdit {{ background: {CARD}; padding: 6px 8px; }}")
    return text


def copy_all_button(text: QPlainTextEdit) -> RButton:
    return RButton("复制全部", lambda: QGuiApplication.clipboard().setText(text.toPlainText()), "ghost", height=24, size=9)


class Fill(QWidget):
    """纯色底的区域（侧栏）。"""

    def __init__(self, color: str) -> None:
        super().__init__()
        self.color = QColor(color)

    def paintEvent(self, e: Any) -> None:
        QPainter(self).fillRect(self.rect(), self.color)


class TaskRow(QWidget):
    """任务行：点名字或空白处 = 选中这个任务（开关和按钮自己处理点击，不会传到这里）。"""

    def __init__(self, on_click: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.on_click = on_click

    def mousePressEvent(self, e: Any) -> None:
        if self.on_click and e.button() == Qt.LeftButton:
            self.on_click()


class EnergyBar(QWidget):
    """时光能量条：实心一段 = 读到的能量；后面接着的浅色一段 = 预估多出来的。"""

    def __init__(self) -> None:
        super().__init__()
        self.part: float | None = None
        self.guess: float | None = None
        self.setFixedSize(150, 8)

    def set(self, part: float | None, guess: float | None = None) -> None:
        self.part, self.guess = part, guess
        self.update()

    def paintEvent(self, e: Any) -> None:
        p_ = QPainter(self)
        p_.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p_.setPen(Qt.NoPen)
        p_.setBrush(QColor("#E3E3E8"))
        p_.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        fills = ((self.guess, 90), (self.part, 255)) if self.guess and self.guess > (self.part or 0) else ((self.part, 255),)
        for part, alpha in fills:  # 先画浅色的预估（从头画到预估值），再把读到的实心一段盖在上面
            if part:
                color = QColor(ACCENT)
                color.setAlpha(alpha)
                p_.setBrush(color)
                p_.drawRoundedRect(QRectF(0, 0, max(h, int(w * min(1.0, part))), h), h / 2, h / 2)


class DetailText(QPlainTextEdit):
    """详情文字：只读，可以划选、Ctrl+C 复制；高度跟随折行后的行数，最多 8 行（再多滚动）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setFont(font(10))
        self.document().setDocumentMargin(0)
        self.setWordWrapMode(QTextOption.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        menu = PopupMenu(self)
        menu.add_command("复制", self.copy)
        menu.add_command("全选", self.selectAll)
        self.customContextMenuRequested.connect(lambda pos: menu.popup(self.mapToGlobal(pos)))

    def show_text(self, text: str) -> None:
        self.setPlainText(text)
        self.fit()

    def fit(self) -> None:
        fm = QFontMetrics(self.font())
        width = max(1, self.viewport().width())
        tall = fm.boundingRect(0, 0, width, 100000, Qt.TextWrapAnywhere.value, self.toPlainText() or " ").height()
        lines = max(1, min(8, -(-tall // fm.lineSpacing())))
        self.setFixedHeight(lines * fm.lineSpacing() + 2)

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        self.fit()


class FocusDrop(QObject):
    """点了输入框以外的地方：Qt 默认不会让输入框失焦（自绘控件不抢焦点），这里把焦点收回。"""

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        if e.type() == QEvent.MouseButtonPress:
            focus = QApplication.focusWidget()
            if isinstance(focus, QLineEdit) and obj is not focus and isinstance(obj, QWidget) and obj.window() is focus.window():
                focus.clearFocus()
        return False
