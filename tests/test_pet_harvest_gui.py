"""刷益灵花内嵌宠物捕捉卡片的保存、切换和执行入口回归。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from PySide6.QtWidgets import QApplication, QLabel
    from PySide6.QtGui import QFontDatabase
    from zmxy import gui
    from zmxy.store import Store
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, '未安装PySide6')
class HarvestGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # offscreen不自动加载系统中文字体；按实际中文尺寸校验选择方块。
        for path in ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhbd.ttc'):
            if Path(path).exists():
                QFontDatabase.addApplicationFont(path)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'settings.json')
        self.store.accounts[:] = [{'username': 'u', 'name': '离线测试', 'saves': {
            '1': {'tasks': [], 'options': {'pet_cultivation': {'harvest_times': 3,
                'auto_release': True, 'cleanup_rejected': False, 'levels': [61]}}},
            '2': {'tasks': [], 'options': {'pet_cultivation': {'harvest_times': 5,
                'auto_release': False, 'cleanup_rejected': True}}}}}]
        with mock.patch.object(gui, 'Store', return_value=self.store):
            self.win = gui.App(background=True)
        self.win._timer.stop()
        self.addCleanup(lambda: self.app.setQuitOnLastWindowClosed(True))
        self.app.setQuitOnLastWindowClosed(False)
        self.addCleanup(self.win.close)
        self.win.current = 0
        self.win._set_slot(1)
        self.win._load_pet_harvest(self.store.save_view(self.store.accounts[0], 1))

    def test_renamed_and_excluded_from_generic_weekly_tasks(self):
        self.assertEqual(self.win.specs['pet_cultivation'].title, '刷益灵花')
        self.assertNotIn('pet_cultivation', self.win.weekly_run_ids)
        self.assertIn('pet_cultivation', self.win.exec_btns)
        self.assertIsNotNone(self.win.pet_harvest_count)
        self.assertEqual(set(self.win.pet_harvest_flags), {'auto_release', 'cleanup_rejected'})
        titles = [w for w in self.win.findChildren(QLabel) if w.text() == '刷益灵花']
        self.assertEqual(len(titles), 1)
        self.assertIs(self.win.pet_harvest_count.parentWidget(), self.win.pet_buy.parentWidget())

    def test_star_seal_is_independent_row_inside_pet_card(self):
        from zmxy.tasks import pet_catch
        self.assertEqual(len(pet_catch.catalog()), 4)
        self.assertNotIn('star_seal', self.win.weekly_run_ids)
        field = self.win.count_vars['star_seal']
        self.assertIs(field.parentWidget(), self.win.pet_buy.parentWidget())
        with mock.patch.object(self.win, 'start') as start:
            self.win.exec_btns['star_seal'].command()
        start.assert_called_once_with(only='star_seal')
        field.set('7')
        cfg = self.store.save_view(self.store.accounts[0], 1)
        self.assertEqual(cfg['options']['star_seal']['times'], 7)
        self.assertNotEqual(cfg.get('options', {}).get('pet_catch', {}).get('times'), 7)

    def test_star_seal_choices_are_single_select_and_fit_chips(self):
        from zmxy.dialogs import ChoiceDialog
        from zmxy.widgets import font, measure
        spec = self.win.specs['star_seal']
        dialog = ChoiceDialog(self.win, spec, self.store.accounts[0], 1)
        self.addCleanup(dialog.close)
        self.assertEqual(len(dialog.items), 4)
        self.assertEqual(dialog.ch.limit, 1)
        for mid in (10011, 10012, 10013, 10014):
            chip = dialog.chips[mid]
            text = dict(dialog.items)[mid]
            self.assertLessEqual(measure(font(10), text), chip.width() - 16)
        dialog.toggle(10011)
        dialog.toggle(10013)
        self.assertEqual(dialog.picked, {10013})

    def test_old_options_load_without_enabling_both_switches(self):
        self.assertEqual(self.win.pet_harvest_count.get(), '3')
        self.assertTrue(self.win.pet_harvest_flags['auto_release'].get())
        self.assertFalse(self.win.pet_harvest_flags['cleanup_rejected'].get())

    def test_vip_note_uses_existing_hover_hint_and_level_wording(self):
        card = self.win.pet_harvest_count.parentWidget()
        labels = card.findChildren(QLabel)
        hints = [w for w in labels if w.text() == '说明' and '潜能' in w._tip.text and '益灵花' in w._tip.text]  # 卡片里另有筛选的说明
        self.assertEqual(len(hints), 1)
        note = hints[0]._tip.text
        self.assertIn('3 级', note)
        self.assertIn('4 级', note)
        self.assertNotIn('阶', note)
        self.assertFalse(any('VIP3' in w.text() for w in labels))

    def test_edit_and_reopen_persist_same_task_options(self):
        self.win.pet_harvest_count.set('7')
        self.win.pet_harvest_flags['auto_release'].set(False)
        self.win.pet_harvest_flags['cleanup_rejected'].set(True)
        self.win.save_pet_harvest()
        saved = Store(self.store.path).save_view(self.store.accounts[0], 1)['options']['pet_cultivation']
        self.assertEqual(saved['harvest_times'], '7')
        self.assertFalse(saved['auto_release'])
        self.assertTrue(saved['cleanup_rejected'])
        self.assertEqual(saved['levels'], [61])  # 仅编辑新控件，不覆写其他旧数据。

    def test_load_other_slot_does_not_overwrite_previous_slot(self):
        with mock.patch.object(self.store, 'save') as save:
            self.win._set_slot(2)
            self.win._load_pet_harvest(self.store.save_view(self.store.accounts[0], 2))
        save.assert_not_called()
        self.assertEqual(self.win.pet_harvest_count.get(), '5')
        self.assertFalse(self.win.pet_harvest_flags['auto_release'].get())
        self.assertTrue(self.win.pet_harvest_flags['cleanup_rejected'].get())

    def test_execute_starts_harvest_directly_without_dialog(self):
        with mock.patch.object(self.win, 'start') as start, mock.patch.object(self.win, 'choose') as choose:
            self.win.exec_btns['pet_cultivation'].command()
        start.assert_called_once_with(only='pet_cultivation')
        choose.assert_not_called()

    def test_pet_card_inputs_share_size_and_action_columns_align(self):
        fields = [self.win.pet_buy, self.win.pet_harvest_count,
                  self.win.count_vars['pet_catch'], *self.win.pet_fields.values()]
        self.assertEqual({(f.width(), f.height()) for f in fields}, {(84, 26)})
        self.win.resize(820, 780)
        self.win.show()
        self.win.page.set('weekly')
        self.win._show_page()
        self.win.pet_box.show()
        for group in self.win.pet_groups.values():
            group.show()
        self.app.processEvents()
        card = self.win.pet_buy.parentWidget()
        from PySide6.QtCore import QPoint
        positions = [f.mapTo(card, QPoint()).x() for f in fields[:3]]
        self.assertEqual(len(set(positions)), 1)
        for f in fields:
            pos = f.mapTo(card, QPoint())
            self.assertGreaterEqual(pos.x(), 0)
            self.assertLessEqual(pos.x()+f.width(), card.width())
