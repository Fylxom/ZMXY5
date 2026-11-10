# 弹窗显示、曝光、恢复和暂停重建后的完整画面提交回归。

import os
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
    from PySide6.QtGui import QColor, QExposeEvent, QPalette, QRegion
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
    from shiboken6 import isValid
    from zmxy import core, widgets
    from zmxy.dialogs import ChoiceDialog, RefineDialog
    from zmxy.refine_dialog import fabao_key
except ImportError:
    QApplication = None


if QApplication is not None:
    class RecordedDialog(widgets.Dialog):
        def __init__(self, parent):
            self.uploads = []
            self.refreshes = []
            super().__init__(parent, "绘制测试")
            self.body.addWidget(widgets.label("测试弹窗"))
            self.chip = widgets.Chip("测试选项", False, lambda: None)
            self.body.addWidget(self.chip)

        def backingStore(self):
            backing = super().backingStore()
            if backing is None:
                return None

            def flush(region, window):
                self.uploads.append(region)
                backing.flush(region, window)

            return SimpleNamespace(size=backing.size, flush=flush)

        def _refresh_surface(self):
            self.refreshes.append(True)
            super()._refresh_surface()


@unittest.skipIf(QApplication is None, "未安装 PySide6")
class DialogPaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        widgets.apply_theme(cls.app)

    def setUp(self):
        self.parent = QWidget()
        self.parent.resize(700, 500)
        self.parent.show()
        self.addCleanup(self.parent.deleteLater)
        self.addCleanup(self.parent.close)
        self.dlg = RecordedDialog(self.parent)
        self.addCleanup(self.dlg.deleteLater)
        self.addCleanup(self.dlg.close)

    def drain(self):
        for _ in range(3):
            self.app.processEvents()

    def show(self):
        self.dlg.show()
        self.drain()
        self.dlg.uploads.clear()
        self.dlg.refreshes.clear()

    def expose(self):
        event = QExposeEvent(QRegion(QRect(0, 0, 2, 2)))
        self.app.sendEvent(self.dlg.windowHandle(), event)

    def assert_full_upload(self):
        self.assertTrue(self.dlg.uploads)
        self.assertTrue(all(region == QRegion(self.dlg.rect()) for region in self.dlg.uploads))

    def test_fabao_selection_key_survives_advance(self):
        old = fabao_key({"entity": "weapon-1", "id": 130080001})
        advanced = fabao_key({"entity": "weapon-1", "id": 130080005})
        self.assertEqual(old, advanced)

    def test_dialog_owns_opaque_white_background_even_with_colored_parent(self):
        palette = self.parent.palette()
        palette.setColor(QPalette.Window, QColor("#ff00ff"))
        self.parent.setPalette(palette)
        self.dlg.show()
        self.drain()
        self.assertEqual(self.dlg.grab().toImage().pixelColor(2, 2).name(), widgets.BG.lower())
        self.assertFalse(self.dlg.testAttribute(Qt.WA_TranslucentBackground))

    def test_show_submits_a_complete_frame(self):
        self.dlg.show()
        self.drain()
        self.assert_full_upload()
        self.assertTrue(self.dlg.chip.isVisible())

    def test_small_exposure_submits_entire_frame_after_event_processing(self):
        self.show()
        native = None
        if self.app.platformName() == "windows":
            # 原生窗口模式额外验证实际屏幕像素；只在独立测试中用 GDI 污染画面，
            # 保留 Qt 内部缓存，模拟系统画面失效而 Qt 只知道局部脏区的情形。
            import ctypes
            from ctypes import wintypes

            QTest.qWait(180)  # 等 Windows 显示动画结束，否则抓到的是上一窗口的过渡画面。
            screen = widgets.hold(self.dlg.screen())
            hwnd = int(self.dlg.winId())
            before = screen.grabWindow(hwnd).toImage()
            self.assertFalse(before.isNull())
            user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
            gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
            gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            dc = user32.GetDC(hwnd)
            brush = gdi32.CreateSolidBrush(0x00FF00FF)
            self.assertTrue(dc)
            self.assertTrue(brush)
            try:
                rect = wintypes.RECT(0, 0, before.width(), before.height())
                self.assertTrue(user32.FillRect(dc, ctypes.byref(rect), brush))
            finally:
                user32.ReleaseDC(hwnd, dc)
                gdi32.DeleteObject(brush)
            self.assertEqual(screen.grabWindow(hwnd).toImage().pixelColor(4, 4).name(), "#ff00ff")
            native = screen, hwnd, before
            self.dlg.uploads.clear()
        self.expose()
        self.assertEqual(self.dlg.uploads, [])
        self.drain()
        self.assert_full_upload()
        if native is not None:
            screen, hwnd, before = native
            QTest.qWait(30)
            self.assertEqual(screen.grabWindow(hwnd).toImage(), before)

    def test_exposure_burst_is_coalesced_and_idle_does_not_poll(self):
        self.show()
        for _ in range(20):
            self.expose()
        self.drain()
        self.assertEqual(len(self.dlg.uploads), 1)
        self.assertEqual(len(self.dlg.refreshes), 1)
        QTest.qWait(40)
        self.assertEqual(len(self.dlg.refreshes), 1)

    def test_disabled_updates_defer_submission_until_reenabled(self):
        self.show()
        self.dlg.setUpdatesEnabled(False)
        self.expose()
        self.drain()
        self.assertEqual(self.dlg.uploads, [])
        self.dlg.setUpdatesEnabled(True)
        self.drain()
        self.assert_full_upload()

    def test_close_before_queued_refresh_does_not_submit(self):
        self.show()
        self.expose()
        self.dlg.close()
        self.drain()
        self.assertEqual(self.dlg.uploads, [])

    def test_deleted_dialog_cancels_queued_refresh(self):
        dlg = RecordedDialog(None)
        calls = dlg.refreshes
        dlg.show()
        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.drain()
        self.assertFalse(isValid(dlg))
        self.assertEqual(calls, [])

    def test_hover_only_repaints_child_without_full_submission(self):
        self.show()
        self.app.sendEvent(self.dlg.chip, QEvent(QEvent.Enter))
        self.drain()
        self.assertEqual(self.dlg.uploads, [])
        self.assertEqual(self.dlg.refreshes, [])

    def test_activation_and_minimize_restore_submit_complete_frames(self):
        self.show()
        self.app.sendEvent(self.dlg, QEvent(QEvent.WindowActivate))
        self.drain()
        self.assert_full_upload()
        self.dlg.showMinimized()
        self.drain()
        self.dlg.uploads.clear()
        self.dlg.showNormal()
        self.drain()
        self.assert_full_upload()

    def test_world_boss_choice_search_selection_and_reopen(self):
        spec = core.load_tasks()["world_boss"]
        self.assertIn(("选存档", "choose_world_boss_save"), spec.choices.actions)
        cfg = {"profile": {}, "options": {}}
        self.parent.store = SimpleNamespace(save=lambda: None)
        for _ in range(3):
            dlg = ChoiceDialog(self.parent, spec, {}, 1, cfg)
            dlg.show()
            self.drain()
            save_button = next(b for b in dlg.findChildren(widgets.RButton) if b.text == "选存档")
            self.assertGreater(save_button.y(), dlg.mode.geometry().bottom())
            self.assertTrue(dlg.items)
            value, name = dlg.items[0]
            dlg.toggle(value)
            dlg.query.setText(name)
            self.drain()
            self.assertIn(value, dlg.shown)
            self.assertEqual(dlg.grab().toImage().pixelColor(2, 2).name(), widgets.BG.lower())
            dlg.save()
            self.assertTrue(dlg.saved)
            self.assertIn(value, cfg["options"][spec.id][spec.choices.option])
            dlg.deleteLater()
            # 每次重开都取消刚才的选择，让下轮仍覆盖点选和保存。
            cfg["options"][spec.id][spec.choices.option] = []

    def test_refine_rebuild_failure_restores_updates(self):
        dlg = RefineDialog(self.parent, {}, 1, {})
        self.addCleanup(dlg.deleteLater)
        self.addCleanup(dlg.close)
        with mock.patch.object(dlg.pages["feather"], "_wing", side_effect=RuntimeError("重建失败")):
            with self.assertRaises(RuntimeError):
                dlg.pages["feather"]._pick_wing()
        self.assertTrue(dlg.updatesEnabled())

    def test_refine_nested_rebuild_preserves_outer_update_pause(self):
        dlg = RefineDialog(self.parent, {}, 1, {})
        self.addCleanup(dlg.deleteLater)
        self.addCleanup(dlg.close)
        dlg.setUpdatesEnabled(False)
        dlg.pages["feather"]._pick_wing()
        self.assertFalse(dlg.updatesEnabled())
        dlg.setUpdatesEnabled(True)


if __name__ == "__main__":
    unittest.main()
