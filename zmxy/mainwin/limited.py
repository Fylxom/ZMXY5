"""活动页：“识别”读服务器时间，把档期已过的活动单放在“已结束”里（全局的，与账号/存档无关）。

数据表换了新版以后，第一次执行任务时有存档进角成功，就用那次登录拿到的服务器时间顺带识别一次（不另外登录），
识别结果记下当时的数据表版本（``tables``），同一版表不再自动识别。
"""

from __future__ import annotations

import time
from typing import Any

from .. import core, data as tables, protocol as p, runner
from ..tasks import chapter_role, limited, login_gift
from ..widgets import SUB, RButton, clear_layout, hbox, hint, label, notice, set_fg
from .rules import ALWAYS_LISTED, SAVE_END
from .running import read_error


def known_ids(value: Any) -> set[str]:
    """设置文件里的任务 id 列表：形状不对的当没有。"""
    return {tid for tid in value if isinstance(tid, str)} if isinstance(value, (list, tuple, set)) else set()


def ended_ids(specs: list[core.TaskSpec], now: int) -> list[str]:
    """按数据表里的档期，服务器时间 now 时已经结束的活动。"""
    return [sp.id for sp in specs if sp.window is not None and sp.id not in ALWAYS_LISTED and not sp.window(now)]


class LimitedMixin:
    def _ended_ids(self) -> set[str]:
        """已结束的限时任务：识别出的（全局，档期已过）+ 当前存档自己做完的（云梯、玄女战记、登录有礼；按存档）。"""
        saved = self.store.data.get("limited")
        ended = {t for t in saved.get("ended", ()) if t in self.specs and t not in ALWAYS_LISTED} if isinstance(saved, dict) else set()
        acc = self._acc()
        profile = self.store.save_view(acc, self.slot).get("profile", {}) if acc else {}
        ended.update(tid for key, tid in SAVE_END.items() if profile.get(key) and tid in self.specs)
        return ended

    def _build_limited(self) -> None:
        page = self.page_lays["limited"]
        clear_layout(page)
        head = hbox(margins=(2, 0, 2, 6))
        self.exec_btns["limited:read"] = RButton("识别结束活动", self.limited_read, "side", height=22, size=9, width=88)
        head.addWidget(self.exec_btns["limited:read"])
        head.addSpacing(8)
        head.addWidget(hint("按服务器时间和数据表档期，把过期的活动移到“已结束”并关掉开关（所有账号共用），重新开放的打开。\n"
                            "数据表换新版后，第一次执行有存档进角时顺带识别一次。\n"
                            "云梯、玄女战记、登录有礼按存档自己做没做完算，只关那个存档的开关。"))
        head.addSpacing(10)
        saved = self.store.data.get("limited")
        at = saved.get("at") if isinstance(saved, dict) else None
        self.limited_status = label(time.strftime("识别于 %m-%d %H:%M", time.localtime(at)) if at else "还没识别", 9, SUB)
        head.addWidget(self.limited_status, 1)
        page.addLayout(head)
        ended = self._limited_sig = self._ended_ids()
        self._build_task_page(page, self.limited_specs, group_of=lambda sp: "已结束" if sp.id in ended else "活动")
        page.addStretch(1)
        for tid in ended:
            if tid in self.task_labels:
                set_fg(self.task_labels[tid], SUB)
        if self._busy_sig is not None:
            self._refresh_busy()  # 重建出来的按钮按当前账号的状态置灰
        runner.on_clock = lambda now: self.events.put(("clock", now))  # 执行任务进角成功：看要不要顺带识别

    def _tables_stale(self) -> bool:
        """数据表换过版本、还没按新表识别过。"""
        saved = self.store.data.get("limited")
        return not isinstance(saved, dict) or saved.get("tables") != tables.TABLE_ROOT.name

    def _on_clock(self, now: int) -> None:  # 执行任务时某个存档进角成功（工作线程发来的服务器时间）
        if not self._tables_stale() or getattr(self, "_clock_failed", None) == tables.TABLE_ROOT.name:
            return
        try:
            ended = ended_ids(self.limited_specs, now)
        except Exception as exc:  # 新表读不出档期：这一版不再自动试，手动“识别结束活动”会报具体原因
            self._clock_failed = tables.TABLE_ROOT.name
            self._add_log("—", "系统", "info", f"数据表已更新，顺带识别结束活动失败：{read_error(exc)}")
            return
        saved = self.store.data.get("limited")
        before = known_ids(saved.get("ended", ())) if isinstance(saved, dict) else set()
        before &= {sp.id for sp in self.limited_specs}
        titles = lambda ids: "、".join(sp.title for sp in self.limited_specs if sp.id in ids)  # noqa: E731
        changes = [f"{titles(set(ended) - before)} 已结束"] if set(ended) - before else []
        changes += [f"{titles(before - set(ended))} 重新开放"] if before - set(ended) else []
        self._add_log("—", "系统", "info", "数据表已更新，顺带识别了结束活动：" + ("；".join(changes) or "没有变化"))
        self._on_limited({"at": now, "ended": ended})

    def limited_read(self) -> None:
        """“识别结束活动”：登录读服务器时间，按活动档期判断哪些限时任务已经结束。
        当前存档不存在时依次换本账号的其他存档、其他账号（只要能拿到服务器时间就行，不执行任何任务）。"""
        if self._busy() or not self._require_account():
            return
        acc = self._acc()
        tries = [(acc, self.slot)]
        tries += [(acc, n) for n in range(1, 9) if n != self.slot]
        tries += [(a, self.acc_slots.get(self._acc_key(a), 1)) for i, a in enumerate(self.store.accounts)
                  if i != self.current and not self._busy(a.get("username", ""))]  # 正在执行的账号不能再登录一次
        try:
            first = self.store.to_account(acc, self._cookie_saver(acc), self.slot)
        except Exception as exc:
            notice(self, "账号配置错误", str(exc))
            return
        self.limited_status.setText("正在登录并识别…")
        specs = list(self.limited_specs)

        def work() -> None:
            try:
                self._recognize(tries, first, specs)
            except Exception as exc:
                self.events.put(("limited", read_error(exc)))

        self._spawn(work, {a.get("username", "") for a, _n in tries})

    def _recognize(self, tries: list[tuple[dict[str, Any], int]], first: Any, specs: list[core.TaskSpec]) -> None:
        """工作线程：依次试，读到服务器时间就发出结果。"""
        last: Exception | None = None
        for i, (a, n) in enumerate(tries):
            try:
                info, now = core.read_clock(first if i == 0 else self.store.to_account(a, self._cookie_saver(a), n))
            except p.AllianceError as exc:
                last = exc
                if isinstance(exc, p.ConfigError) and "没有存档" in str(exc):  # login.choose_record：换下一个存档/账号再试；其他错误（密码、网络）直接报
                    continue
                raise
            if now is None:
                raise RuntimeError("服务器没有返回时间")
            ended = ended_ids(specs, now)
            self.events.put(("save_end", (a, n, {
                limited.CLOUD_END: limited.cloud_end(info),
                chapter_role.END: chapter_role.chapter_end(info),
                login_gift.END: login_gift.login_gift_end(info, now),
            })))  # 读到哪个存档就记哪个存档的进度
            self.events.put(("limited", {"at": now, "ended": ended}))
            return
        raise last or RuntimeError("没有可用的存档")

    def _on_limited(self, data: Any) -> None:  # 活动页“识别”的结果：dict = {at, ended}，str = 错误说明
        if isinstance(data, dict):
            previous = self.store.data.get("limited")
            old_ended = known_ids(previous.get("ended", ())) if isinstance(previous, dict) else set()
            active_ids = {sp.id for sp in self.limited_specs} - set(ALWAYS_LISTED)
            returned = (old_ended - known_ids(data.get("ended", ()))) & active_ids
            self.store.data["limited"] = {**data, "tables": tables.TABLE_ROOT.name}  # 按哪一版数据表识别的
            self.store.switch_on(returned)  # 上次识别已结束、本次重新开放：恢复活动开关
            self.store.switch_off(data.get("ended", ()))  # 档期已过的：开着的关掉（只在识别这一下，之后可以手动再打开）
            self.store.save()
            self._build_limited()
            self.on_select()  # 重建后的开关/模式/摘要要重新读存档
        else:
            self.limited_status.setText(f"识别失败：{data}")

    def _on_save_end(self, data: tuple) -> None:  # 识别时读到的存档自己做完了哪些；None = 读不到，不改
        acc, slot, flags = data
        for key, flag in flags.items():
            if flag is not None:
                profile = self.store.save_cfg(acc, slot).setdefault("profile", {})
                was_ended = profile.get(key) is True
                profile[key] = flag
                if flag:
                    self.store.switch_off([SAVE_END[key]], (acc, slot))  # 只关这个存档的开关，不锁定
                elif was_ended:
                    self.store.switch_on([SAVE_END[key]], (acc, slot))  # 活动返场时只重开这个存档
        self.store.save()
