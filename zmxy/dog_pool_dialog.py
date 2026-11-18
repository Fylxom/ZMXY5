"""狗卡轮转的选择框（活动页“抽取狗卡”行上的“选择”）：哪些号参加、用哪个存档、哪个是集卡号；显示上一轮各号的进度。"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QGridLayout, QWidget

from .store import Store
from .tasks.events import dog_pool
from .widgets import SUB, WARN, Check, Dialog, Select, elide_label, hbox, hint, label, notice

SLOTS = [str(i) for i in range(1, 9)]
NOTE = ("参加的号一起登录：各自抽完、新种类自己激活，再互相送卡，每个号都拿到“当天首次赠送”多给的那次抽卡。\n"
        "集卡号：别的号有它没激活的种类的多余卡，先送给它激活；每天送完以后，各号剩下的多余卡都汇总给它\n"
        "（它没激活过的种类留在原号）。\n"
        "同一个 4399 账号的存档不能互送，所以每个账号只选一个存档。\n"
        "参加的存档里有一个开着这一行的开关、执行到它（包括定时），就把全部参加的号一起做一轮，别的号开不开都行；\n"
        "一天只做一轮，单独点这一行的“执行”会再做一轮。没参加的存档开着开关，照旧只抽自己。")


def progress(st: Any) -> str:
    """上一轮记下的进度：激活 n/总数、缺的种类、这一轮抽 / 送 / 收。"""
    if not isinstance(st, dict):
        return "还没做过"
    text = f"激活 {st.get('act', 0)}/{st.get('total', 0)}"
    missing = st.get("missing") or []
    if missing:
        text += " · 缺" + "、".join(missing) if len(missing) <= 3 else f" · 缺 {len(missing)} 种"
    return text + f" · 上次 抽{st.get('drawn', 0)} 送{st.get('gave', 0)} 收{st.get('got', 0)}"


def summary(store: Store) -> str:
    """行上的灰字：几个号参加、谁是集卡号。"""
    members = dog_pool.members_of(store.data.get("dog_pool"))
    if not members:
        return "只抽自己"
    names = {a.get("username"): store.label(a) for a in store.accounts}
    hub = [names.get(m["user"], m["user"]) for m in members if m["hub"]]
    return f"{len(members)} 个号" + (f" · 集卡：{hub[0]}" if hub else "")


class DogPoolDialog(Dialog):
    def __init__(self, master: QWidget, store: Store) -> None:
        super().__init__(master, "抽取狗卡 · 多号轮转")
        self.store, self.saved = store, False
        head = hbox()
        head.addWidget(label("选几个号一起抽、互相送卡", 9, SUB))
        head.addSpacing(6)
        head.addWidget(hint(NOTE))
        head.addStretch(1)
        self.body.addLayout(head)
        self.body.addSpacing(10)
        record, broken = dog_pool.peek_record()
        status = record.get("status") if isinstance(record.get("status"), dict) else {}
        old = {m["user"]: m for m in dog_pool.members_of(store.data.get("dog_pool"))}
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        for col, text in enumerate(("账号", "参加", "存档", "集卡", "上一轮")):
            grid.addWidget(label(text, 9, SUB), 0, col)
        self.rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for acc in store.accounts:
            user = acc.get("username")
            if not isinstance(user, str) or not user or user in seen:
                continue
            seen.add(user)
            m = old.get(user)
            row: dict[str, Any] = {"user": user}
            row["join"] = Check(m is not None, lambda r=row: self._flip(r, "join"))
            row["slot"] = Select(SLOTS, str(m["slot"] if m else 1), width=56, height=26, size=9, command=lambda r=row: self._show(r))
            row["hub"] = Check(bool(m and m["hub"]), lambda r=row: self._flip(r, "hub"))
            row["state"] = elide_label("", 9, SUB)
            row["state"].setFixedWidth(300)
            row["status"] = status
            n = len(self.rows) + 1
            name = elide_label(store.label(acc), 10)
            name.setFixedWidth(120)
            grid.addWidget(name, n, 0)
            for col, key in enumerate(("join", "slot", "hub", "state"), 1):
                grid.addWidget(row[key], n, col)
            self.rows.append(row)
            self._show(row)
        self.body.addLayout(grid)
        self.warn = label("", 9, WARN)
        self.body.addSpacing(8)
        self.body.addWidget(self.warn)
        codes, failed, unknown = (record.get(k) or [] for k in ("codes", "failed", "unknown"))
        parts = [broken] if broken else []
        parts += [f"有 {len(codes)} 个兑换码还没兑换完，下一轮先处理"] if codes else []
        parts += [f"{len(failed)} 个兑换码兑换失败"] if failed else []
        parts += [f"{len(unknown)} 次赠送 / 兑换结果不明，请到游戏里核对"] if unknown else []
        if parts:
            self.body.addSpacing(4)
            self.body.addWidget(label("；".join(parts) + ("（记在 dog_pool.json）" if failed or unknown else ""), 9, WARN))
        self._check()
        self.buttons("完成", self.save)
        self.on_return = self.save

    def _flip(self, row: dict[str, Any], key: str) -> None:
        box = row[key]
        box.set(not box.on)
        if key == "hub" and box.on:
            row["join"].set(True)  # 集卡号一定参加，而且只有一个
            for other in self.rows:
                if other is not row:
                    other["hub"].set(False)
        if key == "join" and not box.on:
            row["hub"].set(False)
        self._show(row)
        self._check()

    def _show(self, row: dict[str, Any]) -> None:
        on = row["join"].on
        row["state"].setText(progress(row["status"].get(dog_pool.key_of(row["user"], int(row["slot"].get())))) if on else "")

    def _check(self) -> None:
        joined = [r for r in self.rows if r["join"].on]
        text = ""
        if len(joined) == 1:
            text = "只有 1 个号参加：没有别的号可以互送，等于只抽自己"
        elif joined and not any(r["hub"].on for r in joined):
            text = "没选集卡号：多余的卡留在各号手里"
        self.warn.setText(text)
        self.warn.setVisible(bool(text))

    def save(self) -> None:
        members = [{"user": r["user"], "slot": int(r["slot"].get()), "hub": r["hub"].on} for r in self.rows if r["join"].on]
        if len(members) == 1:
            notice(self, "提示", "至少选 2 个号才能互送；只抽自己的话一个都不用勾")
            return
        self.store.dog_pool()["members"] = members
        self.saved = True
        self.accept()
