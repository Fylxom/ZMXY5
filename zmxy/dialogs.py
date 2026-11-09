"""对话框：账号、验证码、选存档、盛典；任务的选择和洗练在 choice_dialog.py、refine_dialog.py，从这里拿。"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from . import core
from .choice_dialog import ChoiceDialog
from .refine_dialog import RefineDialog
from .store import WEEKDAYS, Store
from .widgets import SUB, Chip, Dialog, Field, Segmented, Switch, hbox, label, notice, vbox

__all__ = ["AccountDialog", "CaptchaDialog", "ChoiceDialog", "FestivalDialog", "RefineDialog", "SaveDialog"]


# ---- 账号对话框
class AccountDialog(Dialog):
    FIELDS = (("name", "备注名", False), ("username", "4399 账号", False),
              ("password", "密码", True), ("second_password", "二级密码", True))

    def __init__(self, master: QWidget, store: Store, acc: dict[str, Any]) -> None:
        super().__init__(master, "编辑账号" if acc.get("username") else "添加账号")
        self.store, self.acc, self.saved = store, acc, False
        self.vars: dict[str, Field] = {}
        for key, text, secret in self.FIELDS:
            self.body.addSpacing(6)
            self.body.addWidget(label(text, 9, SUB))
            self.body.addSpacing(4)
            value = store.get_secret(acc, key) if secret else acc.get(key, "")
            self.vars[key] = Field(value or "", width=320, password=secret, max_len=6 if key == "second_password" else None)  # 二级密码最多 6 位
            self.body.addWidget(self.vars[key])
        self.buttons("保存", self.save, top=20)
        self.on_return = self.save

    def save(self) -> None:
        username = self.vars["username"].get().strip()
        if not username:
            notice(self, "提示", "请填写账号")
            return
        # 执行项、日志、断点、读取到的角色信息都按备注名找回账号；账号级任务的设置按 4399 账号存。重了就会写到别的账号上
        others = [a for a in self.store.accounts if a is not self.acc]
        if any(a.get("username") == username for a in others):
            notice(self, "提示", f"账号 {username} 已经添加过了")
            return
        name = self.vars["name"].get().strip() or username
        if any(self.store.label(a) == name for a in others):
            notice(self, "提示", f"备注名“{name}”和别的账号重复了，请换一个")
            return
        old = self.acc.get("username")
        if old and old != username:  # 改了 4399 账号：账号级任务的设置、登高采菊等账号级状态跟着走
            for table in ("wide", "account_profiles"):
                rows = self.store.data.get(table)
                if isinstance(rows, dict) and old in rows:
                    rows[username] = rows.pop(old)
        for key, _, secret in self.FIELDS:
            val = self.vars[key].get()
            if secret:
                self.store.set_secret(self.acc, key, val)
            else:
                self.acc[key] = val.strip()
        self.acc["name"] = self.acc["name"] or self.acc["username"]
        self.saved = True
        self.accept()


class CaptchaDialog(Dialog):
    """4399 要求验证码时弹出：显示图片，输入答案。取消或关闭 = 放弃（answer 为 None）。"""

    def __init__(self, master: QWidget, image: bytes) -> None:
        super().__init__(master, "输入验证码")
        self.answer: str | None = None
        self.body.addWidget(label("4399 要求输入验证码", 9, SUB))
        self.body.addSpacing(8)
        pix = QPixmap()
        if pix.loadFromData(image):
            pic = QLabel()
            pic.setPixmap(pix)
            self.body.addWidget(pic)
        else:
            self.body.addWidget(label("验证码图片无法显示", 9, SUB))
        self.body.addSpacing(10)
        self.field = Field(width=200)
        self.body.addWidget(self.field)
        self.buttons("确定", self.ok)
        self.on_return = self.ok
        self.field.setFocus()

    def ok(self) -> None:
        text = self.field.get().strip()
        if text:
            self.answer = text
            self.accept()


class SaveDialog(Dialog):
    """账号级任务选存档：同一登录账号通用，与左边选中的存档无关。cfg["record"]：1-8。"""

    def __init__(self, master: QWidget, title: str, username: str, cfg: dict[str, Any], claim: bool = False, weekly: bool = False) -> None:
        super().__init__(master, f"{title} · 选存档")
        self.cfg, self.saved, self.claim, self.weekly = cfg, False, claim, weekly
        self.body.addWidget(label(f"账号 {username}：该账号级任务使用所选存档，所有存档共用这个选择", 9, SUB))
        self.body.addSpacing(10)
        slots = [(str(i), str(i)) for i in range(1, 9)]
        self.record = Segmented(slots, str(cfg.get("record") or 1), seg_width=35, size=9)
        self.claim_record = Segmented(slots, str(cfg.get("claim_record") or cfg.get("record") or 1), seg_width=35, size=9)
        for text, seg in (("发船存档", self.record), ("领奖存档", self.claim_record)) if claim else (("", self.record),):
            if text:
                self.body.addSpacing(8)
                self.body.addWidget(label(text))
                self.body.addSpacing(4)
            self.body.addWidget(seg)
        if weekly:
            self._build_week(slots)
        self.buttons("完成", self.save)
        self.on_return = self.save

    def _build_week(self, slots: list[tuple[str, str]]) -> None:
        """“多存档”开关：打开后单个存档那一条换成周一…周日各一条，每天各选一个存档。"""
        saved = self.cfg.get("week")
        saved = saved if isinstance(saved, list) and len(saved) == 7 else [self.cfg.get("record") or 1] * 7
        self.multi = Switch(bool(self.cfg.get("multi")), self._show_week)
        row = hbox()
        row.addWidget(label("多存档"))
        row.addSpacing(8)
        row.addWidget(self.multi)
        row.addSpacing(8)
        row.addWidget(label("每周几各用哪个存档", 9, SUB))
        row.addStretch(1)
        self.body.insertLayout(2, row)
        self.body.insertSpacing(3, 8)
        self.week_box = QWidget()
        col = vbox(self.week_box)
        self.week: list[Segmented] = []
        for day, record in zip(WEEKDAYS, saved):
            line = hbox(margins=(0, 3, 0, 3))
            line.addWidget(label(day))
            line.addSpacing(10)
            self.week.append(Segmented(slots, str(record), seg_width=35, size=9))
            line.addWidget(self.week[-1])
            line.addStretch(1)
            col.addLayout(line)
        self.body.addWidget(self.week_box)
        self._show_week()

    def _show_week(self) -> None:
        self.record.setVisible(not self.multi.get())
        self.week_box.setVisible(self.multi.get())

    def save(self) -> None:
        self.cfg["record"] = int(self.record.get())
        if self.weekly:
            self.cfg["multi"] = self.multi.get()
            self.cfg["week"] = [int(s.get()) for s in self.week]
        if self.claim:
            self.cfg["claim_record"] = int(self.claim_record.get())
        self.saved = True
        self.accept()


class FestivalDialog(Dialog):
    """盛典选择：三个任务各自开关，关闭时收起选项并保留选择。"""

    def __init__(self, master: QWidget, spec: core.TaskSpec, login: dict[str, Any], slot: int, cfg: dict[str, Any] | None = None) -> None:
        super().__init__(master, f"{spec.title} · 存档 {slot} · 选择")
        ch = spec.choices
        assert ch is not None and ch.items is not None
        self.acc = cfg if cfg is not None else master.store.save_cfg(login, slot)
        self.spec, self.saved = spec, False
        saved = self.acc.get("options", {}).get(spec.id, {})
        self.toggles: dict[str, Switch] = {}
        self.groups: dict[str, QWidget] = {}
        self.chips: dict[str, dict[Any, Chip]] = {}
        self.picked: dict[str, set[Any]] = {}
        self.items = {ch.option: ch.items(), **{pk.key: pk.items(self.acc) for pk in ch.picks}}
        defaults = dict(ch.toggle_defaults)
        sections = (
            ("do_online", "在线任务", None,
             "等待每日挑战所需的在线时长，期间自动保持连接。"),
            ("do_compose", "炼丹炉", "compose",
             "从勾选类别中选择材料足够、已解锁的配方，合成一次。"),
            ("do_items", "使用关卡道具", ch.option,
             "在日常任务 → 主线 → 精英的第一场副本中使用；均未勾选时在主线第一关使用。"),
        )
        for index, (key, title, option, hint) in enumerate(sections):
            if index:
                self.body.addSpacing(16)
            row = hbox(spacing=8)
            row.addWidget(label(title, 10, bold=True))
            self.toggles[key] = Switch(bool(saved.get(key, defaults.get(key, True))), self._show_sections)
            row.addWidget(self.toggles[key])
            row.addStretch(1)
            self.body.addLayout(row)
            group = self.groups[key] = QWidget()
            box = vbox(group, spacing=8, margins=(0, 8, 0, 0))
            if option is not None:
                self._add_chips(box, option, saved.get(option) or ())
            note = label(hint, 9, SUB)
            note.setWordWrap(True)
            note.setFixedWidth(472)
            box.addWidget(note)
            self.body.addWidget(group)
        self._show_sections()
        self.buttons("完成", self.save)
        self.on_return = self.save

    def _add_chips(self, box: QVBoxLayout, option: str, raw: Any) -> None:
        items = self.items[option]
        raw = (raw,) if isinstance(raw, (str, int)) else raw
        self.picked[option] = set(raw) & {value for value, _ in items}
        self.chips[option] = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        columns = 4 if option == "compose" else 3
        for i, (value, name) in enumerate(items):
            chip = Chip(name, value in self.picked[option], lambda v=value: self._pick(option, v), width=112, height=34)
            self.chips[option][value] = chip
            grid.addWidget(chip, *divmod(i, columns))
        grid.setColumnStretch(columns, 1)
        box.addLayout(grid)

    def _show_sections(self) -> None:
        for key, group in self.groups.items():
            group.setVisible(self.toggles[key].get())

    def _pick(self, option: str, value: Any) -> None:
        self.picked[option].symmetric_difference_update({value})
        self.chips[option][value].set(value in self.picked[option])

    def save(self) -> None:
        entry = dict(self.acc.get("options", {}).get(self.spec.id, {}))
        entry.update({key: sw.get() for key, sw in self.toggles.items()})
        entry.update({key: [value for value, _ in items if value in self.picked[key]]
                      for key, items in self.items.items()})
        self.acc.setdefault("options", {})[self.spec.id] = entry
        self.saved = True
        self.accept()
