"""设置（账号栏右上角“设置”）：顶部检查更新和下载进度；“更新”自动检查、下载线路和测速；“关于”发布仓库、数据文件夹。
更新的检查 / 下载在后台线程里做，结果放进 events 队列（update_check / update_progress / update_ready）。规则见 appupdate。"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QVBoxLayout, QWidget

from .. import appupdate
from ..store import settings_path
from ..widgets import (RED, SUB, TEXT, Card, Dialog, LinkRow, ProgressBar, RButton, Segmented, Switch, TextButton, ask_choice,
                       ask_confirm, divider, hbox, hint, label, notice, set_fg, tip, vbox)
from .rules import TITLE


WIDTH = 440  # 设置页内容宽度：固定，长说明在里面换行，不把窗口撑宽


class SettingsDialog(Dialog):
    """三块：顶部（程序名、版本、检查更新、下载进度）、更新（自动检查、下载线路和测速）、关于（发布仓库、数据文件夹）。
    每块一张卡片，一行一项、细线隔开；长说明收进“说明”浮层，不占版面。"""

    def __init__(self, app: Any) -> None:
        super().__init__(app, "设置", (22, 20, 22, 18))
        self.app = app
        self.probing: set[str] = set()
        self.body.addWidget(self._hero())
        self.body.addSpacing(16)
        self.body.addWidget(self._section("更新", [self._auto_row(), self._preview_row(), self._mirror_row()]))
        self.body.addSpacing(16)
        repo = LinkRow("GitHub 发布仓库", appupdate.page_url().removeprefix("https://"),
                       lambda: QDesktopServices.openUrl(QUrl(appupdate.page_url())))
        tip(repo, "在浏览器里打开：新版本、更新说明、部分源码都在这里")
        folder = LinkRow("数据文件夹", "打开", self._open_folder)
        tip(folder, f"账号设置、日志、定时记录都在这里，备份时整个复制即可\n{settings_path().parent}")
        self.body.addWidget(self._section("关于", [repo, folder], pad=4))
        self.buttons("关闭", self.accept, cancel=None)
        self.on_return = self.accept
        if app._update_cancel is not None:  # 打开设置时正在下载（启动时自动检查后点了“立即更新”）
            self.show_downloading(True)

    # ---- 搭界面
    def _hero(self) -> QWidget:
        """顶部：程序名 + 版本，右边“检查更新”；下面是下载进度条和状态（有内容才显示）。"""
        card = Card(padx=18, pady=16)
        card.setFixedWidth(WIDTH)
        col = card.box()
        top = hbox(spacing=12)
        names = QVBoxLayout()
        names.setSpacing(3)
        names.addWidget(label(TITLE, 13, TEXT, True))
        version = appupdate.current_version()
        names.addWidget(label(f"版本 {version}" if version else "源码运行 · 不检查更新", 9, SUB))
        top.addLayout(names, 1)
        self.check_btn = RButton("检查更新", self._check, "primary", height=32, size=10, width=96)
        self.check_btn.set_enabled(appupdate.enabled())
        top.addWidget(self.check_btn, 0, Qt.AlignVCenter)
        col.addLayout(top)
        self.bar_box = QWidget()  # 进度条连同上面的间距一起藏：不下载时顶部不留空
        bar_lay = vbox(self.bar_box, margins=(0, 12, 0, 0))
        self.bar = ProgressBar()
        bar_lay.addWidget(self.bar)
        self.bar_box.setVisible(False)
        col.addWidget(self.bar_box)
        self.status = label("", 9, SUB)  # 检查 / 下载更新的进度和结果；空 = 不显示
        self.status.setWordWrap(True)
        self.status.setContentsMargins(0, 8, 0, 0)
        self.status.setVisible(False)
        col.addWidget(self.status)
        return card

    @staticmethod
    def _section(title: str, rows: list[QWidget], pad: int = 0) -> QWidget:
        """一块：小标题 + 卡片，卡片里各行之间一条细线。"""
        box = QWidget()
        col = vbox(box, spacing=0)
        col.addWidget(label(title, 9, SUB), 0)
        col.addSpacing(6)
        card = Card(padx=4 + pad, pady=4 + pad)
        card.setFixedWidth(WIDTH)
        inner = card.box()
        for i, row in enumerate(rows):
            if i:
                inner.addSpacing(pad)
                inner.addWidget(divider())
                inner.addSpacing(pad)
            inner.addWidget(row)
        col.addWidget(card)
        return box

    def _auto_row(self) -> QWidget:
        row = QWidget()
        lay = hbox(row, spacing=8, margins=(10, 10, 10, 10))
        lay.addWidget(label("启动时自动检查更新"))
        lay.addStretch(1)
        self.auto = Switch(self.app._update_cfg().get("auto", True) is not False, self._toggle_auto)
        lay.addWidget(self.auto)
        return row

    def _preview_row(self) -> QWidget:
        row = QWidget()
        lay = hbox(row, spacing=6, margins=(10, 10, 10, 10))
        lay.addWidget(label("接收预览版"))
        lay.addWidget(hint("打开后，检查更新时预览版也算：新功能先拿到，但可能还有问题。\n关着只收正式版。"))
        lay.addStretch(1)
        self.preview = Switch(self.app._update_cfg().get("preview") is True, self._toggle_preview)
        lay.addWidget(self.preview)
        return row

    def _toggle_preview(self) -> None:
        self.app._update_cfg()["preview"] = self.preview.get()
        self.app.store.save()

    def _mirror_row(self) -> QWidget:
        """下载线路：右边是分段选择，左边一个“测速”；测完每段正下方写这条线路的速度。"""
        row = QWidget()
        grid = QGridLayout(row)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        name = hbox(spacing=6)
        name.addWidget(label("下载线路"))
        name.addWidget(hint("国内直连 GitHub 很慢或连不上时选镜像。\n“测速”会经每条线路下载新版程序的开头 1 MB，看哪条快；"
                            "还没有发布版本时只能看通不通。"))
        name.addStretch(1)
        grid.addLayout(name, 0, 0, Qt.AlignVCenter)
        grid.setColumnStretch(0, 1)
        self.probe_btn = TextButton("测速", self._probe)
        self.probe_btn.set_enabled(bool(appupdate.REPO))
        grid.addWidget(self.probe_btn, 0, 1, Qt.AlignVCenter)
        self.mirror = Segmented([(n, key) for key, (n, _) in appupdate.MIRRORS.items()], self.app._update_mirror(),
                                self._pick_mirror, seg_width=62, height=28, size=9)
        grid.addWidget(self.mirror, 0, 2)
        self.speeds = QWidget()  # 测之前不显示
        under = hbox(self.speeds, spacing=0, margins=(self.mirror.pad, 0, self.mirror.pad, 0))
        self.probe_lbls: dict[str, QLabel] = {}
        for key in appupdate.MIRRORS:
            lbl = self.probe_lbls[key] = label("", 8, SUB)
            lbl.setFixedWidth(self.mirror.seg_w)
            lbl.setAlignment(Qt.AlignCenter)
            under.addWidget(lbl)
        self.speeds.setVisible(False)
        grid.addWidget(self.speeds, 1, 2)
        return row

    # ---- 状态
    def show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def show_progress(self, frac: float | None) -> None:
        self.bar.set(frac)

    def show_downloading(self, on: bool) -> None:
        """下载期间：“检查更新”变成“取消下载”，出现进度条。"""
        self.check_btn.set_text("取消下载" if on else "检查更新", "stop" if on else "primary")
        self.bar_box.setVisible(on)
        if on:
            self.bar.set(0)

    def _toggle_auto(self) -> None:
        self.app._update_cfg()["auto"] = self.auto.get()
        self.app.store.save()

    def _pick_mirror(self) -> None:
        self.app._update_cfg()["mirror"] = self.mirror.get()
        self.app.store.save()

    def _probe(self) -> None:
        """三条线路同时测（各自一个后台线程），结果陆续回来，见 show_probe。"""
        self.probing = set(appupdate.MIRRORS)
        self.probe_btn.set_enabled(False)
        self.probe_btn.set_text("测速中…")
        for lbl in self.probe_lbls.values():
            lbl.setText("…")
            set_fg(lbl, SUB)
        self.speeds.setVisible(True)
        for key in appupdate.MIRRORS:
            threading.Thread(target=lambda k=key: self.app.events.put(("update_probe", (k, appupdate.probe(k)))),
                             name=f"probe-{key}", daemon=True).start()

    def show_probe(self, key: str, result: appupdate.Probe) -> None:
        """测到速度写速度（慢的红字）；还没有发布（没文件可测）写回应用用时；不通写“不通”（红字）。"""
        if not result.ok:
            text, bad = "不通", True
        elif result.speed is None:
            text, bad = f"{result.seconds:.1f} 秒", False
        else:
            text, bad = appupdate.speed_text(result.speed), result.speed < appupdate.SLOW
        self.probe_lbls[key].setText(text)
        set_fg(self.probe_lbls[key], RED if bad else SUB)
        self.speeds.setVisible(True)
        self.probing.discard(key)
        if not self.probing:
            self.probe_btn.set_text("测速")
            self.probe_btn.set_enabled(True)

    def _check(self) -> None:
        if self.app._update_cancel is not None:  # 正在下载：这个按钮是“取消下载”
            self.app.cancel_update()
            return
        self.show_status("正在检查…")
        self.app.check_update(manual=True)

    @staticmethod
    def _open_folder() -> None:
        folder = settings_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))  # noqa: S606 - 打开资源管理器


SPEED_AFTER = 3  # 下载开始几秒后才显示速度（刚开始的几块不准）


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} 分钟" if minutes < 60 else f"{minutes // 60} 小时" + (f" {minutes % 60} 分" if minutes % 60 else "")


class SettingsMixin:
    _update_cancel: threading.Event | None = None  # 正在下载更新时的取消信号；没在下载 = None
    _update_started = 0.0  # 这次下载开始的时刻（time.monotonic），算速度用

    def open_settings(self) -> None:
        self.settings_dlg = SettingsDialog(self)
        try:
            self.settings_dlg.run()
        finally:
            self.settings_dlg = None

    def _update_cfg(self) -> dict[str, Any]:
        cfg = self.store.data.get("update")
        if not isinstance(cfg, dict):
            cfg = self.store.data["update"] = {}
        return cfg

    def _update_mirror(self) -> str:
        """下载线路（appupdate.MIRRORS 的键）；没存过或镜像已经删掉 = 直连。"""
        mirror = self._update_cfg().get("mirror")
        return mirror if mirror in appupdate.MIRRORS else appupdate.DIRECT

    def _on_update_probe(self, data: tuple[str, appupdate.Probe]) -> None:
        dlg = getattr(self, "settings_dlg", None)
        if dlg is not None:  # 测的时候关掉了设置：结果不用了
            dlg.show_probe(*data)

    def _update_status(self, text: str) -> None:
        dlg = getattr(self, "settings_dlg", None)
        if dlg is not None:
            dlg.show_status(text)

    # ---- 检查
    def startup_update_check(self) -> None:
        """启动后：清掉上次更新留下的旧程序；开着“自动检查”就在后台看一次。"""
        appupdate.cleanup()
        if appupdate.enabled() and self._update_cfg().get("auto", True) is not False:
            self.check_update(manual=False)

    def check_update(self, manual: bool) -> None:
        if self._update_busy:
            return
        self._update_busy = True
        mirror = self._update_mirror()
        preview = self._update_cfg().get("preview") is True

        def work() -> None:
            try:
                result: Any = appupdate.check(mirror, preview)
            except appupdate.UpdateError as exc:
                result = str(exc)
            except Exception as exc:  # 检查更新出错不能影响别的
                result = f"检查失败：{type(exc).__name__}"
            self.events.put(("update_check", (manual, result)))
        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _on_update_check(self, data: tuple[bool, Any]) -> None:
        manual, result = data
        self._update_busy = False
        if isinstance(result, str):
            self._update_status(result)
            if manual and getattr(self, "settings_dlg", None) is None:
                notice(self, "检查更新", result)
            return  # 自动检查失败不打扰
        if result is None:
            self._update_status(f"已经是最新版本（{appupdate.current_version()}）")
            return
        self._update_status(f"发现新版本 {result.tag}" + ("（预览版）" if result.preview else ""))
        if not manual and self._update_cfg().get("skip") == result.tag:
            return  # 用户说过跳过这个版本
        self._offer_update(result, manual)

    def _offer_update(self, release: appupdate.Release, manual: bool) -> None:
        notes = release.notes[:600] + ("…" if len(release.notes) > 600 else "") if release.notes else "（没有写更新说明）"
        kind = "预览版" if release.preview else "新版本"
        text = f"发现{kind} {release.tag}（现在是 {appupdate.current_version()}）\n\n{notes}"
        options = (("立即更新", "primary"),) if manual else (("跳过这个版本", "plain"), ("立即更新", "primary"))
        picked = ask_choice(self.settings_dlg if getattr(self, "settings_dlg", None) else self, "发现新版本", text, options, "以后再说")
        if picked is None:
            return
        if options[picked][0] == "跳过这个版本":
            self._update_cfg()["skip"] = release.tag
            self.store.save()
            return
        self._download_update(release)

    # ---- 下载、替换
    def _download_update(self, release: appupdate.Release) -> None:
        if self.jobs or self.timer_queue or self.timed_names:
            notice(self, "等一下再更新", "有任务正在执行，等它结束后再点“检查更新”。")
            return
        if self._update_busy:
            return
        self._update_busy = True
        mirror = self._update_mirror()
        cancel = self._update_cancel = threading.Event()
        self._update_started = time.monotonic()
        self._update_status(f"正在下载 {release.tag}…")
        self.pill_note(f"正在下载新版本 {release.tag}")
        dlg = getattr(self, "settings_dlg", None)
        if dlg is not None:
            dlg.show_downloading(True)

        def progress(done: int, total: int) -> None:
            self.events.put(("update_progress", (done, total)))

        def work() -> None:
            try:
                result: Any = appupdate.download(release, progress, mirror, cancel)
            except appupdate.UpdateError as exc:
                result = str(exc)
            except Exception as exc:
                result = f"下载失败：{type(exc).__name__}"
            self.events.put(("update_ready", (release, result)))
        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _on_update_progress(self, data: tuple[int, int]) -> None:
        done, total = data
        parts = [f"正在下载 {done / 1048576:.1f}" + (f" / {total / 1048576:.1f} MB" if total else " MB")]
        spent = time.monotonic() - self._update_started
        slow = False
        if spent >= SPEED_AFTER and done:
            speed = done / spent  # 从开始到现在的平均速度
            parts.append(appupdate.speed_text(speed))
            if total > done:
                parts.append(f"约还要 {_duration((total - done) / speed)}")
            slow = speed < appupdate.SLOW
        text = " · ".join(parts)
        self.pill_note(text)
        dlg = getattr(self, "settings_dlg", None)
        if dlg is not None:
            dlg.show_progress(done / total if total else None)
        self._update_status(text + ("；很慢，可以点“取消下载”，在“下载线路”换一条再试" if slow else ""))

    def cancel_update(self) -> None:
        """设置里“取消下载”：下载线程读到下一块时停下，删掉没下完的文件。"""
        cancel = getattr(self, "_update_cancel", None)
        if cancel is not None:
            cancel.set()
            self._update_status("正在取消…")

    def _on_update_ready(self, data: tuple[appupdate.Release, Any]) -> None:
        release, result = data
        self._update_busy = False
        cancelled = self._update_cancel is not None and self._update_cancel.is_set()
        self._update_cancel = None
        self.pill_note("")
        dlg = getattr(self, "settings_dlg", None)
        if dlg is not None:
            dlg.show_downloading(False)
        if isinstance(result, str):
            if cancelled:
                self._update_status("已取消下载；可以换一条下载线路再点“检查更新”")
                return
            self._update_status(result)
            notice(getattr(self, "settings_dlg", None) or self, "更新失败", result)
            return
        try:
            appupdate.install(result)
        except appupdate.UpdateError as exc:
            self._update_status(str(exc))
            notice(getattr(self, "settings_dlg", None) or self, "更新失败", str(exc))
            return
        self._update_status(f"{release.tag} 已下载好，重启程序后生效")
        if self.jobs or self.timer_queue or self.timed_names:  # 下载期间开始了执行：不打断，下次启动就是新版
            notice(getattr(self, "settings_dlg", None) or self, "更新好了", f"新版本 {release.tag} 已经放好，任务正在执行，下次打开程序就是新版本。")
            return
        if ask_confirm(getattr(self, "settings_dlg", None) or self, "更新好了",
                       f"新版本 {release.tag} 已经放好，重启程序后生效。现在重启吗？", "现在重启", "稍后"):
            appupdate.restart_after_exit()
            dlg = getattr(self, "settings_dlg", None)
            if dlg is not None:
                dlg.accept()
            QApplication.instance().setQuitOnLastWindowClosed(True)
            self.close()
            QApplication.instance().quit()

    def pill_note(self, text: str) -> None:
        """账号栏底下状态条旁边的小字：下载进度（没有就不显示）。"""
        lbl: QWidget | None = getattr(self, "update_note", None)
        if lbl is not None:
            lbl.setText(text)
            lbl.setVisible(bool(text))
