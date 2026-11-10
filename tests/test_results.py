"""任务结果写回设置（results.py，界面和命令行共用）。全部离线，设置文件放临时目录。"""

import tempfile
import unittest
from pathlib import Path

from zmxy import results, store
from zmxy.core import PAUSED, PROGRESS, SUBMITTED, TaskResult, load_tasks
from zmxy.tasks import chapter_role, last_rewards as lr, time_boat


class SaveResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store.Store(Path(self.tmp.name) / "settings.json")
        self.acc = {"username": "u", "saves": {"1": {"tasks": [chapter_role.ID, "dungeon_main"]}}}
        self.where = (self.acc, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, result):
        return results.save_result(self.store, self.where, result, load_tasks())

    def profile(self):
        return self.acc["saves"]["1"].get("profile", {})

    def test_progress_line_without_data_changes_nothing(self):
        self.assertFalse(self.save(TaskResult("dungeon_main", PROGRESS, "第 1 关")))
        self.assertEqual(self.acc["saves"]["1"], {"tasks": [chapter_role.ID, "dungeon_main"]})

    def test_reward_outcome_merges_within_the_week_and_keeps_known_results(self):
        self.save(TaskResult(lr.ID, SUBMITTED, profile={lr.OUT_KEY: {"week": "2026-45", "竞技场": lr.GOT}}))
        self.save(TaskResult(lr.ID, SUBMITTED, profile={lr.OUT_KEY: {"week": "2026-45", "竞技场": lr.CLAIMED, "斗宠": lr.NONE}}))
        self.assertEqual(self.profile()[lr.OUT_KEY], {"week": "2026-45", "竞技场": lr.GOT, "斗宠": lr.NONE})
        self.save(TaskResult(lr.ID, SUBMITTED, profile={lr.OUT_KEY: {"week": "2026-46", "斗宠": lr.CLAIMED}}))
        self.assertEqual(self.profile()[lr.OUT_KEY], {"week": "2026-46", "斗宠": lr.CLAIMED})  # 换周了：旧的不留

    def test_finished_activity_switches_off_only_this_save(self):
        self.assertTrue(self.save(TaskResult(chapter_role.ID, SUBMITTED, profile={chapter_role.END: True})))
        self.assertEqual(self.acc["saves"]["1"]["tasks"], ["dungeon_main"])

    def test_pause_keeps_progress_and_account_profile(self):
        self.assertTrue(self.save(TaskResult("dungeon_main", PAUSED, progress={"51": 2}, account_profile={"x": 1})))
        self.assertEqual(self.acc["saves"]["1"]["resume_done"], {"dungeon_main": {"51": 2}})
        self.assertEqual(self.store.account_profile("u"), {"x": 1})

    def test_time_boat_snapshot_goes_through_its_own_merge(self):
        calls = []
        real = time_boat.merge_profile
        time_boat.merge_profile = lambda new, old: calls.append((new, old)) or {"merged": True}
        try:
            self.acc["saves"]["1"]["profile"] = {time_boat.PROFILE_KEY: {"old": 1}}
            self.save(TaskResult(time_boat.ID, SUBMITTED, profile={time_boat.PROFILE_KEY: {"new": 1}}))
        finally:
            time_boat.merge_profile = real
        self.assertEqual(calls, [({"new": 1}, {"old": 1})])
        self.assertEqual(self.profile()[time_boat.PROFILE_KEY], {"merged": True})


if __name__ == "__main__":
    unittest.main()
