"""自绘控件和设计令牌（PySide6）。

所有控件都画在主窗口自己的画面上，不向系统另要子窗口：最小化后恢复、切页都是一次画完。
尺寸一律写逻辑像素，高分屏的缩放交给 Qt。外观（颜色、圆角、间距）照原来的 Tk 版。
弹窗显示/曝光/恢复时，在 Qt 事件处理后完整重画并提交缓存，防止系统窗口残留背后画面。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPalette, QPen, QRegion
from PySide6.QtWidgets import QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget


_screens: set[Any] = set()


def hold(screen: Any) -> Any:
    """取到的屏幕对象一直留着不放。不留的话每取一次 PySide 就新建、用完再销毁一个包装；两次点按钮开弹窗时堆损坏闪退
    （2026-10-09、10-11），转储里都停在这一步（docs/LESSONS.md）。留着以后再取就只是查到同一个。"""
    if screen is not None:
        _screens.add(screen)
    return screen


STATUS_LABEL = {
    "progress": "进行中", "submitted": "已完成", "skipped": "跳过",
    "blocked": "已阻止", "error": "出错", "uncertain": "结果未知", "info": "信息", "paused": "已暂停", "todo": "待处理",
}
STATUS_COLOR = {
    "progress": "#0A64F0", "submitted": "#34C759", "skipped": "#AEAEB2",
    "blocked": "#FF9F0A", "error": "#FF3B30", "uncertain": "#FF3B30", "info": "#AEAEB2", "paused": "#FF9F0A", "todo": "#FF9F0A",
}

# ---- 设计令牌
FONT = "Microsoft YaHei UI"
BG = "#FFFFFF"
SIDEBAR = "#F5F5F7"
CARD = "#F5F5F7"
LINE = "#ECECEF"
TEXT = "#1D1D1F"
SUB = "#86868B"
ACCENT = "#0A64F0"
GREEN = "#34C759"
RED = "#E5352B"
WARN = "#D9534F"  # 提醒用的红色：留次数 / 次数提醒的任务名
ORANGE = "#E8870B"  # 选中了但有问题的方块（定时页：该选的没选）
FILL = "#F2F2F5"  # 输入框、下拉框的底色

# 整个程序共用的样式：只管滚动条和文本框这些 Qt 自带的部件，其余控件都是自己画的
QSS = f"""
QScrollArea, QPlainTextEdit {{ border: none; background: transparent; }}
QPlainTextEdit {{ color: {TEXT}; selection-background-color: #B4D5FE; selection-color: {TEXT}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #D8D8DC; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{ background: #BDBDC2; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""

_fonts: dict[tuple[int, bool], QFont] = {}


def font(size: int = 10, bold: bool = False) -> QFont:
    key = (size, bold)
    if key not in _fonts:
        f = QFont(FONT, size)
        f.setBold(bold)
        _fonts[key] = f
    return _fonts[key]


def measure(f: QFont, text: str) -> int:
    return QFontMetrics(f).horizontalAdvance(text)


def fit(f: QFont, text: str, room: int) -> str:
    """放不下就截断加省略号。"""
    return QFontMetrics(f).elidedText(text, Qt.ElideRight, max(1, int(room)))


def set_fg(widget: QWidget, color: str) -> None:
    pal = widget.palette()
    pal.setColor(QPalette.WindowText, QColor(color))
    widget.setPalette(pal)


def label(text: str = "", size: int = 10, color: str = TEXT, bold: bool = False) -> QLabel:
    w = QLabel(text)
    w.setFont(font(size, bold))
    set_fg(w, color)
    return w


class ElideLabel(QLabel):
    """单行灰字摘要：位置够时和普通 QLabel 一样；被挤窄时末尾显示省略号（鼠标停上去看全文），不把右边的按钮挤出窗口。
    全文用自己画的说明浮层（和“说明”同一个），Qt 自带的 setToolTip 在这个窗口里不弹。"""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        tip(self)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def setText(self, text: str) -> None:
        super().setText(text)
        self._sync_tip()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._sync_tip()

    def _sync_tip(self) -> None:
        cut = measure(self.font(), self.text()) > self.width()
        set_tip(self, self.text() if cut else "")

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(QPalette.WindowText))
        p.drawText(self.rect(), int(Qt.AlignLeft | Qt.AlignVCenter), fit(self.font(), self.text(), self.width()))


def elide_label(text: str = "", size: int = 10, color: str = TEXT) -> ElideLabel:
    w = ElideLabel(text)
    w.setFont(font(size))
    set_fg(w, color)
    return w


def apply_theme(app: Any) -> None:
    """启动时调用一次：字体、底色、文字颜色、滚动条样式。"""
    app.setFont(font(10))
    pal = app.palette()
    for role, color in ((QPalette.Window, BG), (QPalette.Base, BG), (QPalette.WindowText, TEXT), (QPalette.Text, TEXT)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(QSS)


def hbox(parent: QWidget | None = None, spacing: int = 0, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout(parent) if parent is not None else QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def vbox(parent: QWidget | None = None, spacing: int = 0, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout(parent) if parent is not None else QVBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    """一页内容：比窗口高就滚动，不把行压扁。返回（滚动区, 往里加东西的竖排布局）。"""
    area = QScrollArea()
    area.setFrameShape(QFrame.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    inner = QWidget()
    area.setWidget(inner)
    return area, vbox(inner)


def clear_layout(lay: QLayout) -> None:
    """把布局里的东西全部拆掉（控件销毁，子布局递归）。"""
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


# ---- 说明浮层
class _Tip(QObject):
    """鼠标停在控件上时在它下面浮出一段说明，移开就收起。"""

    def __init__(self, widget: QWidget, text: str) -> None:
        super().__init__(widget)
        self.widget, self.text, self.pop = widget, text, None
        widget.installEventFilter(self)

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        if e.type() == QEvent.Enter:
            self.show()
        elif e.type() in (QEvent.Leave, QEvent.Hide, QEvent.MouseButtonPress):
            self.hide()
        return False

    def show(self) -> None:
        self.hide()
        if not self.text:
            return
        pop = self.pop = _TipPop(self.text)
        at = self.widget.mapToGlobal(QPoint(-14, self.widget.height() + 6))  # 文字和“说明”两个字左对齐
        area = hold(QGuiApplication.screenAt(at) or QGuiApplication.primaryScreen()).availableGeometry()
        if at.y() + pop.height() > area.bottom():  # 下面放不下就放到上面
            at.setY(self.widget.mapToGlobal(QPoint(0, 0)).y() - pop.height() - 6)
        pop.move(max(area.left() + 4, min(at.x(), area.right() - pop.width() - 4)), at.y())
        pop.show()

    def hide(self) -> None:
        if self.pop is not None:
            self.pop.close()
            self.pop.deleteLater()
            self.pop = None


class _TipPop(QWidget):
    """说明浮层：白底圆角（和下拉菜单一个样子），一行一行自己画。"""

    PADX, PADY, GAP, MAXW = 14, 10, 6, 480

    def __init__(self, text: str) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.f = font(10)
        fm = QFontMetrics(self.f)
        self.lines: list[str] = []
        for para in text.split("\n"):  # 一般一行放得下；超过最大宽度的才折行
            cur = ""
            for ch in para:
                if cur and fm.horizontalAdvance(cur + ch) > self.MAXW:
                    self.lines.append(cur)
                    cur = ""
                cur += ch
            self.lines.append(cur)
        self.step = fm.height() + self.GAP
        self.setFixedSize(max(fm.horizontalAdvance(line) for line in self.lines) + 2 * self.PADX + 2,
                          len(self.lines) * self.step - self.GAP + 2 * self.PADY)
        self.rounded = _round_window(self, "#DCDCE2")

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self.rounded:  # 旧系统没有圆角：自己画细边
            p.setPen(QColor("#D8D8DE"))
            p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        p.setFont(self.f)
        p.setPen(QColor(TEXT))
        for i, line in enumerate(self.lines):
            p.drawText(QRectF(self.PADX, self.PADY + i * self.step, self.width(), self.step - self.GAP), Qt.AlignLeft | Qt.AlignVCenter, line)


def tip(widget: QWidget, text: str = "") -> None:
    widget._tip = _Tip(widget, text)


def set_tip(widget: QWidget, text: str) -> None:
    widget._tip.text = text


def hint(text: str) -> QLabel:
    """一个灰色的“说明”小字：长说明不占版面，鼠标停上去才显示。"""
    w = label("说明", 9, SUB, True)
    w.setCursor(Qt.WhatsThisCursor)
    tip(w, text)
    return w


# ---- 自绘控件
def _cursor_on(w: QWidget) -> bool:
    """鼠标现在是不是在这个控件上（按实际位置算，不靠进入 / 离开事件）。"""
    return w.isVisible() and w.rect().contains(w.mapFromGlobal(QCursor.pos()))


class _Hoverable(QWidget):
    """鼠标移上去要变样的控件。"""

    def __init__(self) -> None:
        super().__init__()
        self.hover = False

    def enterEvent(self, e: Any) -> None:
        self.hover = True
        self.update()

    def leaveEvent(self, e: Any) -> None:
        self.hover = False
        self.update()

    def moveEvent(self, e: Any) -> None:
        """重画表格时新控件先摆在临时位置，正好在鼠标下就收到“进入”，排好位置后却收不到“离开”，一直是悬停色：挪动后按实际位置重算。"""
        super().moveEvent(e)
        if self.hover != _cursor_on(self):
            self.hover = not self.hover
            self.update()


class Card(QWidget):
    """圆角卡片。内容用 grid() / box() 得到的布局往里放。"""

    def __init__(self, padx: int = 16, pady: int = 4, fill: str = CARD, radius: int = 12) -> None:
        super().__init__()
        self.fill, self.radius, self.pad = fill, radius, (padx, pady, padx, pady)

    def grid(self) -> QGridLayout:
        lay = QGridLayout(self)
        lay.setContentsMargins(*self.pad)
        lay.setSpacing(0)
        return lay

    def box(self, horizontal: bool = False) -> QVBoxLayout | QHBoxLayout:
        return hbox(self, margins=self.pad) if horizontal else vbox(self, margins=self.pad)

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.fill))
        p.drawRoundedRect(QRectF(self.rect()), self.radius, self.radius)


class RButton(QWidget):
    """全圆角按钮（圆角 = 高度的一半）。"""

    KINDS = {  # kind: (normal, hover, pressed, fg)
        "primary": (ACCENT, "#0958D6", "#084CBA", "#FFFFFF"),
        "plain": ("#EFEFF2", "#E6E6EA", "#DCDCE1", TEXT),
        "stop": ("#EFEFF2", "#E6E6EA", "#DCDCE1", RED),
        "side": ("#E9E9ED", "#E0E0E5", "#D6D6DC", TEXT),
        "ghost": (BG, "#F2F2F5", "#E8E8EC", SUB),
    }

    def __init__(self, text: str, command: Callable[[], None], kind: str = "plain",
                 width: int | None = None, height: int = 30, size: int = 10) -> None:
        super().__init__()
        self.text, self.command, self.kind, self.enabled, self.state = text, command, kind, True, "normal"
        self.f, self._fixed = font(size), width
        self.setFixedSize(width or measure(self.f, text) + 28, height)
        self.setCursor(Qt.PointingHandCursor)

    def set_enabled(self, enabled: bool) -> None:
        if enabled != self.enabled:
            self.enabled = enabled
            self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
            self.update()

    def set_text(self, text: str, kind: str | None = None) -> None:
        self.text, self.kind = text, kind or self.kind
        if not self._fixed:
            self.setFixedWidth(measure(self.f, text) + 28)
        self.update()

    def _set(self, state: str) -> None:
        self.state = state
        self.update()

    def enterEvent(self, e: Any) -> None:
        self._set("hover")

    def leaveEvent(self, e: Any) -> None:
        self._set("normal")

    def moveEvent(self, e: Any) -> None:
        """同 _Hoverable.moveEvent：挪动后按鼠标实际位置重算悬停。"""
        super().moveEvent(e)
        if self.state != "pressed" and (self.state == "hover") != _cursor_on(self):
            self._set("hover" if self.state == "normal" else "normal")

    def mousePressEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self._set("pressed")

    def mouseReleaseEvent(self, e: Any) -> None:
        if e.button() != Qt.LeftButton:
            return
        inside = self.rect().contains(e.position().toPoint())
        self._set("hover" if inside else "normal")
        if inside and self.enabled:
            self.command()

    def paintEvent(self, e: Any) -> None:
        normal, hover, pressed, fg = self.KINDS[self.kind]
        fill = {"normal": normal, "hover": hover, "pressed": pressed}[self.state] if self.enabled else "#F4F4F6"
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(fill))
        p.drawRoundedRect(QRectF(self.rect()), self.height() / 2, self.height() / 2)
        p.setFont(self.f)
        p.setPen(QColor(fg if self.enabled else "#C2C2C7"))
        p.drawText(self.rect(), Qt.AlignCenter, self.text)


class Switch(QWidget):
    """开关。get() / set()；用户点了才调 command（set 不调）。"""

    def __init__(self, on: bool = False, command: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.on, self.command, self.pos = bool(on), command, 1.0 if on else 0.0
        self.setFixedSize(34, 20)
        self.setCursor(Qt.PointingHandCursor)
        self.anim = QVariantAnimation(self, duration=120, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._step)

    def get(self) -> bool:
        return self.on

    def set(self, on: bool) -> None:
        self.on = bool(on)
        self.anim.stop()
        self.pos = 1.0 if self.on else 0.0
        self.update()

    def _step(self, v: float) -> None:
        self.pos = float(v)
        self.update()

    def mousePressEvent(self, e: Any) -> None:
        if e.button() != Qt.LeftButton:
            return
        self.on = not self.on
        self.anim.stop()
        self.anim.setStartValue(self.pos)
        self.anim.setEndValue(1.0 if self.on else 0.0)
        self.anim.start()
        if self.command:
            self.command()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor("#DCDCE0"), QColor(GREEN)
        mix = QColor(*(round(off.getRgb()[i] + (on.getRgb()[i] - off.getRgb()[i]) * self.pos) for i in range(3)))
        p.setPen(Qt.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(QRectF(0, 0, 34, 20), 10, 10)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(2 + 14 * self.pos, 2, 16, 16))


class Segmented(QWidget):
    """分段控件。options: [(显示文字, 值)]。get() / set()；用户点了才调 command。值不在选项里 = 这一条没有选中的。"""

    TRACK = "#EFEFF2"

    def __init__(self, options: list[tuple[str, str]], value: str = "", command: Callable[[], None] | None = None,
                 seg_width: int | None = None, height: int = 30, size: int = 10) -> None:
        super().__init__()
        self.options, self.value, self.command = options, value, command
        self.f, self.fixed_w, self.pad = font(size), seg_width, 2
        self.setFixedHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.anim = QVariantAnimation(self, duration=150, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._step)
        self._measure()
        self.x = float(max(0, self._index()) * self.seg_w)

    def _measure(self) -> None:
        self.seg_w = self.fixed_w or max(measure(self.f, t) for t, _ in self.options) + 24
        self.setFixedWidth(self.seg_w * len(self.options) + 2 * self.pad)

    def _index(self) -> int:
        return next((i for i, (_, v) in enumerate(self.options) if v == self.value), -1)

    def _step(self, v: float) -> None:
        self.x = float(v)
        self.update()

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value
        self.anim.stop()
        self.x = float(max(0, self._index()) * self.seg_w)
        self.update()

    def set_options(self, options: list[tuple[str, str]]) -> None:
        """换一组选项（如日志页的账号列表随运行出现）。"""
        self.options = options
        self._measure()
        self.set(self.value)

    def mousePressEvent(self, e: Any) -> None:
        if e.button() != Qt.LeftButton:
            return
        i = min(max(int((e.position().x() - self.pad) // self.seg_w), 0), len(self.options) - 1)
        if self.options[i][1] != self.value:
            had = self._index() >= 0
            self.value = self.options[i][1]
            self.anim.stop()
            if had:
                self.anim.setStartValue(self.x)
                self.anim.setEndValue(float(i * self.seg_w))
                self.anim.start()
            else:
                self.x = float(i * self.seg_w)
                self.update()
            if self.command:
                self.command()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h, pad = self.height(), self.pad
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.TRACK))
        p.drawRoundedRect(QRectF(0, 0, self.width(), h), h / 2, h / 2)
        if self._index() >= 0:
            p.setPen(QPen(QColor("#E2E2E6"), 1))
            p.setBrush(QColor("#FFFFFF"))
            p.drawRoundedRect(QRectF(pad + self.x + 0.5, pad + 0.5, self.seg_w - 1, h - 2 * pad - 1), (h - 2 * pad) / 2, (h - 2 * pad) / 2)
        p.setFont(self.f)
        for i, (text, value) in enumerate(self.options):
            p.setPen(QColor(TEXT if value == self.value else SUB))
            p.drawText(QRectF(pad + i * self.seg_w, 0, self.seg_w, h), Qt.AlignCenter, text)


class Field(QLineEdit):
    """圆角输入框，聚焦时出现蓝色描边。on_change：内容变了就调（程序改的也调，和原来绑在变量上的行为一样）。"""

    def __init__(self, text: str = "", width: int = 300, height: int = 36, password: bool = False,
                 on_change: Callable[[], None] | None = None, max_len: int | None = None) -> None:
        super().__init__(text)
        self.setFixedSize(width, height)
        self.setFont(font(10))
        pad = 10 if width >= 60 else 6
        self.setStyleSheet(f"QLineEdit {{ background: {FILL}; color: {TEXT}; border: 2px solid {FILL}; border-radius: 9px; padding: 0 {pad}px;"
                           f" selection-background-color: #B4D5FE; selection-color: {TEXT}; }}"
                           f"QLineEdit:focus {{ border: 2px solid {ACCENT}; }}")
        if password:
            self.setEchoMode(QLineEdit.Password)
        if max_len:
            self.setMaxLength(max_len)
        if on_change:
            self.textChanged.connect(lambda _t: on_change())

    def get(self) -> str:
        return self.text()

    def set(self, text: str) -> None:
        if text != self.text():
            self.setText(text)


class ClockField(Field):
    """“时:分”输入框：一直是 00:00 的样子，敲数字从右边推进来（00:00 → 00:01 → 00:12 → 01:23 → 12:34），退格往回退一位。
    敲到一半不是个时间（如 00:78）照样显示，valid() 为假；这样离开输入框就回到上一个对的时间。"""

    def __init__(self, width: int = 64, height: int = 24, on_change: Callable[[], None] | None = None) -> None:
        super().__init__("00:00", width, height, on_change=on_change)
        self.setReadOnly(True)  # 不让 Qt 自己改字（粘贴、拖放、输入法），按键都在 keyPressEvent 里处理
        self.good = "00:00"

    def valid(self) -> bool:
        return int(self.text()[:2]) <= 23 and int(self.text()[3:]) <= 59

    def set(self, text: str) -> None:
        digits = "".join(c for c in str(text) if c.isdigit())[-4:].rjust(4, "0")
        super().set(f"{digits[:2]}:{digits[2:]}")
        if self.valid():
            self.good = self.text()

    def keyPressEvent(self, e: Any) -> None:
        digits = self.text().replace(":", "")
        if e.text().isdigit() and len(e.text()) == 1 and e.text().isascii():
            self.set(digits[1:] + e.text())
        elif e.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            self.set("0" + digits[:3])
        else:
            e.ignore()  # Tab 等交给上层

    def focusOutEvent(self, e: Any) -> None:
        if not self.valid():
            self.set(self.good)
        super().focusOutEvent(e)


class AccountRow(_Hoverable):
    def __init__(self, name: str, note: str, selected: bool, on_click: Callable[[], None], on_double: Callable[[], None]) -> None:
        super().__init__()
        self.name, self.note, self.selected = name, note, selected
        self.on_click, self.on_double = on_click, on_double
        self.setFixedHeight(34)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self.on_click()

    def mouseDoubleClickEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self.on_double()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if self.selected or self.hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#E4E4E9" if self.selected else "#ECECF0"))
            p.drawRoundedRect(QRectF(0, 0, w, h), 8, 8)
        note_w = measure(font(9), self.note) if self.note else 0
        p.setFont(font(10))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(12, 0, w - 24, h), Qt.AlignLeft | Qt.AlignVCenter, fit(font(10), self.name, w - 24 - note_w - (8 if note_w else 0)))
        if self.note:
            p.setFont(font(9))
            p.setPen(QColor(SUB))
            p.drawText(QRectF(12, 0, w - 24, h), Qt.AlignRight | Qt.AlignVCenter, self.note)


class StatusPill(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.setFixedHeight(32)

    def set_busy(self, busy: bool) -> None:
        if busy != self.busy:
            self.busy = busy
            self.update()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        p.setPen(QPen(QColor("#E6E6EA"), 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, h - 1), 10, 10)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FF9F0A" if self.busy else GREEN))
        p.drawEllipse(QRectF(12, h / 2 - 4, 8, 8))
        p.setFont(font(9))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(28, 0, self.width() - 36, h), Qt.AlignLeft | Qt.AlignVCenter, "运行中" if self.busy else "空闲")


class Chip(_Hoverable):
    """可点选的圆角方块（选关用）。"""

    def __init__(self, text: str, on: bool, command: Callable[[], None], width: int = 112, height: int = 34, on_card: bool = False) -> None:
        super().__init__()
        self.text, self.on, self.command = text, on, command
        self.idle = BG if on_card else CARD  # 放在灰色卡片里时没选中用白底，不然和卡片分不出来
        self.sub = ""  # 名字下面的一行小字（世界Boss 的估伤）
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)

    def note(self, sub: str) -> None:
        self.sub = sub
        self.update()

    def set(self, on: bool) -> None:
        if on != self.on:
            self.on = on
            self.update()

    def mousePressEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self.command()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if self.on:
            p.setPen(QPen(QColor(ACCENT), 1))
            p.setBrush(QColor("#E4EEFE"))
            p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 9, 9)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ECECF0" if self.hover else self.idle))
            p.drawRoundedRect(QRectF(0, 0, w, h), 9, 9)
        p.setFont(font(10))
        p.setPen(QColor(ACCENT if self.on else TEXT))
        if not self.sub:
            p.drawText(self.rect(), Qt.AlignCenter, fit(font(10), self.text, w - 16))
            return
        p.drawText(QRectF(0, 3, w, h / 2), Qt.AlignCenter, fit(font(10), self.text, w - 16))
        p.setFont(font(8))
        p.setPen(QColor(SUB))
        p.drawText(QRectF(0, h / 2 - 1, w, h / 2 - 3), Qt.AlignCenter, fit(font(8), self.sub, w - 8))


class Check(_Hoverable):
    """勾选框（定时页“参加的存档”表的格子）：没勾 = 白底灰边，勾上 = 蓝底白勾，勾了但有问题（warn）= 橙底白勾。
    整个格子都能点，框画在中间。"""

    BOX = 16

    def __init__(self, on: bool, command: Callable[[], None], width: int = 30, height: int = 24) -> None:
        super().__init__()
        self.on, self.warn, self.command = on, False, command
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)

    def set(self, on: bool) -> None:
        if on != self.on:
            self.on = on
            self.update()

    def mark(self, warn: bool) -> None:
        if warn != self.warn:
            self.warn = warn
            self.update()

    def mousePressEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self.command()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        b = self.BOX
        box = QRectF((self.width() - b) / 2 + 0.5, (self.height() - b) / 2 + 0.5, b - 1, b - 1)
        if not self.on:
            p.setPen(QPen(QColor(ACCENT if self.hover else "#C7C7CC"), 1.2))
            p.setBrush(QColor(BG))
            p.drawRoundedRect(box, 4, 4)
            return
        fill = QColor(ORANGE if self.warn else ACCENT)
        p.setPen(Qt.NoPen)
        p.setBrush(fill.darker(110) if self.hover else fill)
        p.drawRoundedRect(box, 4, 4)
        pen = QPen(QColor("#FFFFFF"), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        x, y = box.left(), box.top()
        p.drawPolyline([QPointF(x + b * 0.25, y + b * 0.52), QPointF(x + b * 0.43, y + b * 0.70), QPointF(x + b * 0.75, y + b * 0.32)])


# ---- 浮层：下拉菜单、右键菜单
def _round_window(win: QWidget, border: str) -> bool:
    """让无边框弹窗用系统的圆角、细描边和阴影（Windows 11）；旧系统不支持时返回 False，调用方自己画直角边框。"""
    try:
        import ctypes
        hwnd = int(win.winId())
        c = QColor(border)
        attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        ok = attr(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4) == 0  # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        attr(hwnd, 34, ctypes.byref(ctypes.c_int(c.blue() << 16 | c.green() << 8 | c.red())), 4)  # DWMWA_BORDER_COLOR
        return ok
    except Exception:
        return False


class _Popup(QWidget):
    """白底浮层的共同部分：点别处 / Esc / 切走就收起（Qt 的 Popup 窗口自己管）。"""

    def __init__(self, parent: QWidget, w: int, h: int) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setFixedSize(w, h)
        self.rounded = _round_window(self, "#DCDCE2")
        self.hover = -1

    def show_at(self, x: int, y: int, above: int | None = None) -> None:
        """放在 (x, y)；下面放不下时：above 给了就贴着它的上边向上弹，否则往上挪到放得下。"""
        screen = hold(QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen())
        area = screen.availableGeometry()
        if y + self.height() + 2 > area.bottom():
            y = above - self.height() if above is not None else area.bottom() - self.height() - 4
        self.move(max(area.left(), min(x, area.right() - self.width() - 4)), max(area.top(), y))
        self.show()

    def _frame(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self.rounded:
            p.setPen(QColor("#D8D8DE"))
            p.drawRect(0, 0, self.width() - 1, self.height() - 1)

    def mouseMoveEvent(self, e: Any) -> None:
        i = self._row_at(e.position())
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e: Any) -> None:
        self.hover = -1
        self.update()


class _SelectMenu(_Popup):
    RH, PAD = 34, 5

    def __init__(self, owner: "Select") -> None:
        self.owner, self.names, self.cur = owner, owner.names, owner.value
        self.rows = min(len(self.names), 8)
        super().__init__(owner, max(owner.width(), 160), self.rows * self.RH + 2 * self.PAD)
        self.scrolls = len(self.names) > self.rows
        self.top = max(0, min(self.names.index(self.cur) - self.rows // 2, len(self.names) - self.rows)) if self.scrolls and self.cur in self.names else 0

    def _row_at(self, pos: QPointF) -> int:
        y = pos.y() - self.PAD
        i = int(y // self.RH) + self.top
        return i if 0 <= y < self.rows * self.RH and 0 <= i < len(self.names) else -1

    def wheelEvent(self, e: Any) -> None:
        if self.scrolls:
            self.top = max(0, min(self.top + (-1 if e.angleDelta().y() > 0 else 1), len(self.names) - self.rows))
            self.hover = self._row_at(e.position())
            self.update()

    def mousePressEvent(self, e: Any) -> None:
        if not self.rect().contains(e.position().toPoint()):  # 点在菜单外：收起
            self.close()
            return
        i = self._row_at(e.position())
        if i >= 0:
            self.close()
            self.owner._pick(self.names[i])

    def closeEvent(self, e: Any) -> None:
        self.owner._closed()
        super().closeEvent(e)

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._frame(p)
        w, h, rh, pad = self.width(), self.height(), self.RH, self.PAD
        f, small = font(10), font(9)
        right = w - (22 if self.scrolls else 16)  # 文字右边界（要滚动时给滚动条让位）
        for i, name in enumerate(self.names):
            top = pad + (i - self.top) * rh
            if not -rh < top < h:
                continue
            on = name == self.cur
            if on or i == self.hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#E3EDFF" if on and i == self.hover else "#EEF4FF" if on else "#F2F2F5"))
                p.drawRoundedRect(QRectF(5, top + 1, w - 10 - (6 if self.scrolls else 0), rh - 2), 7, 7)
            main, _, sub = name.partition("\t")
            sub_w = measure(small, sub) + 12 if sub else 0
            if sub:
                p.setFont(small)
                p.setPen(QColor("#6F9BE8" if on else SUB))
                p.drawText(QRectF(16, top, right - 16, rh), Qt.AlignRight | Qt.AlignVCenter, sub)
            p.setFont(f)
            p.setPen(QColor(ACCENT if on else TEXT))
            p.drawText(QRectF(16, top, right - 16 - sub_w, rh), Qt.AlignLeft | Qt.AlignVCenter, fit(f, main, right - 16 - sub_w))
        if self.scrolls:  # 细滚动条：只是位置提示，滚轮滚动
            span = h - 2 * pad
            bar = max(24, span * self.rows // len(self.names))
            at = pad + (span - bar) * self.top // (len(self.names) - self.rows)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#C9C9CF"))
            p.drawRoundedRect(QRectF(w - 7, at, 3, bar), 1.5, 1.5)


class Select(_Hoverable):
    """圆角下拉框：点开弹出菜单。get() 是选中的名字。名字里 \\t 后面的部分是灰色小字（菜单里靠右对齐）。"""

    def __init__(self, names: list[str], value: str, width: int = 260, height: int = 32, size: int = 10,
                 command: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.names, self.value, self.command, self.f = names, value, command, font(size)
        self.pop = None
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value
        self.update()

    def _pick(self, name: str) -> None:
        if name != self.value:
            self.value = name
            self.update()
            if self.command:
                self.command()

    def _closed(self) -> None:
        self.pop = None
        self.update()

    def mousePressEvent(self, e: Any) -> None:
        if e.button() != Qt.LeftButton or not self.names:
            return
        self.pop = _SelectMenu(self)
        at = self.mapToGlobal(QPoint(0, 0))
        self.pop.show_at(at.x(), at.y() + self.height() + 5, above=at.y() - 5)
        self.update()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        small, opened = h < 30, self.pop is not None  # 行上的小号下拉：边距收紧；展开时：蓝色描边，箭头朝上
        if opened:
            p.setPen(QPen(QColor(ACCENT), 2))
            p.setBrush(QColor(FILL))
            p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 8, 8)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#E9E9EE" if self.hover else FILL))
            p.drawRoundedRect(QRectF(0, 0, w, h), 9, 9)
        left, right = (9 if small else 12), w - (24 if small else 40)
        main, _, sub = self.value.partition("\t")
        if sub and not small:
            p.setFont(font(9))
            p.setPen(QColor(SUB))
            p.drawText(QRectF(left, 0, right - left, h), Qt.AlignRight | Qt.AlignVCenter, sub)
            right -= measure(font(9), sub) + 10
        p.setFont(self.f)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(left, 0, right - left, h), Qt.AlignLeft | Qt.AlignVCenter, fit(self.f, main, right - left))
        x, y, d, s = w - (12 if small else 18), h / 2, (3 if small else 4), (-1 if opened else 1)  # 右侧的小箭头
        pen = QPen(QColor(ACCENT if opened else SUB), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(x - d, y - s * d / 2), QPointF(x, y + s * d / 2), QPointF(x + d, y - s * d / 2)])


class _MenuPopup(_Popup):
    RH, GAP, PAD = 30, 9, 5

    def __init__(self, parent: QWidget, items: list[dict[str, Any] | None]) -> None:
        self.items, self.tops, h = items, [], self.PAD
        for it in items:
            self.tops.append(h)
            h += self.RH if it else self.GAP
        w = max(128, max((measure(font(9), it["label"]) for it in items if it), default=0) + 40)
        super().__init__(parent, w, h + self.PAD)

    def _row_at(self, pos: QPointF) -> int:
        for i, (it, top) in enumerate(zip(self.items, self.tops)):
            if it is not None and top <= pos.y() < top + self.RH:
                return i
        return -1

    def mousePressEvent(self, e: Any) -> None:
        i = self._row_at(e.position()) if self.rect().contains(e.position().toPoint()) else -1
        if i < 0:
            if not self.rect().contains(e.position().toPoint()):
                self.close()
            return
        if self.items[i]["state"] == "normal":
            self.close()
            self.items[i]["command"]()

    def paintEvent(self, e: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._frame(p)
        w = self.width()
        p.setFont(font(9))
        for i, (it, top) in enumerate(zip(self.items, self.tops)):
            if it is None:
                p.setPen(QColor(LINE))
                p.drawLine(12, top + self.GAP // 2, w - 12, top + self.GAP // 2)
                continue
            on = it["state"] == "normal"
            if on and i == self.hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#F2F2F5"))
                p.drawRoundedRect(QRectF(5, top + 1, w - 10, self.RH - 2), 7, 7)
            p.setPen(QColor(TEXT if on else "#B8B8BD"))
            p.drawText(QRectF(16, top, w - 32, self.RH), Qt.AlignLeft | Qt.AlignVCenter, it["label"])


class PopupMenu:
    """自绘的右键菜单：白色圆角浮层，悬停高亮；点一项执行并收起，点别处 / Esc / 切走收起。序号把分隔线算在内。"""

    def __init__(self, master: QWidget) -> None:
        self.master = master
        self.items: list[dict[str, Any] | None] = []  # None = 分隔线

    def add_command(self, label: str, command: Callable[[], None]) -> None:
        self.items.append({"label": label, "command": command, "state": "normal"})

    def add_separator(self) -> None:
        self.items.append(None)

    def entryconfigure(self, index: int, state: str) -> None:
        self.items[index]["state"] = state

    def popup(self, pos: QPoint) -> None:
        _MenuPopup(self.master, self.items).show_at(pos.x(), pos.y())


# ---- 提示框
def place_on(win: QWidget, master: QWidget | None) -> None:
    """按窗口现在的大小放在主窗口上方三分之一处；放不下就往上、往左挪，整个留在屏幕里（标题栏和下面的按钮都看得见）。"""
    if master is None:
        return
    top = master.window()
    at = top.mapToGlobal(QPoint(0, 0))
    x, y = at.x() + (top.width() - win.width()) // 2, at.y() + (top.height() - win.height()) // 3
    screen = hold(top.screen() or win.screen()).availableGeometry()
    bar = max(win.frameGeometry().height() - win.height(), 32)  # 标题栏的高度（还没显示时取不到，按 32 算）
    x = max(screen.left() + 8, min(x, screen.right() - win.width() - 8))
    y = max(screen.top() + bar, min(y, screen.bottom() - win.height() - 8))
    win.move(x, y)


def center_on(win: QWidget, master: QWidget | None) -> None:
    """放在主窗口上方三分之一处（显示之前就算好位置）。"""
    win.adjustSize()
    place_on(win, master)


class Dialog(QDialog):
    """白底固定布局弹窗；显示/重新曝光后完整提交画面。子类把内容放进 self.body。

    Qt 的内部缓存正确不等于系统窗口已经完整显示：只更新鼠标经过的小块无法修复其余残影。
    只响应 Qt 显示事件，合并成一次带 QObject 生命周期的延后刷新；不截屏、不轮询、不拦系统消息。
    """

    def __init__(self, master: QWidget | None, title: str, margins: tuple[int, int, int, int] = (24, 18, 24, 18)) -> None:
        super().__init__(master)
        self._surface_window = None
        self._surface_refresh_pending = False
        self._surface_refresh_needed = False
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(BG))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setWindowTitle(title)
        self.body = vbox(self, margins=margins)
        self.body.setSizeConstraint(QLayout.SetFixedSize)
        self.on_return: Callable[[], None] | None = None

    def showEvent(self, e: Any) -> None:
        super().showEvent(e)
        window = self.windowHandle()
        if window is not None and window is not self._surface_window:
            self._surface_window = window
            window.installEventFilter(self)  # Expose 发给 QWindow，不会传成 QWidget.paintEvent。
        self._queue_surface_refresh()

    def event(self, e: QEvent) -> bool:
        result = super().event(e)
        if hasattr(self, "_surface_refresh_needed"):
            if e.type() in (QEvent.WindowActivate, QEvent.WindowStateChange):
                self._queue_surface_refresh()
            elif e.type() == QEvent.UpdateRequest and self._surface_refresh_needed:
                self._queue_surface_refresh()  # 重建期间禁止刷新时，等恢复后的首次 update 再提交。
        return result

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        if obj is self._surface_window and e.type() == QEvent.Expose:
            self._queue_surface_refresh()
        return super().eventFilter(obj, e)

    def _queue_surface_refresh(self) -> None:
        self._surface_refresh_needed = True
        if not self._surface_refresh_pending:
            self._surface_refresh_pending = True
            QTimer.singleShot(0, self, self._refresh_surface)

    def _refresh_surface(self) -> None:
        self._surface_refresh_pending = False
        if not self.isVisible() or self.isMinimized():
            self._surface_refresh_needed = False
            return
        window = self.windowHandle()
        if not self.updatesEnabled() or window is None or not window.isExposed():
            return  # 不绕过重建时的暂停；下一次 update/Expose 会补上，不用定时重试。
        self._surface_refresh_needed = False
        self.repaint()  # 先完成整窗和子控件绘制，再把完整缓存交给系统，不能仅依赖局部脏区。
        backing = self.backingStore()
        if backing is not None and not backing.size().isEmpty():
            backing.flush(QRegion(self.rect()), window)

    def keyPressEvent(self, e: Any) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.on_return:
                self.on_return()
        elif e.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(e)

    def run(self) -> None:
        center_on(self, self.parentWidget())
        self.exec()

    def resizeEvent(self, e: Any) -> None:
        """内容变多变少（选了翅膀、切了页签）窗口大小跟着变：重新摆一次，别让下半截掉到屏幕外面。"""
        super().resizeEvent(e)
        if self.isVisible():
            place_on(self, self.parentWidget())

    def buttons(self, ok: str, on_ok: Callable[[], None], cancel: str | None = "取消", top: int = 18, width: int = 76) -> QHBoxLayout:
        """最下面一行：右边确认，左边取消。返回这一行的布局（左边还能再放东西）。"""
        self.body.addSpacing(top)
        row = hbox()
        row.addStretch(1)
        if cancel is not None:
            row.addWidget(RButton(cancel, self.reject, "plain", width=width))
            row.addSpacing(8)
        row.addWidget(RButton(ok, on_ok, "primary", width=width))
        self.body.addLayout(row)
        return row


def _message(master: QWidget | None, title: str, text: str) -> Dialog:
    """提示框的共同部分：一段文字，按钮由调用方往下放。"""
    dlg = Dialog(master, title, (24, 20, 24, 18))
    msg = label(text)
    msg.setWordWrap(True)
    msg.setFixedWidth(min(380, max(measure(font(10), line) for line in text.split("\n")) + 4))
    dlg.body.addWidget(msg)
    dlg.body.addSpacing(14)
    return dlg


def _ask(master: QWidget | None, title: str, text: str, ok: str, cancel: str | None) -> bool:
    """自绘的提示框：右边确认，左边取消（cancel=None 就只有一个按钮）。点确认返回 True，取消或关闭返回 False。"""
    dlg = _message(master, title, text)
    row = hbox()
    row.addStretch(1)
    if cancel is not None:
        row.addWidget(RButton(cancel, dlg.reject, "plain", width=84))
        row.addSpacing(10)
    else:
        dlg.on_return = dlg.accept  # 只有一个按钮时回车就是它；要确认的不绑回车，免得手快按掉
    row.addWidget(RButton(ok, dlg.accept, "primary", width=84))
    dlg.body.addLayout(row)
    center_on(dlg, master)
    return dlg.exec() == QDialog.Accepted


def ask_choice(master: QWidget | None, title: str, text: str, options: tuple[tuple[str, str], ...], cancel: str) -> int | None:
    """几个按钮的选择框：最左边取消，后面按 options 的 (文字, 样式) 排。返回点的是第几个（从 0 起）；取消或关闭返回 None。"""
    dlg = _message(master, title, text)
    row = hbox()
    row.addStretch(1)
    row.addWidget(RButton(cancel, dlg.reject, "plain", width=72))
    for i, (text, kind) in enumerate(options):
        row.addSpacing(10)
        row.addWidget(RButton(text, lambda i=i: dlg.done(CHOICE_BASE + i), kind, width=max(84, measure(font(10), text) + 28)))
    dlg.body.addLayout(row)
    center_on(dlg, master)
    code = dlg.exec()
    return code - CHOICE_BASE if code >= CHOICE_BASE else None


CHOICE_BASE = 10  # ask_choice 的返回码从这里起（0 / 1 是 Qt 自己的取消 / 确认）


def ask_confirm(master: QWidget | None, title: str, text: str, ok: str, cancel: str) -> bool:
    """两个按钮的确认框：左边取消，右边确认。点确认返回 True，取消或关闭返回 False。"""
    return _ask(master, title, text, ok, cancel)


def notice(master: QWidget | None, title: str, text: str, ok: str = "好") -> None:
    """只有一个按钮的提示框（提示、警告、出错都用它）。"""
    _ask(master, title, text, ok, None)
