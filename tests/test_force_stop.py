"""强制停止：长线任务（斗神大会、灵宠乱斗）运行中点“停止” → 弹窗确认 → 立刻打断。全部离线。"""

import socket
import threading
import time
import unittest

from zmxy import activity
from zmxy import runner as runner_module
from zmxy import core, protocol as p
from zmxy.tasks import boss_raid, pet_brawl, pvp_match  # noqa: F401  (确保已注册)


class LongRunningFlagTests(unittest.TestCase):
    def test_only_the_long_tasks_are_flagged(self):
        flagged = {t.id for t in core.load_tasks().values() if t.long_running}
        self.assertEqual(flagged, {"pvp_match", "pet_brawl"})
        for tid in flagged:
            self.assertTrue(core.load_tasks()[tid].force_warning)  # 确认框里要说清后果


class StopAfterTests(unittest.TestCase):
    """打完当前已进入的战斗后停止整批任务。"""

    def test_stop_after_keeps_the_active_fight_running_then_sets_the_flag(self):
        stop = threading.Event()
        ctx = core.TaskContext(None, stop=stop)
        ident = threading.get_ident()
        with activity._ACTIVE_LOCK:
            activity._ACTIVE[ident], activity._ACTIVE_STOP[ident] = "pvp_match", stop
        ctx.engage(True)
        try:
            self.assertFalse(ctx.winding)
            core.stop_after(stop)
            self.assertTrue(ctx.winding)
            self.assertFalse(ctx.stopped)
            self.assertFalse(core.TaskContext(None).winding)  # 没有停止标志的（测试、读取）不受影响
        finally:
            activity.release(ident, stop)
        self.assertTrue(stop.is_set())

    def test_stop_after_without_an_engaged_fight_stops_immediately(self):
        stop = threading.Event()
        ctx = core.TaskContext(None, stop=stop)
        self.assertFalse(ctx.winding)
        core.stop_after(stop)
        self.assertTrue(ctx.winding)
        self.assertTrue(ctx.stopped)
        self.assertFalse(core.TaskContext(None).winding)  # 没有停止标志的（测试、读取）不受影响

    def test_all_concurrent_fights_finish_before_shared_batch_stops(self):
        stop = threading.Event()
        started = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        threads = []

        def fight(index):
            ident = threading.get_ident()
            with activity._ACTIVE_LOCK:
                activity._ACTIVE[ident] = f"fight_{index}"
                activity._ACTIVE_STOP[ident] = stop
            ctx = core.TaskContext(None, stop=stop)
            ctx.engage(True)
            started[index].set()
            try:
                release[index].wait(2)
            finally:
                activity.release(ident, stop)

        for index in range(2):
            thread = threading.Thread(target=fight, args=(index,), daemon=True)
            threads.append(thread)
            thread.start()
        self.assertTrue(all(event.wait(2) for event in started))
        core.stop_after(stop)
        self.assertFalse(stop.is_set())

        release[0].set()
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())
        self.assertFalse(stop.is_set())  # 另一个账号仍在打这一场

        release[1].set()
        threads[1].join(2)
        self.assertFalse(threads[1].is_alive())
        self.assertTrue(stop.is_set())

    def test_winding_skips_following_tasks_and_finales(self):
        stop = threading.Event()
        account = core.Account("u", p.ClientConfig(uid=1, username="u"), tasks=("first", "second"))
        run = runner_module._AccountRun(account, stop, lambda name, result: None)
        first = core.TaskSpec("first", "first", lambda ctx: None, finale=lambda ctx: None)
        second = core.TaskSpec("second", "second", lambda ctx: None)
        executed = []

        def execute(spec, step, owner=None):
            executed.append(spec.id)
            stop.after = True
            return core.TaskResult(spec.id, core.SUBMITTED, "done"), False

        run.execute = execute
        run.bodies([first, second])

        self.assertEqual(executed, ["first"])
        self.assertEqual([(result.task, result.status) for result in run.results],
                         [("first", core.SUBMITTED), ("second", core.SKIPPED)])

    def test_engaged_lists_only_the_tasks_that_are_in_a_fight(self):
        mine, other = threading.Event(), threading.Event()
        ident = threading.get_ident()
        ctx = core.TaskContext(None, stop=mine)
        activity._ACTIVE[ident], activity._ACTIVE_STOP[ident] = "world_boss", mine
        try:
            self.assertEqual(core.engaged(mine), set())  # 还在等房间 / 匹配
            ctx.engage(True)
            self.assertEqual(core.engaged(mine), {"world_boss"})
            self.assertEqual(core.engaged(other), set())  # 别的账号那次执行不算
            ctx.engage(False)
            self.assertEqual(core.engaged(mine), set())
        finally:
            activity._ACTIVE.pop(ident, None)
            activity._ACTIVE_STOP.pop(ident, None)
            activity._ENGAGED.discard(ident)

    def test_tasks_with_a_consequence_say_so(self):
        specs = core.load_tasks()
        self.assertIn("作废", specs["world_boss"].force_warning)
        self.assertFalse(specs["world_boss"].long_running)  # 世界Boss 自己会响应停止，不用强制断开


class ForceStopTests(unittest.TestCase):
    def test_force_stop_interrupts_a_running_task_thread_even_if_it_swallows_exceptions(self):
        got, started = {}, threading.Event()

        def worker():
            activity._ACTIVE[threading.get_ident()] = "pvp_match"
            started.set()
            try:
                while True:
                    try:
                        time.sleep(0.01)
                    except Exception:  # 任务里的 except Exception 吞不掉 ForceStop（BaseException）
                        pass
            except core.ForceStop:
                got["stopped"] = True
            finally:
                activity._ACTIVE.pop(threading.get_ident(), None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        started.wait(2)
        self.assertEqual(core.active_tasks(), {"pvp_match"})
        stop = threading.Event()
        self.assertEqual(core.force_stop(stop), 1)
        t.join(2)
        self.assertTrue(got.get("stopped"))
        self.assertTrue(stop.is_set())
        self.assertEqual(core.active_tasks(), set())

    def test_threads_without_an_active_task_are_not_targeted(self):
        self.assertEqual(core.force_stop(), 0)

    def test_abort_all_connections_unblocks_a_blocked_recv_at_once(self):
        a, b = socket.socketpair()
        conn = p.PomeloConnection("x", 1, transport=a)
        outcome = {}

        def reader():
            try:
                outcome["data"] = a.recv(10)  # 对端什么都不发：正常会一直阻塞到超时
            except OSError as exc:
                outcome["error"] = exc

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.2)
        began = time.monotonic()
        # transport 是外部传入的（_owns_socket=False）：不动；换成自己拥有的
        conn._owns_socket = True
        self.assertGreaterEqual(p.abort_all_connections(), 1)
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - began, 1.5)
        b.close()


if __name__ == "__main__":
    unittest.main()
