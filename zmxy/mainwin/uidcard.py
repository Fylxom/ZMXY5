"""UID 查询：周常页最底下的独立卡片（UID、角色编号、1P/2P、查询），各存档各存。"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from ..tasks import uid_lookup
from ..widgets import SUB, Card, Field, RButton, Segmented, hbox, label

UID_WIDTH = 150  # 10 位 UID 完整显示还留余量
ROLE_WIDTH = 64

class UidCardMixin:
    def _build_uid_card(self, page: QVBoxLayout) -> None:
        head = label("查询", 9, SUB)
        head.setContentsMargins(2, 0, 0, 3)
        page.addWidget(head)
        card = Card(padx=16, pady=10)
        col = card.box()
        col.setSpacing(6)
        row = hbox(spacing=8)
        name = self._name_label(uid_lookup.ID, "UID查询", clickable=False)
        name.setFixedWidth(90)
        row.addWidget(name)
        row.addWidget(label("UID", 9, SUB))
        self.uid_field = Field("", width=UID_WIDTH, height=26, on_change=self.save_uid, digits=12)
        self.uid_field.setPlaceholderText("对方 UID")
        row.addWidget(self.uid_field)
        row.addSpacing(8)
        row.addWidget(label("角色编号", 9, SUB))
        self.uid_role = Field("", width=ROLE_WIDTH, height=26, on_change=self.save_uid, digits=4)
        self.uid_role.setPlaceholderText("不填")
        row.addWidget(self.uid_role)
        self.uid_side = Segmented([(text, value) for value, text in uid_lookup.SIDES], "1p", self.save_uid,
                                  seg_width=40, height=26, size=9)
        row.addWidget(self.uid_side)
        row.addSpacing(8)
        self.exec_btns[uid_lookup.ID] = RButton("查询", lambda: self.start(only=uid_lookup.ID), "side", height=26, size=9, width=56)
        row.addWidget(self.exec_btns[uid_lookup.ID])
        row.addStretch(1)
        col.addLayout(row)
        note = label(uid_lookup.NOTE, 9, SUB)  # 直接写出来：很多人不知道角色编号是什么
        note.setWordWrap(True)
        note.setContentsMargins(98, 0, 0, 0)
        col.addWidget(note)
        page.addWidget(card)
        page.addSpacing(8)

    def save_uid(self) -> None:
        if self.current is None or getattr(self, "_loading_uid", False):
            return
        cfg = self.store.save_cfg(self._acc(), self.slot)
        cfg.setdefault("options", {})[uid_lookup.ID] = {
            "uid": self.uid_field.get().strip(), "role": self.uid_role.get().strip(), "side": self.uid_side.get()}
        self.store.save()

    def _load_uid(self, sv: dict) -> None:
        opts = sv.get("options", {}).get(uid_lookup.ID, {})
        self._loading_uid = True
        try:
            self.uid_field.set(str(opts.get("uid") or ""))
            self.uid_role.set(str(opts.get("role") if opts.get("role") is not None else ""))
            self.uid_side.set(opts.get("side") if opts.get("side") in ("1p", "2p") else "1p")
        finally:
            self._loading_uid = False
