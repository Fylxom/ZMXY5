"""主线程轮询：工作线程放进 events 队列的事件按 `_on_<事件名>` 分发，顺带看定时和线上版本。"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .. import core, results, servertime, timerlog
from ..tasks import last_rewards, uid_lookup
from ..widgets import STATUS_LABEL, notice
from .rules import TITLE, Job, shown_name


class EventsMixin:
    def _pump(self) -> None:  # 主线程轮询：界面只能在主线程里动
        try:
            while True:
                kind, data = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(data)
        except queue.Empty:
            pass
        if time.monotonic() - self._timer_checked >= 5:  # 定时：几秒看一眼有没有到点的
            self._timer_checked = time.monotonic()
            self._sync_schedule()
            self._tick_timers()
        if not self.background and time.monotonic() - self._version_checked >= 60:
            self._version_checked = time.monotonic()
            self._check_version()
        if self._busy_sig != self._busy_now():  # 换了账号、执行开始/结束、护送开始等船：按钮跟着变
            self._refresh_busy()
        if self.background and not self.jobs and not self.timer_queue and not self._schedule_busy and not self._global_checking:
            QApplication.instance().quit()  # 后台这轮做完即退出，下次到点再由 Windows 唤起

    def _check_version(self) -> None:
        """线上版本的缓存过期了（一小时）就在后台重读一次；线上比本地新、那一版是第几个养成周还没读过，也去读一次。读完 ``version_changed``。"""
        from .. import online
        ahead = online.newer_online()
        if self._version_reading or (online.fresh() and (not ahead or ahead == self._week_tried)):
            return
        self._version_reading, self._week_tried = True, ahead

        def work() -> None:
            try:
                online.growth_week(int(servertime.now()))  # 读线上版本；和本地不是同一版时顺带把那一版的养成周读出来
            finally:
                self.events.put(("version", None))
        threading.Thread(target=work, name="version-check", daemon=True).start()

    def _on_version(self, _data: Any) -> None:
        self._version_reading = False
        self.version_changed()

    def version_changed(self) -> None:
        """线上版本刚读过（启动时、之后每小时）：靠它的显示重画一遍；游戏已经更新、程序还在用旧表就提醒重启（每个版本只提醒一次）。"""
        from .. import online, tablesync
        self.refresh_reserve()
        self.refresh_boat()
        ahead = online.newer_online()
        if ahead and ahead != self._outdated:
            self._outdated = ahead
            self._add_log("—", "数据更新", core.TODO, f"游戏已更新到 {tablesync.day_of(ahead)} 的版本，程序还在用旧的数据表：重启程序后更新")
            self.setWindowTitle(f"{TITLE} —— 游戏已更新，请重启程序")

    def _on_note(self, text: str) -> None:  # 窗口出来以后才有的数据更新消息（后台下载、新旧表对比）
        self._add_log("", "数据更新", "info", text)

    def _on_result(self, data: tuple[str, core.TaskResult]) -> None:
        name, r = data
        title = self._title(r.task)
        target = self.targets.get(name)
        timed = self._record_timed(name, r, title)
        if (r.task == uid_lookup.ID and r.status == core.SUBMITTED and r.popup
                and not timed and not self.background and self.isVisible()):
            self._show_uid_popup(r.popup)
        if r.log:
            self.table.put(shown_name(name), title, r.status, r.detail or "")
            if not timed and r.status != core.PROGRESS and not self.background:  # 手动执行的最终结果也落盘（定时的已记在定时记录里）
                timerlog.write(shown_name(name), title, STATUS_LABEL.get(r.status, r.status), r.detail or "", file=timerlog.run_path())
        if r.todo and r.status != core.PROGRESS:  # 要用户自己处理的事：攒着，这次执行“结束”前再提醒（同一任务后来的结果盖掉先前的）
            self.todos[(name, r.task)] = (title, r.todo)
        if target is not None and results.save_result(self.store, target, r, self.specs):
            self.store.save()
            if r.profile:
                self._after_profile(r.profile)
        if r.task == last_rewards.ID and r.status != core.PROGRESS and target is not None:
            self.refresh_reward()

    def _record_timed(self, name: str, r: core.TaskResult, title: str) -> bool:
        """定时开始（或插进别的执行）的结果另外记进定时记录。返回这条是不是定时的结果。"""
        timed = name in self.timed_names or (name, r.task) in self.timed_inserted
        if name in self.timed_names and r.status != core.PROGRESS:
            self._timed_failed(name, r)
        if timed and r.log:  # 仅补充资料的结果不重复写入定时日志
            timerlog.write(shown_name(name), title, STATUS_LABEL.get(r.status, r.status), r.detail or "")
            if r.status != core.PROGRESS:
                self.timed_inserted.discard((name, r.task))
        return timed

    def _after_profile(self, profile: dict[str, Any]) -> None:
        """任务读到的信息存好以后刷新界面：摘要文字；活动做完了或档期变了，活动页重排。"""
        self.refresh_summaries()
        over = any(profile.get(key) for key in results.SAVE_END)
        if over or self._ended_ids() != self._limited_sig:  # 云梯走到终点：活动页换分块、开关重新读
            if self._ended_ids() != self._limited_sig:
                self._build_limited()
            self.on_select()

    def _on_cookie(self, data: tuple[dict[str, Any], str]) -> None:
        acc, cookie = data
        self.store.set_secret(acc, "cookie", cookie)
        self.store.save()

    def _on_captcha(self, data: tuple[bytes, dict[str, Any]]) -> None:
        QTimer.singleShot(0, lambda: self._show_captcha(*data))

    def _on_paused(self, data: dict[str, list[str]]) -> None:  # 断点按存档各记各的；后面的存档也没跑，记全部任务
        for name, left in data.items():
            target = self.targets.get(name)
            if target is not None:
                self.store.add_resume(*target, left)
        self.store.save()
        if not self.background:
            notice(self, "任务已暂停", "有账号已停止（背包已满，或被顶号 / 会话断开）。\n处理好后（清理背包 / 确认账号能正常登录），点右上角「继续」从断点接着执行。")

    def _on_fatal(self, data: str) -> None:
        self._add_log("—", "系统", "error", f"运行异常：{data}")
        if self.timed_names:  # 分不清是哪次执行出的：有定时执行在跑就也记一条
            timerlog.write("—", "系统", STATUS_LABEL["error"], f"运行异常：{data}")

    def _on_jobdone(self, job: Job) -> None:
        if job in self.jobs:
            self.jobs.remove(job)
        if job.stop is not None:
            self._flush_todos(job)
            self._add_log("—", "系统", "info", "结束")
        if job.timed:
            self.timed_names -= job.timed
            for name in job.timed:
                self.timed_items.pop(name, None)
            timerlog.write("、".join(sorted(shown_name(n) for n in job.timed)), "定时", STATUS_LABEL["info"], "执行结束")
        self._refresh_busy()

    def _flush_todos(self, job: Job) -> None:
        """这次执行占着的账号攒下的待处理。"""
        for key in [k for k in self.todos if (t := self.targets.get(k[0])) is None or t[0].get("username", "") in job.users]:
            title, text = self.todos.pop(key)
            name = shown_name(key[0])
            self._add_log(name, title, core.TODO, text)
            if key[0] in job.timed:
                timerlog.write(name, title, STATUS_LABEL[core.TODO], text)
