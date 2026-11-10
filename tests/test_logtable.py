"""日志表的状态回写：离屏运行，不登录、不执行线上任务。"""

import os
import unittest
from unittest import mock

from zmxy import core
from tests.support import Fake, run

try:
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
except ImportError:  # 命令行测试环境可以不安装界面依赖
    QApplication = LogTable = None
else:
    from zmxy.logtable import LogTable


@unittest.skipIf(LogTable is None, "未安装 PySide6，跳过日志界面测试")
class LogTableTests(unittest.TestCase):
    ACCOUNT = "账号1 · 存档 2"
    TASK = "福禄寿"

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.table = LogTable(lambda row: None)
        self.addCleanup(self.table.close)

    def put(self, status, detail, account=None, task=None):
        self.table.put(account or self.ACCOUNT, task or self.TASK, status, detail)

    def execute_fulushou(self):
        with mock.patch("zmxy.quota.locked", return_value=None):
            result = run("fulushou", Fake(), {"vip": {"level": 0}, "dungeon": {}},
                         {"levels": [200101], "times": 1}, log=lambda text: self.put(core.PROGRESS, text))
        self.put(result.status, result.detail)
        return result

    def test_repeated_fulushou_results_finish_each_runs_row(self):
        first = self.execute_fulushou()
        first_log = list(self.table.trails[0])
        second = self.execute_fulushou()
        self.assertEqual((first.status, second.status), (core.SUBMITTED, core.SUBMITTED))
        self.assertEqual(first.detail, second.detail)
        self.assertEqual([row[3] for row in self.table.rows], [core.SUBMITTED, core.SUBMITTED])
        self.assertEqual(self.table.live, {})
        self.assertEqual(self.table.trails[0], first_log)
        for trail in self.table.trails:
            self.assertEqual([(status, text) for _, status, text in trail],
                             [(core.PROGRESS, "福星 0/1"), (core.PROGRESS, "福星 1/1"), (core.SUBMITTED, first.detail)])

    def test_previous_result_prefix_cannot_steal_current_final(self):
        self.put(core.SUBMITTED, "已结算 福星×1")
        self.put(core.PROGRESS, "福星 10/10")
        self.put(core.SUBMITTED, "已结算 福星×10")
        self.assertEqual([(row[3], row[4]) for row in self.table.rows],
                         [(core.SUBMITTED, "已结算 福星×1"), (core.SUBMITTED, "已结算 福星×10")])
        self.assertFalse(self.table.live)

    def test_repeated_failure_also_finishes_current_row(self):
        for _ in range(2):
            self.put(core.PROGRESS, "正在结算")
            self.put(core.BLOCKED, "结算未确认")
        self.assertEqual([row[3] for row in self.table.rows], [core.BLOCKED, core.BLOCKED])
        self.assertFalse(self.table.live)

    def test_finale_without_active_row_still_merges_and_keeps_only_new_trail_text(self):
        self.put(core.PROGRESS, "挑战 1/1")
        self.put(core.SUBMITTED, "已结算 1 次")
        self.put(core.SUBMITTED, "已结算 1 次；已领取奖励")
        self.assertEqual(len(self.table.rows), 1)
        self.assertEqual(self.table.rows[0][4], "已结算 1 次；已领取奖励")
        self.assertEqual(self.table.trails[0][-1][1:], (core.SUBMITTED, "已领取奖励"))
        self.assertFalse(self.table.live)

    def test_current_result_does_not_finish_another_accounts_row(self):
        other = "账号2 · 存档 2"
        self.execute_fulushou()
        self.put(core.PROGRESS, "福星 1/1", account=other)
        self.execute_fulushou()
        self.assertEqual([row[3] for row in self.table.rows], [core.SUBMITTED, core.PROGRESS, core.SUBMITTED])
        self.assertEqual(self.table.live, {(other, self.TASK): 1})

    def test_repeated_pinned_task_finishes_and_leaves_bottom_group(self):
        for _ in range(2):
            self.put(core.PROGRESS, "竞技场 1/1", task="竞技场")
            self.put(core.SUBMITTED, "已完成 1 次", task="竞技场")
        self.assertEqual([row[3] for row in self.table.rows], [core.SUBMITTED, core.SUBMITTED])
        self.assertFalse(self.table.live)
        self.assertEqual(self.table.view, [0, 1])

    def test_selected_row_stays_visible_when_detail_panel_opens(self):
        host = QWidget()
        table = LogTable(lambda row: None)
        self.addCleanup(host.close)
        self.addCleanup(table.close)
        detail = QWidget()
        detail.setFixedHeight(80)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(table, 1)
        layout.addWidget(detail)
        detail.hide()
        table.on_select = lambda row: detail.setVisible(row is not None)
        for i in range(10):
            table.add(("12:00:00", self.ACCOUNT, f"任务{i}", core.SUBMITTED, "已完成"))

        host.resize(480, 260)
        host.show()
        self.app.processEvents()
        height_before = table.cv.viewport().height()
        table.select(9)
        self.app.processEvents()

        bar = table.cv.verticalScrollBar()
        top, bottom = 9 * table.rh, 10 * table.rh
        self.assertTrue(detail.isVisible())
        self.assertLess(table.cv.viewport().height(), height_before)
        self.assertGreaterEqual(top, bar.value())
        self.assertLessEqual(bottom, bar.value() + table.cv.viewport().height())


if __name__ == "__main__":
    unittest.main()
