"""UID 资料弹窗（2026-11-14 重做）：离屏检查分类切换、搜索、分批显示和复制。"""

import os
import unittest

from zmxy import data
from zmxy.uid import sheet as sh

try:
    from PySide6.QtWidgets import QApplication
    from zmxy.uid import ui
except ImportError:
    QApplication = ui = None


def sample(items=3):
    pid = next(iter(data.table("Prop")))
    sid = next(iter(data.table("Skill")))
    bag = {"item": {str(i): {"id": int(pid), "count": i + 1} for i in range(items)}}
    return sh.build(7, {"user": {"name": "对手", "level": 168, "skill": {sid: 8}, "bag": bag}}, "2026-11-14T12:00:00+08:00")


@unittest.skipIf(ui is None, "未安装 PySide6")
class UidProfileGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def open(self, data_sheet):
        dlg = ui.UidProfileDialog(None, data_sheet)
        dlg.show()
        self.app.processEvents()
        self.addCleanup(dlg.close)
        return dlg

    def test_tabs_follow_pages_with_counts(self):
        dlg = self.open(sample())
        self.assertEqual([c.text for c in dlg.tabs], ["概览  1", "技能  1", "背包  3"])

    def test_select_switches_page_and_keeps_size(self):
        dlg = self.open(sample())
        size = dlg.size()
        dlg.select(2)
        self.app.processEvents()
        self.assertEqual(dlg.page, 2)
        self.assertTrue(dlg.tabs[2].on and not dlg.tabs[0].on)
        self.assertEqual(len(dlg.area.findChildren(ui.ItemRow)), 3)
        self.assertEqual(dlg.size(), size)

    def test_search_spans_all_pages_and_clicking_tab_clears_it(self):
        dlg = self.open(sample())
        dlg.search.set("×2")
        dlg.refresh()
        self.assertEqual(len(dlg.area.findChildren(ui.ItemRow)), 1)
        dlg.search.set("找不到的东西")
        dlg.refresh()
        self.assertEqual(len(dlg.area.findChildren(ui.ItemRow)) + len(dlg.area.findChildren(ui.Card)), 0)
        dlg.select(1)
        self.assertEqual(dlg.search.get(), "")

    def test_big_group_shows_first_batch_then_rest(self):
        dlg = self.open(sample(ui.FIRST + 5))
        dlg.select(2)
        self.assertEqual(len(dlg.area.findChildren(ui.ItemRow)), ui.FIRST)
        more = next(b for b in dlg.area.findChildren(ui.RButton) if b.text.startswith("显示其余"))
        more.command()
        self.app.processEvents()
        self.assertEqual(len(dlg.area.findChildren(ui.ItemRow)), ui.FIRST + 5)

    def test_copy_all(self):
        s = sample()
        dlg = self.open(s)
        dlg.copy_all()
        self.assertEqual(QApplication.clipboard().text(), s.text())


if __name__ == "__main__":
    unittest.main()
