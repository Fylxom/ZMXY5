"""任务行上的选择：次数、模式、选存档、选择框，以及行上的摘要文字；世界Boss 选择框里的估伤。"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QWidget

from .. import core
from ..dialogs import ChoiceDialog, FestivalDialog, RefineDialog, SaveDialog
from ..store import WEEKLY_WIDE
from ..tasks import boss_raid, pet_catch, refine
from ..widgets import ACCENT, TEXT, WARN, set_fg
from .running import read_error


FESTIVAL_PARTS = (("do_online", "在线任务"), ("do_compose", "炼丹炉"), ("do_items", "使用关卡道具"))


def festival_summary(opt: dict[str, Any]) -> str:
    selected = [name for key, name in FESTIVAL_PARTS if opt.get(key, True)]
    return selected[0] if len(selected) == 1 else f"{len(selected)} 项任务" if selected else "未选择"


def picks_summary(ch: core.Choices, opt: dict[str, Any], sv: dict) -> str:
    """只有附加选择行的任务（如幻兽喂养）：每行下拉选了谁就显示谁（去掉等级和阶数，它们会随喂养变）。"""
    parts = []
    for pk in ch.picks:
        value = opt.get(pk.key)
        if value in (None, "", ()):
            continue
        if pk.text or pk.number:
            shown = str(value)
        else:
            names = dict(pk.items(sv)) if pk.single else {}
            if value not in names:
                continue
            shown = names[value].partition("\t")[0]
        parts.append((f"{pk.label} " if len(ch.picks) > 1 else "") + shown)
    return " · ".join(parts) if parts else "未选择"


def items_summary(ch: core.Choices, opt: dict[str, Any], names: dict) -> str:
    """选了哪些项，再加次数和补货。"""
    picked = [v for v in opt.get(ch.option, ()) if v in names]
    if not picked:
        return "未选择"
    text = "、".join(names[v] for v in picked) if len(picked) <= 2 else f"{len(picked)} {ch.noun}"
    if ch.times:
        times = opt.get("times", ch.times_default)
        text += f" ×{times}" if times else " · 次数 0"
    sellers = opt.get("sellers")
    if sellers:
        text += f" · 补货({len(sellers)})"
    return text


def percent(v: float) -> str:
    return f"{v:.2f}%" if v < 10 else f"{v:.1f}%" if v < 100 else f"{v:.0f}%"


class ChoicesMixin:
    def _opts_cfg(self, tid: str) -> dict[str, Any]:
        """该任务的选项存哪：账号级任务（通天河）按登录账号存、各存档通用（读取到的角色信息仍取它执行的存档）；其余存当前查看的存档。"""
        acc = self._acc()
        if tid not in self.wide:
            return self.store.save_cfg(acc, self.slot)
        wcfg = self._wide_cfg(acc, tid)
        return {"options": wcfg.setdefault("options", {}), "profile": self.store.save_cfg(acc, int(wcfg["record"])).setdefault("profile", {})}

    def refresh_summaries(self) -> None:
        acc = self._acc() or {}
        sv = self.store.save_view(acc, self.slot) if acc else {}
        for tid, lbl in self.wide_labels.items():
            lbl.setText(self._wide_summary(tid, acc))
        self.refresh_boat()
        self.refresh_reward()  # 上届奖励的领取状态也是每个存档各自的
        self._refresh_refine(acc, sv)
        for tid, lbl in self.summaries.items():
            lbl.setText(self._summary(tid, acc, sv))

    def _wide_summary(self, tid: str, acc: dict[str, Any]) -> str:
        if not acc:
            return ""
        wc = self._wide_cfg(acc, tid)
        if tid == "tongtian_escort":
            return f"发{wc['record']}·领{wc.get('claim_record') or wc['record']}"
        return f"多存档 · 今天 {self.store.wide_record(wc)}" if wc.get("multi") else f"存档 {wc['record']}"

    def _refresh_refine(self, acc: dict[str, Any], sv: dict) -> None:
        idle = (sv.get("profile") or {}).get(refine.IDLE) if acc else ""  # 上次执行：法宝已达标，或其他块达标 / 全锁且还有免费次数
        self.refine_lbl.setText((refine.summary(sv.get("options", {}).get(refine.ID, {})) + (f"　{idle}达标或全锁，可换目标" if idle else "")) if acc else "")
        self.idle_red = {refine.ID} if idle else set()
        set_fg(self.task_labels[refine.ID], WARN if idle else ACCENT if self.picked_task == refine.ID else TEXT)

    def _summary(self, tid: str, acc: dict[str, Any], sv: dict) -> str:
        ch = self.specs[tid].choices
        opt = self._task_opts(acc, sv, tid)
        if tid == "festival":
            return festival_summary(opt)
        if ch.items is None:
            return picks_summary(ch, opt, sv)
        names = self._names.get(tid)
        if names is None:
            try:
                names = self._names[tid] = dict(ch.items())
            except Exception as exc:  # 选项来自数据表（会自动更新）：一张表对不上只影响这一行，别让整个界面起不来
                return f"选项读取失败：{type(exc).__name__}"
        text = items_summary(ch, opt, names)
        if ch.mode_inside:  # 档位在选择框里选：行上只显示
            modes = dict(self.specs[tid].modes)
            mode = f"{modes.get(opt.get('mode'), modes[self.specs[tid].default_mode])}档"
            text = f"{mode} · {text}" if tid == boss_raid.WORLD_ID else f"{text} · {mode}"
        if tid == boss_raid.WORLD_ID and acc:
            text = f"存档 {self._wide_cfg(acc, tid)['record']} · {text}"
        return text

    def choose(self, spec: core.TaskSpec) -> None:
        if not self._require_account():
            return
        acc = self._acc()
        wide = spec.id in self.wide
        slot = int(self._wide_cfg(acc, spec.id)["record"]) if wide else self.slot
        dialog = FestivalDialog if spec.id == "festival" else ChoiceDialog
        dlg = dialog(self, spec, acc, slot, self._opts_cfg(spec.id) if wide else None)
        dlg.run()
        if dlg.saved:
            self.store.save()
            self.refresh_summaries()
            if spec.id == pet_catch.ID:  # 换了宠物：筛选换成那种宠物自己的那一份
                self._load_pet_filter(self.store.save_view(acc, self.slot))

    def open_refine(self) -> None:
        if not self._require_account():
            return
        acc = self._acc()
        dlg = RefineDialog(self, acc, self.slot, self.store.save_cfg(acc, self.slot))
        dlg.run()
        self.store.save()  # 取消也存：读取到的翅膀清单记在存档 profile 里
        if dlg.saved:
            self.refresh_summaries()

    def save_count(self, spec: core.TaskSpec) -> None:
        """行上次数输入框：0–上限的整数存下来（0 和留空都是不执行），其他输入先不动。"""
        if self.current is None or self._loading_counts:
            return
        text = self.count_vars[spec.id].get().strip()
        if text and not (text.isascii() and text.isdigit() and int(text) <= spec.times_max):
            return
        cfg = self._opts_cfg(spec.id)
        cfg.setdefault("options", {}).setdefault(spec.id, {})["times"] = int(text) if text else 0
        self.store.save()

    def save_weekly(self) -> None:
        """周常页的开关：这个存档“自动配置并执行”和周常定时做哪些。"""
        if self.current is None:
            return
        self.store.save_cfg(self._acc(), self.slot)["weekly"] = [t for t, v in self.weekly_vars.items() if v.get()]
        self.store.save()

    def save_mode(self, spec: core.TaskSpec) -> None:
        if self.current is None:
            return
        cfg = self._opts_cfg(spec.id)
        cfg.setdefault("options", {}).setdefault(spec.id, {})["mode"] = self.mode_vars[spec.id].get()
        self.store.save()

    def save_tasks(self) -> None:
        if self.current is None:
            return
        acc = self._acc()
        self.store.save_cfg(acc, self.slot)["tasks"] = [t for t, v in self.task_vars.items() if v.get() and t not in self.wide]
        for tid in self.wide:  # 账号级任务按登录账号保存，同账号的各存档共用（用户明确要求）
            if tid in self.task_vars:  # 通天河三项没有开关，只能单独点执行
                self._wide_cfg(acc, tid)["on"] = self.task_vars[tid].get()
        self.store.save()

    def choose_save(self, spec: core.TaskSpec, parent: QWidget | None = None) -> None:
        if not self._require_account():
            return
        acc = self._acc()
        dlg = SaveDialog(parent or self, spec.title, self.store.label(acc), self._wide_cfg(acc, spec.id), claim=spec.id == "tongtian_escort", weekly=spec.id in WEEKLY_WIDE)
        dlg.run()
        if dlg.saved:
            self.store.save()
            self.on_select()  # 换了执行存档：次数框要读新存档的

    def toggle_pickup(self, key: str) -> None:
        if self.current is None:
            return
        chip = self.pickup_chips[key]
        chip.set(not chip.on)
        self.store.save_cfg(self._acc(), self.slot)["no_pickup"] = [k for k, c in self.pickup_chips.items() if c.on]
        self.store.save()

    def boss_guess(self, dlg: ChoiceDialog) -> None:
        """世界Boss 选择框里的“估伤”：登录选定的存档，只读战力和装备，估低档 / 高档每分钟打掉每个祖巫百分之几，写在框里各祖巫的名字下面（不弹框、不进房间）。"""
        if self.current is None:
            return
        acc, tid = self._acc(), boss_raid.WORLD_ID
        wcfg = self._wide_cfg(acc, tid)
        if self._busy():
            dlg._status("这个账号正在执行，等它结束再估")
            return
        try:
            account = self.store.to_account(acc, self._cookie_saver(acc), int(wcfg["record"]))
        except Exception as exc:
            dlg._status(f"账号配置错误：{exc}")
            return
        self._guess_host = dlg
        dlg._status("正在登录估算…")
        spec = self.specs[tid]

        def work() -> None:
            try:
                with core.login_lock(account.config.username), core.Session(account.config, on_cookie=account.on_cookie) as session:
                    session.bind(spec)
                    self.events.put(("boss_guess", (int(wcfg["record"]), boss_raid.estimate(session))))
            except Exception as exc:
                self.events.put(("boss_guess", (int(wcfg["record"]), read_error(exc))))

        self._spawn(work, {acc.get("username", "")})

    def _on_boss_guess(self, data: tuple) -> None:  # “估伤”的结果：tuple = (战力, 装备倍数, [(祖巫 id, 低档, 高档)], 角色等级)，str = 错误说明
        slot, value = data
        host, self._guess_host = self._guess_host if self._guess_host is not None and self._guess_host.isVisible() else None, None
        if host is None:  # 选择框已经关了
            return
        if isinstance(value, str):
            host._status(f"估伤失败：{value}")
            return
        power, gear, rows, level = value
        if level is not None and (host.acc.get("profile") or {}).get(boss_raid.PROFILE_KEY) != level:  # 顺手记下角色等级：等级不够的祖巫不显示（原来要点“读取”）
            host.acc.setdefault("profile", {})[boss_raid.PROFILE_KEY] = level
            host._refill_items()
            self.store.save()
        host.annotate({boss: f"低 {percent(low)}  高 {percent(high)}" for boss, low, high in rows},
                      f"存档 {slot} 战力 {power:,.0f}，装备倍数 " + (f"{gear:.2f}" if gear else "读不到（按主线饰品、没有红莲算）"),
                      "低档 / 高档每分钟造成的伤害，占 Boss 血量的百分比")

    def choose_world_boss_save(self, dlg: ChoiceDialog) -> None:
        """世界Boss 选择框里的“选存档”：修改账号级目标存档后按新角色等级刷新可选祖巫。"""
        spec = self.specs[boss_raid.WORLD_ID]
        self.choose_save(spec, parent=dlg)
        if self.current is None:
            return
        acc = self._acc()
        cfg = self._wide_cfg(acc, spec.id)
        dlg.slot = int(cfg["record"])
        dlg.acc["profile"] = self.store.save_cfg(acc, dlg.slot).setdefault("profile", {})
        dlg._refill_items()
