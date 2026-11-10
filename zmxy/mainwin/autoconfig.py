"""周常页顶部：自动配置、按养成周留次数、上届奖励的领取状态。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout

from .. import core
from ..tasks import last_rewards, time_boat
from ..widgets import SUB, TEXT, WARN, RButton, Switch, hbox, label, set_fg, tip
from .rules import weekly_auto_run_ids
from .running import read_error


def reserve_note(names: str, week: int, keep: int, live: Any) -> str:
    from .. import state
    if keep >= state.RESERVE_CAP and live is None:  # 表里这一周已经结束，线上换没换版本不知道（没读到 / 还没读）：先留着，不说“未更新”
        return f"无法确认线上版本，{names}先留着"
    if keep >= state.RESERVE_CAP:  # 到了开始那天但游戏还没更新：次数加上了也先别用
        return f"版本未更新，{names}请等待更新后使用"
    return f"{state.GROWTH_WEEKS[week - 1]}养成周快到了，今天要留{names}"


def short_reward(text: str) -> str:
    """上届奖励的摘要缩短到半行放得下（周常页和星斗幻境并排）：两项一样 → “都已领取”；不一样 → “竞技场 已领 · 斗宠 未领”。"""
    parts = [part.split(" ", 1) for part in text.split(" · ")]
    if len(parts) > 1 and all(len(p) == 2 for p in parts) and len({p[1] for p in parts}) == 1:
        return "都" + parts[0][1]
    for long, short in (("已领取", "已领"), ("未领取", "未领"), ("未读取", "未读")):
        text = text.replace(long, short)
    return text


class AutoConfigMixin:
    def _build_auto_bar(self, page: QVBoxLayout) -> None:
        bar = hbox(margins=(0, 0, 0, 8))
        self.exec_btns["weekly:auto"] = RButton("自动配置", self.auto_config, "side", height=24, size=9, width=70)
        bar.addWidget(self.exec_btns["weekly:auto"])
        bar.addSpacing(8)
        self.exec_btns["weekly:auto_run"] = RButton("自动配置并执行", self.auto_config_run, "side", height=24, size=9, width=106)
        bar.addWidget(self.exec_btns["weekly:auto_run"])
        tip(self.exec_btns["weekly:auto"], "登录当前存档，读各任务现有次数填进次数框；开着“按养成周留次数”先扣掉今天要留的。\n"
                                           "读不到的不改。时光之舟、上届奖励一起刷新。")
        tip(self.exec_btns["weekly:auto_run"], "自动配置后接着做：开着开关、次数大于 0 的任务，限时抢购、妖幡卷，没领的上届奖励。\n"
                                               "该选的没选的跳过。")
        bar.addSpacing(12)
        reserve = label("按养成周留次数", 9)
        tip(reserve, "养成周开始前 4 天起，每天多留 1 天的次数（上古蛮荒每天 2 次），开始那天正好攒满；"
                     "开始那天游戏还没更新就全留着。\n"
                     "上古蛮荒 → 装备宝石周，镇妖塔 → 幻兽法宝周，十绝阵、世界Boss → 翅膀光环周。\n"
                     "影响“自动配置”填的次数；要留时任务名变红。")
        bar.addWidget(reserve)
        bar.addSpacing(6)
        self.reserve_var = Switch(bool((self.store.data.get("ui") or {}).get("reserve", True)), self._toggle_reserve)
        bar.addWidget(self.reserve_var)
        bar.addSpacing(10)
        self.auto_status = label("", 9, SUB)
        self.auto_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bar.addWidget(self.auto_status, 1)
        page.addLayout(bar)

    def _toggle_reserve(self) -> None:
        self._save_ui()
        self.refresh_reserve()

    def refresh_reserve(self) -> None:
        """开关开着、且现在在某个养成周开始前的留次数期内，对应任务的名字变红（按服务器时钟看数据表里的养成周档期），开关旁边写说明。"""
        from .. import online, servertime, state
        now = servertime.now()
        live = online.known_growth_week(now)
        week, keep = (state.growth_reserve(now, live=live) if self.reserve_var.get() else None) or (None, 0)
        self.red_tasks = {t for t in self.reserve_ids if self.specs[t].reserve == week}
        for tid in self.reserve_ids:
            if tid != self.picked_task:
                set_fg(self.task_labels[tid], WARN if tid in self.red_tasks else TEXT)
        names = "、".join(f"{self.specs[t].title} {keep * self.specs[t].reserve_gain} 次" for t in self.reserve_ids if t in self.red_tasks)  # 每天涨得多的留得也多
        self.auto_status.setText(reserve_note(names, week, keep, live) if names else "")

    def auto_config(self) -> bool:
        """“自动配置”：登录当前存档，只读地查出每个周常任务现在拥有的最大次数，填进次数框。返回是否开始读了。"""
        target = self._read_target()
        if target is None:
            return False
        acc, slot, account = target
        account = replace(account, peers=self.store.peer_resolver(self._cookie_saver))  # 徒弟奖励次数要登录徒弟的账号读
        self.auto_status.setText("正在登录并读取各任务的剩余次数…")
        specs = [self.specs[t] for t in [*self.auto_ids, time_boat.ID]]  # 时光之舟看板也一起刷新
        reserve = self.reserve_var.get()

        def work() -> None:
            try:
                self.events.put(("auto", (acc, slot, core.read_auto(account, specs, reserve))))
            except Exception as exc:
                self.events.put(("auto", (acc, slot, read_error(exc))))

        self._spawn(work, {acc.get("username", "")})
        return True

    def _reward_state(self) -> dict:
        """当前存档的上届奖励状态 {text, pending}：自动配置读到的领取状态 + 本周执行过的结果（领到/无奖励）；都没有 = 空（周常奖励每周一换）。"""
        if self.current is None:
            return {}
        prof = self.store.save_view(self._acc(), self.slot).get("profile", {})
        week = last_rewards.week_key()
        read, out = prof.get(last_rewards.ID), prof.get(last_rewards.OUT_KEY)
        states = read.get("states") if isinstance(read, dict) and read.get("week") == week else None
        out = out if isinstance(out, dict) and out.get("week") == week else None
        if not states and not out:
            return {}
        text, pending = last_rewards.describe(states or {}, out)
        return {"text": text, "pending": pending}

    def refresh_reward(self) -> None:
        self.reward_lbl.setText(short_reward(self._reward_state().get("text") or "竞技场 未读取 · 斗宠 未读取"))

    def auto_config_run(self) -> None:
        """“自动配置并执行”：先自动配置，再执行次数大于 0 的周常、限时抢购和没领的上届奖励。"""
        self._run_after_auto = self.auto_config()  # 没选账号、账号正在执行等原因没有开始读 = False

    def _run_weekly_all(self, tries: int = 0) -> None:
        if self._busy():  # 读取线程刚结束，等它真正退出
            if tries < 25:
                QTimer.singleShot(200, lambda: self._run_weekly_all(tries + 1))
            return
        counts = {tid: field.get() for tid, field in self.count_vars.items()}
        # 读不到上届奖励状态也试着领；限时抢购没有次数框，由任务自身按库存、账号限购和余额判断。
        ids = weekly_auto_run_ids(self.weekly_run_ids, counts, self._reward_state().get("pending") is not False)
        acc = self._acc()
        ids = [t for t in ids if t in self.wide or t in self.weekly_vars and self.weekly_vars[t].get()]  # 只做打开的
        missing = [t for t in ids if self._unpicked(acc, self.slot, t)]  # 该选的没选：不做（规则 1）
        ids = [t for t in ids if t not in missing]
        if missing:
            note = self.auto_status.text()
            self.auto_status.setText((note + "；" if note else "") + f"没选，不做：{'、'.join(self.specs[t].title for t in missing)}")
        if not ids:
            note = self.auto_status.text() if missing else ""
            self.auto_status.setText((note + "；" if note else "") + "没有需要执行的周常任务（只做打开了开关的）")
            return
        self.start(tasks=ids, here=True)

    def _on_auto(self, data: tuple) -> None:  # “自动配置”的结果：dict = {任务 id: 次数/None/错误说明/状态}，str = 登录错误
        acc, slot, value = data
        go, self._run_after_auto = self._run_after_auto, False
        if not isinstance(value, dict):
            self.auto_status.setText(f"自动配置失败：{value}")
            return
        here = self.current is not None and self._acc() is acc and self.slot == slot
        skipped = self._store_auto(acc, slot, value, here)
        if skipped:  # 填了什么直接看次数框，这里只说没读到的
            note = self.auto_status.text()
            self.auto_status.setText((note + "；" if note else "") + f"没读到未改：{'、'.join(skipped)}")
        if go and here:
            QTimer.singleShot(100, self._run_weekly_all)

    def _on_auto_store(self, data: tuple) -> None:
        """全局“周常”定时读到的次数：和手动“自动配置”一样存进这个存档（正看着它就填进次数框），不另外执行。"""
        acc, slot, value = data
        here = self.current is not None and self._acc() is acc and self.slot == slot
        self._store_auto(acc, slot, value, here)

    def _store_auto(self, acc: dict, slot: int, value: dict, here: bool) -> list[str]:
        """自动配置的结果存进存档（正看着它就填进次数框）。返回没读到的任务名。"""
        skipped = []
        for tid, n in value.items():
            if isinstance(n, tuple):
                self._apply_auto_state(acc, slot, tid, n, here)
            elif not isinstance(n, int):
                skipped.append(self.specs[tid].title)
            elif here:
                self.count_vars[tid].set(str(n))  # 自动配置得 0 也填 0，不留空；写入由输入框的 on_change 负责
            else:
                cfg = self._wide_cfg(acc, tid) if tid in self.wide else self.store.save_cfg(acc, slot)  # 账号级任务的次数存在账号上
                cfg.setdefault("options", {}).setdefault(tid, {})["times"] = n
        self.store.save()
        self.refresh_reserve()
        return skipped

    def _apply_auto_state(self, acc: dict, slot: int, tid: str, state: tuple, here: bool) -> None:
        """不是次数的结果，存进该存档的 profile。"""
        if tid == time_boat.ID:  # 看板快照：和“读取”一样
            self._store_profile(self.store.save_cfg(acc, slot), state[1])
            if here:
                self.refresh_boat()
        elif tid == last_rewards.ID:  # (文字, 是否还有没领的, 各入口的领取状态, 周序号)
            self.store.save_cfg(acc, slot).setdefault("profile", {})[last_rewards.ID] = {"states": state[2], "week": state[3]}
            if here:
                self.refresh_reward()
