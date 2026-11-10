"""时光之舟：周常页里的独立模块（能量、兑换、读取）。"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout

from .. import core, results
from ..tasks import time_boat
from ..widgets import LINE, SUB, TEXT, WARN, Card, Field, RButton, Switch, ask_confirm, elide_label, hbox, hint, label, notice, set_fg
from .parts import EnergyBar, Fill
from .running import read_error


class BoatMixin:
    def _build_boat(self, page: QVBoxLayout) -> None:
        """顶部时光能量 + “读取”；每个兑换目标一行：每次消耗 / 本周剩余 / 挑战次数 / 次数框 / “兑换”。"""
        head = hbox(margins=(2, 0, 2, 3))
        head.addWidget(label("时光之舟", 9, SUB))
        head.addSpacing(8)
        head.addWidget(hint("次数提醒：本周兑换次数该用还没用完时名字变红。九宫、森林看对应养成周（第一周周末、第二周周一起），噩梦每周末。\n"
                            "预估：上次读取后过了几个 0 点 × 每天补充的点数，兑换以实时为准。\n"
                            "能量不够时问要不要用时光水晶补。\n"
                            "兑换并挑战：换一批打一批，按那个任务在日常页的设置打。"))
        head.addStretch(1)
        page.addLayout(head)
        card = Card(padx=16, pady=12)
        col = card.box()
        col.addLayout(self._boat_top())
        col.addSpacing(10)
        rule = Fill(LINE)
        rule.setFixedHeight(1)
        col.addWidget(rule)
        col.addSpacing(6)
        col.addLayout(self._boat_grid())
        page.addWidget(card)
        page.addSpacing(8)

    def _boat_top(self) -> QHBoxLayout:
        top = hbox()
        top.addWidget(label("时光能量", 10, bold=True))
        top.addSpacing(12)
        self.boat_bar = EnergyBar()
        top.addWidget(self.boat_bar)
        top.addSpacing(10)
        self.boat_energy = label("", 10, bold=True)
        top.addWidget(self.boat_energy)
        top.addSpacing(20)
        top.addWidget(label("次数提醒", 9))
        top.addSpacing(6)
        self.remind_var = Switch(bool((self.store.data.get("ui") or {}).get("remind", True)), self._toggle_remind)
        top.addWidget(self.remind_var)
        top.addStretch(1)
        self.boat_status = elide_label("", 9, SUB)  # 窄了省略，不把整页撑宽
        top.addWidget(self.boat_status)
        top.addSpacing(8)
        self.exec_btns["time_boat:read"] = RButton("读取", self.boat_read, "side", height=22, size=9, width=50)
        top.addWidget(self.exec_btns["time_boat:read"])
        return top

    def _boat_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(8)
        for c, width in enumerate((90, 72, 72, 72, 60)):  # 各列按内容定宽、靠左，不按比例撑满整张卡片
            grid.setColumnMinimumWidth(c, width)
        grid.setColumnStretch(9, 1)
        for c, text in enumerate(("兑换目标", "每次消耗", "本周剩余", "挑战次数", "兑换次数")):
            grid.addWidget(label(text, 9, SUB), 0, c)
        for r, (oid, name, cost, _remain, _fight) in enumerate(time_boat.board({})["rows"], start=1):
            self.boat_names[oid] = label(name)
            grid.addWidget(self.boat_names[oid], r, 0)
            grid.addWidget(label(f"{cost} 点"), r, 1)
            cells = self.boat_cells[oid] = tuple(label("—") for _ in range(2))
            for c, lbl in enumerate(cells, start=2):
                grid.addWidget(lbl, r, c)
            self.boat_vars[oid] = Field("1", width=48, height=24, on_change=lambda o=oid: self.save_boat_count(o))
            grid.addWidget(self.boat_vars[oid], r, 4)
            grid.setColumnMinimumWidth(5, 12)
            btn = self.exec_btns[f"time_boat:{oid}"] = RButton("兑换", lambda o=oid, n=name: self.boat_exchange(o, n), "side", height=22, size=9, width=50)
            grid.addWidget(btn, r, 6)
            grid.setColumnMinimumWidth(7, 8)
            btn = self.exec_btns[f"time_boat:{oid}:fight"] = RButton("兑换并挑战", lambda o=oid, n=name: self.boat_exchange(o, n, True), "side", height=22, size=9, width=84)
            grid.addWidget(btn, r, 8)
        return grid

    def _toggle_remind(self) -> None:
        self._save_ui()
        self.refresh_boat()

    def refresh_boat(self) -> None:
        """按当前账号/存档的快照（上次读取或兑换时存的）重画时光之舟模块。"""
        acc = self._acc()
        sv = self.store.save_view(acc, self.slot) if acc else {}
        data = time_boat.board(sv)
        self._show_boat_energy(data, sv)
        self.boat_status.setText(time.strftime("%m-%d %H:%M 读取", time.localtime(data["at"])) if data["at"] else "还没读取" if acc else "")
        for oid, _name, _cost, remain, fight in data["rows"]:
            for lbl, text in zip(self.boat_cells[oid], (remain, fight)):
                lbl.setText(text)
                set_fg(lbl, SUB if text in ("—", "0") or text.startswith("0/") else TEXT)
        from .. import online, servertime
        due = time_boat.urgent(sv, servertime.now(), online.known_growth_week(servertime.now())) if acc and self.remind_var.get() else set()
        for oid, lbl in self.boat_names.items():  # “次数提醒”开着：本周兑换次数该用了还没用完的目标名变红
            set_fg(lbl, WARN if oid in due else TEXT)
        self._load_boat_counts(sv)

    def _show_boat_energy(self, data: dict[str, Any], sv: dict) -> None:
        now, top = data["energy"], data["max"]
        known = now is not None and bool(top)
        from .. import servertime
        guess = time_boat.estimate(sv, servertime.now()) if known else None  # 读取之后过了几天：按每天补充的点数估一个现在的值
        self.boat_bar.set(now / top if known and now > 0 else None, guess / top if guess else None)
        self.boat_energy.setText((f"{now} / {top}" + (f"（预估 {guess}）" if guess is not None and guess != now else "")) if known else "— / —")

    def _load_boat_counts(self, sv: dict) -> None:
        counts = sv.get("options", {}).get(time_boat.ID, {}).get("counts", {})
        self._loading_counts = True
        for oid, var in self.boat_vars.items():
            saved = counts.get(str(oid), 1)
            var.set(str(saved) if saved else "")
        self._loading_counts = False

    def _boat_count(self, oid: int) -> int | None:
        """次数框里的值：留空 = 0；不是 1–上限的整数 = None。"""
        text = self.boat_vars[oid].get().strip()
        if not text:
            return 0
        return int(text) if text.isascii() and text.isdigit() and 1 <= int(text) <= time_boat.MAX_TIMES else None

    def save_boat_count(self, oid: int) -> None:
        n = self._boat_count(oid)
        if self.current is None or self._loading_counts or n is None:
            return
        cfg = self.store.save_cfg(self._acc(), self.slot)
        cfg.setdefault("options", {}).setdefault(time_boat.ID, {}).setdefault("counts", {})[str(oid)] = n
        self.store.save()

    def boat_exchange(self, oid: int, name: str, fight: bool = False) -> None:
        """某一行的“兑换”：只对当前账号的当前存档，用这一行的次数兑换这一个目标。
        fight = “兑换并挑战”：换一批打一批（对应任务按它自己在日常页的设置打），直到兑换够这一行的次数。"""
        if not self._require_account():
            return
        n = self._boat_count(oid)
        if not n:
            notice(self, "提示", f"请先在「{name}」这一行填兑换次数（1–{time_boat.MAX_TIMES}）")
            return
        cfg = self.store.save_cfg(self._acc(), self.slot)
        crystal = self._ask_crystal(cfg, oid, name, n)
        if crystal is None:
            return
        cfg.setdefault("options", {}).setdefault(time_boat.ID, {}).update(levels=[oid], times=n, crystal=crystal, fight=fight)
        self.store.save()
        tail = f"，并按「{self._title(time_boat.FIGHT[oid])}」的设置把兑换来的次数打掉" if fight else ""
        self.start(only=time_boat.ID, here=True, stay=not fight,  # 只兑换留在周常页，兑换并挑战跳到日志；用水晶时已在上面的确认框里确认过，不再弹第二个
                   what="" if crystal else f"时光之舟：用时光能量兑换「{name}」挑战次数 ×{n}{tail}")

    def _ask_crystal(self, cfg: dict[str, Any], oid: int, name: str, n: int) -> bool | None:
        """按上次读取的快照看能量够不够（任务里仍以服务器实时数据为准）。够 = False；不够就问要不要用时光水晶：用 = True，取消 = None。"""
        snap = time_boat.board(cfg)
        row = next((r for r in snap["rows"] if r[0] == oid), None)
        if row is None or snap["energy"] is None:
            return False
        remain = int(row[3]) if row[3].isdigit() else n
        need, have = min(n, remain) * row[2], int(snap["energy"])
        if have >= need:
            return False
        if ask_confirm(self, "时光能量不足",
                       f"「{name}」兑换 ×{min(n, remain)} 需要 {need} 点时光能量，当前只有 {have} 点。\n\n"
                       f"是否使用时光水晶补足缺少的 {need - have} 点？", "兑换", "取消"):
            return True
        return None

    def boat_read(self) -> None:
        """“读取”：登录当前存档，只读角色信息，更新时光能量和各目标的次数（不兑换）。"""
        target = self._read_target()
        if target is None:
            return
        acc, slot, account = target
        self.boat_status.setText("正在登录并读取…")

        def work() -> None:
            try:
                self.events.put(("boat", (acc, slot, time_boat.profile(core.read_info(account)))))
            except Exception as exc:
                self.events.put(("boat", (acc, slot, read_error(exc))))

        self._spawn(work, {acc.get("username", "")})

    def _on_boat(self, data: tuple) -> None:  # 时光之舟“读取”的结果：dict = 快照，str = 错误说明
        acc, slot, value = data
        if isinstance(value, dict):
            self._store_profile(self.store.save_cfg(acc, slot), value)
            self.store.save()
        self.refresh_boat()
        if not isinstance(value, dict):
            self.boat_status.setText(f"读取失败：{value}")

    def _store_profile(self, cfg: dict[str, Any], updates: dict[str, Any]) -> None:
        """先把存着的本周依据并进新读到的，再换掉显示用的快照。"""
        results.merge_profile(cfg, updates)
