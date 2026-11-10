"""UID 资料弹窗的离屏布局、页签、详情、搜索和完整复制回归。"""

import os
import unittest
from pathlib import Path

from zmxy.uid import profile

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFontDatabase
    from zmxy.uid.ui import ProfileRow, UidProfileDialog
except ImportError:
    QApplication = UidProfileDialog = None


@unittest.skipIf(UidProfileDialog is None, "未安装 PySide6")
class UidProfileGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        # offscreen 插件不自动加载 Windows 中文字体；按真实中文度量校验两行布局。
        for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
            if Path(path).exists():
                QFontDatabase.addApplicationFont(path)

    def setUp(self):
        self.payload = profile.build_payload(7, {
            "user": {"name": "对手", "baoZhu": {"lv": 2}, "showTitle": 1,
                     "equipments": {"weapon": {"id": 110708063, "name": "天枢伞", "level": 168,
                         "recast": {"atk": {"level": 9, "add": 1415}}}}},
            "bag": {"item": [{"id": 101001001, "count": 94, "type": "item",
                             "isConsume": 1, "updateTime": 1791986467583, "entityId": "item-123"}]},
        }, "2026-10-16T12:00:00+08:00")
        self.dlg = UidProfileDialog(None, self.payload)
        self.dlg.show()
        self.app.processEvents()
        self.addCleanup(self.dlg.close)

    def select(self, title):
        self.dlg.page_index = next(i for i,p in enumerate(self.dlg.pages) if p.title == title)
        self.dlg.refresh()
        self.app.processEvents()

    def rows(self):
        return self.dlg.area.findChildren(ProfileRow)

    def test_simple_item_stays_within_two_visual_lines(self):
        self.select("背包仓库")
        row = self.rows()[0]
        self.assertIn("还魂丹", row.caption.text())
        self.assertLessEqual(row.caption.height(), row.caption.fontMetrics().lineSpacing() * 2 + 6)
        self.assertNotIn("item-123", row.caption.text())

    def test_equipment_details_keep_grouping_and_toggle_closed(self):
        self.select("装备外观")
        row = self.rows()[0]
        self.assertIsNone(row.details)
        row.toggle_details()
        self.app.processEvents()
        self.assertTrue(row.details.isVisible())
        self.assertIn("洗练\n  攻击\n    等级：9", row.details.text())
        row.toggle_details()
        self.assertFalse(row.details.isVisible())

    def test_search_can_find_collapsed_detail_and_copy_retains_it(self):
        self.select("装备外观")
        self.dlg.search.setText("1415")
        self.dlg.refresh()
        self.assertEqual(len(self.rows()), 1)
        self.dlg.copy_page()
        self.assertIn("加成：1415", QApplication.clipboard().text())
        self.dlg.search.setText("不存在")
        self.dlg.refresh()
        self.assertEqual(len(self.rows()), 0)

    def test_technical_toggle_and_tabs_preserve_dialog_size(self):
        original = self.dlg.size()
        self.select("背包仓库")
        row = self.rows()[0]
        row.toggle_details()
        self.dlg.toggle_technical()
        self.app.processEvents()
        row = self.rows()[0]
        self.assertIn("item-123", row.details.text())
        self.dlg.copy_all()
        self.assertIn("item-123", QApplication.clipboard().text())
        self.select("装备外观")
        self.assertEqual(self.dlg.size(), original)

    def test_album_has_one_compact_pet_row_with_all_photos_in_details(self):
        self.dlg.close()
        payload = profile.build_payload(7, {"user": {"petAlbum": {
            "1": {"id": 171541301, "level": 149, "time": 1791986467583},
            "2": {"id": 171541301, "level": 168, "time": 1792072867583},
        }}}, "2026-10-16T12:00:00+08:00")
        self.dlg = UidProfileDialog(None, payload)
        self.addCleanup(self.dlg.close)
        self.dlg.show()
        self.select("伙伴")
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertIn("相片：2 张", row.caption.text())
        self.assertLessEqual(row.caption.height(), row.caption.fontMetrics().lineSpacing() * 2 + 6)
        row.toggle_details()
        self.assertIn("碧玉径：苍穹蒲牢", row.details.text())
        self.assertIn("碧玉径BOSS：苍穹蒲牢", row.details.text())


if __name__ == "__main__":
    unittest.main()
