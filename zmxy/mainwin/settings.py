"""设置（账号栏右上角“设置”）：程序更新（版本、启动时自动检查、检查更新）、打开数据文件夹。
更新的检查 / 下载在后台线程里做，结果放进 events 队列（update_check / update_progress / update_ready）。规则见 appupdate。"""

from __future__ import annotations

import os
import threading
from typing import Any

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from .. import appupdate
from ..store import settings_path
from ..widgets import SUB, Card, Dialog, RButton, Switch, ask_choice, ask_confirm, elide_label, hbox, label, notice


class SettingsDialog(Dialog):
    def __init__(self, app: Any) -> None:
        super().__init__(app, "设置", (24, 18, 24, 18))
        self.app = app
        cfg = app._update_cfg()
        self.body.addWidget(label("程序更新", 9, SUB))
        self.body.addSpacing(4)
        card = Card(padx=14, pady=8)
        col = card.box()
        version = appupdate.current_version()
        row = hbox(spacing=8, margins=(0, 4, 0, 4))
        row.addWidget(label("当前版本"))
        row.addStretch(1)
        row.addWidget(label(version or "源码运行（不检查更新）", 10, SUB))
        col.addLayout(row)
        row = hbox(spacing=8, margins=(0, 4, 0, 4))
        row.addWidget(label("启动时自动检查更新"))
        row.addStretch(1)
        self.auto = Switch(cfg.get("auto", True) is not False, self._toggle_auto)
        row.addWidget(self.auto)
        col.addLayout(row)
        row = hbox(spacing=8, margins=(0, 4, 0, 4))
        self.status = label(self._idle_text(), 9, SUB)
        self.status.setFixedWidth(260)
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.check_btn = RButton("检查更新", self._check, "side", height=26, size=9, width=80)
        self.check_btn.set_enabled(appupdate.enabled())
        row.addWidget(self.check_btn)
        col.addLayout(row)
        self.body.addWidget(card)
        self.body.addSpacing(14)
        self.body.addWidget(label("数据", 9, SUB))
        self.body.addSpacing(4)
        card = Card(padx=14, pady=8)
        col = card.box()
        row = hbox(spacing=8, margins=(0, 4, 0, 4))
        text = QVBoxLayout()
        text.setSpacing(2)
        info = label("账号设置、日志、定时记录都在这个文件夹，备份时整个复制即可", 9, SUB)
        info.setWordWrap(True)
        info.setFixedWidth(260)
        text.addWidget(info)
        path = elide_label(str(settings_path().parent), 9, SUB)
        path.setFixedWidth(260)
        text.addWidget(path)
        row.addLayout(text, 1)
        row.addWidget(RButton("打开", self._open_folder, "side", height=26, size=9, width=80))
        col.addLayout(row)
        self.body.addWidget(card)
        self.buttons("关闭", self.accept, cancel=None)
        self.on_return = self.accept

    @staticmethod
    def _idle_text() -> str:
        if appupdate.current_version() is None:
            return "源码运行时不检查更新，打包后的程序才会检查"
        if not appupdate.REPO:
            return "这个版本还没有设置更新地址"
        return "从 GitHub 检查有没有新版本"

    def show_status(self, text: str) -> None:
        self.status.setText(text)

    def _toggle_auto(self) -> None:
        self.app._update_cfg()["auto"] = self.auto.get()
        self.app.store.save()

    def _check(self) -> None:
        self.show_status("正在检查…")
        self.app.check_update(manual=True)

    @staticmethod
    def _open_folder() -> None:
        folder = settings_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))  # noqa: S606 - 打开资源管理器


class SettingsMixin:
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

        def work() -> None:
            try:
                result: Any = appupdate.check()
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
        self._update_status(f"发现新版本 {result.tag}")
        if not manual and self._update_cfg().get("skip") == result.tag:
            return  # 用户说过跳过这个版本
        self._offer_update(result, manual)

    def _offer_update(self, release: appupdate.Release, manual: bool) -> None:
        notes = release.notes[:600] + ("…" if len(release.notes) > 600 else "") if release.notes else "（没有写更新说明）"
        text = f"发现新版本 {release.tag}（现在是 {appupdate.current_version()}）\n\n{notes}"
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
        self._update_status(f"正在下载 {release.tag}…")
        self.pill_note(f"正在下载新版本 {release.tag}")

        def progress(done: int, total: int) -> None:
            self.events.put(("update_progress", (done, total)))

        def work() -> None:
            try:
                result: Any = appupdate.download(release, progress)
            except appupdate.UpdateError as exc:
                result = str(exc)
            except Exception as exc:
                result = f"下载失败：{type(exc).__name__}"
            self.events.put(("update_ready", (release, result)))
        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _on_update_progress(self, data: tuple[int, int]) -> None:
        done, total = data
        text = f"正在下载 {done / 1048576:.1f}" + (f" / {total / 1048576:.1f} MB" if total else " MB")
        self._update_status(text)
        self.pill_note(text)

    def _on_update_ready(self, data: tuple[appupdate.Release, Any]) -> None:
        release, result = data
        self._update_busy = False
        self.pill_note("")
        if isinstance(result, str):
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
