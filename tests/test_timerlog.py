"""每日定时的执行记录：落盘、只留最近三天。全部离线。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from zmxy import timerlog


class TimerLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "sub" / "timer_log.txt"
        timerlog._pruned = {}

    def put(self, day, detail, hour=20):
        timerlog.write("号 · 存档 1", "联盟药园", "已执行", detail, datetime(2026, 10, day, hour, 30, 5), self.file)

    def test_one_line_per_record(self):
        self.put(8, "采摘 3\n浇灌 2")
        self.assertEqual(self.file.read_text(encoding="utf-8"), "2026-10-08 20:30:05  号 · 存档 1  联盟药园  [已执行]  采摘 3 浇灌 2\n")

    def test_keeps_last_three_days(self):
        for day in (5, 6, 7, 8):
            self.put(day, f"d{day}")
        text = timerlog.read(datetime(2026, 10, 8, 21, 0), self.file)
        self.assertEqual([ln[:10] for ln in text.splitlines()], ["2026-10-06", "2026-10-07", "2026-10-08"])

    def test_read_prunes_even_without_new_writes(self):
        self.put(5, "old")
        self.assertEqual(timerlog.read(datetime(2026, 10, 9, 0, 0), self.file), "")

    def test_missing_file_reads_empty(self):
        self.assertEqual(timerlog.read(file=self.file), "")


if __name__ == "__main__":
    unittest.main()
