import unittest
import tempfile
from pathlib import Path
from unittest import mock

from zmxy import runner as runner_module
from zmxy import core, protocol as p
from zmxy.core import Account, TaskResult
from zmxy.store import Store


class PendingTests(unittest.TestCase):
    def test_pending_lists_paused_and_unrun_tasks_per_save(self):
        a = Account("a · 存档 1", p.ClientConfig(username="a"), ("dungeon_main", "dungeon_hidden", "dungeon_elite"))
        b = Account("b · 存档 1", p.ClientConfig(username="b"), ("dungeon_main",))
        c = Account("c · 存档 1", p.ClientConfig(username="c"), ("dungeon_main",))
        results = {a.name: [TaskResult("dungeon_main", "submitted"), TaskResult("dungeon_hidden", core.PAUSED)],
                   }
        got = core.pending([a, b, c], results)
        self.assertEqual(got, {a.name: ["dungeon_hidden", "dungeon_elite"], b.name: ["dungeon_main"], c.name: ["dungeon_main"]})

    def test_saves_halted_for_other_reasons_are_not_resumed(self):
        a = Account("a · 存档 1", p.ClientConfig(username="a"), ("dungeon_main", "dungeon_hidden"))
        b = Account("b · 存档 1", p.ClientConfig(username="b"), ("dungeon_main", "dungeon_hidden"))   # 结果未知：后面的任务没跑
        c = Account("c · 存档 1", p.ClientConfig(username="c"), ("dungeon_main",))                    # 登录失败：一个都没跑
        results = {a.name: [TaskResult("dungeon_main", core.PAUSED)],
                   b.name: [TaskResult("dungeon_main", core.UNCERTAIN)],
                   c.name: [TaskResult("login", core.ERROR)]}
        self.assertEqual(core.pending([a, b, c], results), {a.name: ["dungeon_main", "dungeon_hidden"]})

    def test_finished_save_has_no_breakpoint(self):
        a = Account("a · 存档 1", p.ClientConfig(username="a"), ("dungeon_main",))
        self.assertEqual(core.pending([a], {a.name: [TaskResult("dungeon_main", "submitted")]}), {})

    def test_completed_arena_tasks_are_not_requeued_on_later_pause(self):
        a = Account("a", p.ClientConfig(username="a"), ("arena", "pet_arena", "dungeon_main"))
        results = {a.name: [TaskResult("arena", core.SUBMITTED, "已挑战5/5次"),
                            TaskResult("pet_arena", core.SUBMITTED, "已挑战5/5次"),
                            TaskResult("dungeon_main", core.PAUSED)]}
        self.assertEqual(core.pending([a], results), {a.name: ["dungeon_main"]})

    def test_skipped_or_blocked_arena_final_results_are_not_retried(self):
        a = Account("a", p.ClientConfig(username="a"), ("arena", "pet_arena", "dungeon_main"))
        results = {a.name: [TaskResult("arena", core.SKIPPED, "免费次数已用完"),
                            TaskResult("pet_arena", core.BLOCKED, "没有可挑战目标"),
                            TaskResult("dungeon_main", core.PAUSED)]}
        self.assertEqual(core.pending([a], results), {a.name: ["dungeon_main"]})

    def test_unfinished_interleaved_job_survives_settle_and_resumes(self):
        for tid in ("arena", "pet_arena", "tongtian_escort"):
            with self.subTest(tid=tid):
                a = Account("a", p.ClientConfig(username="a"), (tid, "dungeon_main"))
                runner = runner_module._AccountRun(a, None, lambda *args: None)
                runner.chosen = a.tasks
                runner.results = [TaskResult(tid, core.SKIPPED, "进行中", progress={"1": 2}, pending=True),
                                  TaskResult("dungeon_main", core.PAUSED)]
                runner.settle()
                self.assertTrue(runner.results[0].pending)
                self.assertEqual(runner.results[0].progress, {"1": 2})
                self.assertEqual(set(core.pending([a], {a.name: runner.results})[a.name]), set(a.tasks))

    def test_reported_arena_completion_replaces_placeholder_before_breakpoint(self):
        a = Account("a", p.ClientConfig(username="a"), ("arena", "pet_arena", "dungeon_main"))
        runner = runner_module._AccountRun(a, None, lambda *args: None)
        runner.chosen = a.tasks
        runner.results = [TaskResult("arena", core.SKIPPED, pending=True),
                          TaskResult("dungeon_main", core.PAUSED)]
        for tid in ("arena", "pet_arena"):
            runner.show(a.name, TaskResult(tid, core.SUBMITTED, "已挑战5/5次"), True)
        runner.settle()
        self.assertEqual(core.pending([a], {a.name: runner.results}), {a.name: ["dungeon_main"]})

    def test_saved_resume_round_does_not_execute_completed_arena_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "settings.json")
            acc = {"username": "a", "saves": {"1": {"tasks": ["arena", "pet_arena", "dungeon_main"]}}}
            store.accounts.append(acc)
            a = Account(store.slot_name("a", 1), p.ClientConfig(username="a"), tuple(acc["saves"]["1"]["tasks"]))
            runner = runner_module._AccountRun(a, None, lambda *args: None)
            runner.chosen = a.tasks
            runner.results = [TaskResult(t, core.SUBMITTED) for t in ("arena", "pet_arena")]
            runner.results.append(TaskResult("dungeon_main", core.PAUSED))
            runner.settle()
            store.add_resume(acc, 1, core.pending([a], {a.name: runner.results})[a.name])
            store.save()
            restored = Store(store.path)
            with mock.patch.object(restored, "to_account", return_value=a):
                resumed = restored.resume_accounts({}, lambda acc: None)
            self.assertEqual([x.tasks for x in resumed], [("dungeon_main",)])

    def test_completed_escort_is_not_requeued_either(self):
        a = Account("a", p.ClientConfig(username="a"), ("tongtian_escort", "dungeon_main"))
        results = {a.name: [TaskResult("tongtian_escort", core.SUBMITTED), TaskResult("dungeon_main", core.PAUSED)]}
        self.assertEqual(core.pending([a], results), {a.name: ["dungeon_main"]})


if __name__ == "__main__":
    unittest.main()


class OrderTests(unittest.TestCase):
    def test_task_order_keeps_first_last_terminal_with_account_wide_priority(self):
        a = Account("a", None, ("pet_elf", "shanfa_like", "double_drop", "growth_road", "online_welfare"))
        self.assertEqual(core.task_order(a), ["double_drop", "shanfa_like", "pet_elf", "growth_road", "online_welfare"])

    def test_growth_road_and_limited_quest_run_last_daily_quest_claims_in_run(self):
        a = Account("a", None, ("growth_road", "daily_quest", "limited_quest", "dungeon_main", "pet_elf"))
        order = core.task_order(a)
        self.assertEqual(set(order[-2:]), {"growth_road", "limited_quest"})
        self.assertEqual(order, sorted(order[:-2], key=core.display_rank) + order[-2:])
        self.assertIsNone(core.load_tasks()["daily_quest"].finale)  # 打完勾选的关当场领，不等别的任务
