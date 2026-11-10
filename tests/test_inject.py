"""执行中插入任务（护送经书等船时别的任务插进来做）和按次执行的强制停止。全部离线。"""

import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

from zmxy import activity
from zmxy import session as session_module
from zmxy import core, protocol as p
from zmxy.core import Account


def account(tasks, slot=1, options=None):
    return Account("n", replace(p.ClientConfig(uid=1, username="u"), record_index=slot - 1), tasks=tuple(tasks), options=options or {})


class InjectTests(unittest.TestCase):
    def go(self, specs, tasks, stop=None):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake), \
                mock.patch("zmxy.core.TaskContext.sleep", lambda ctx, s, background=True: (self.idle.append(core.hosting("u")), True)[1]):
            return core.run_account(account(tasks), stop=stop)

    def setUp(self):
        self.idle = []  # 每次空等时界面看到的“能不能插任务”

    def test_task_injected_during_the_wait_runs_with_its_own_options(self):
        seen = []

        def host(ctx):
            self.assertIsNone(core.hosting("u"))  # 自己在做事：界面锁着
            self.assertIsNone(core.inject(account(["b"], options={"b": {"times": 3}})))
            self.assertEqual(core.inject(account(["b"])), "任务已经在执行或排队中")
            self.assertEqual(core.inject(account(["host"])), "任务已经在执行或排队中")
            seen.append("wait")
            self.assertTrue(ctx.wait(5))
            seen.append(("back", core.active_tasks()))
            return core.TaskResult("host", "submitted")

        def other(ctx):
            seen.append(("b", dict(ctx.options), core.active_tasks()))
            self.assertIsNone(core.hosting("u"))  # 插进来的任务在做：界面锁着，做完才放开
            ctx.wait(1)  # 插进来的任务里不再嵌套
            return core.TaskResult("b", "submitted")

        specs = {"host": core.TaskSpec("host", "host", host, hosts=True), "b": core.TaskSpec("b", "b", other)}
        res = self.go(specs, ["host"])
        self.assertEqual(seen, ["wait", ("b", {"times": 3}, {"b"}), ("back", {"host"})])
        self.assertEqual([(r.task, r.status) for r in res], [("b", "submitted"), ("host", "submitted")])
        self.assertEqual((self.idle[0], set(self.idle[1:])), (None, {1}))  # 插进来的任务自己等的时候不行；宿主空等着的时候才能插
        self.assertIsNone(core.hosting("u"))  # 执行结束：不再接收
        self.assertEqual(core.inject(account(["b"])), "正在执行的任务已经结束")

    def test_task_of_another_save_switches_saves_and_reports_under_its_own_name(self):
        from contextlib import contextmanager
        fake, other, seen, emitted = mock.MagicMock(), mock.MagicMock(), [], []
        fake.__enter__.return_value = fake

        @contextmanager
        def other_save(slot):
            seen.append(("换到", slot))
            yield other
            seen.append("换回")

        fake.other_save = other_save

        def host(ctx):
            self.assertIsNone(core.inject(replace(account(["b"], slot=2), name="存档2")))
            ctx.wait(1)
            seen.append(("host", ctx.session is fake))
            return core.TaskResult("host", "submitted")

        def b(ctx):
            seen.append(("b", ctx.session is other))
            return core.TaskResult("b", "submitted")

        specs = {"host": core.TaskSpec("host", "host", host, hosts=True), "b": core.TaskSpec("b", "b", b)}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake),                 mock.patch("zmxy.core.TaskContext.sleep", lambda self, s, background=True: True):
            core.run_account(account(["host"]), emit=lambda name, r: emitted.append((name, r.task)))
        self.assertEqual(seen, [("换到", 2), ("b", True), "换回", ("host", True)])
        self.assertEqual(emitted, [("存档2", "b"), ("n", "host")])

    def test_task_injected_late_still_runs_before_the_run_closes(self):
        def host(ctx):
            core.inject(account(["b"]))
            return core.TaskResult("host", "submitted")

        specs = {"host": core.TaskSpec("host", "host", host, hosts=True),
                 "b": core.TaskSpec("b", "b", lambda ctx: core.TaskResult("b", "paused", "背包满"))}
        res = self.go(specs, ["host"])
        self.assertEqual([(r.task, r.status) for r in res], [("host", "submitted"), ("b", "blocked")])  # 插入的任务不进断点

    def test_only_hosting_runs_accept_and_stop_skips_the_queue(self):
        stop = threading.Event()

        def plain(ctx):
            self.assertIsNone(core.hosting("u"))
            self.assertIsNotNone(core.inject(account(["b"])))
            return core.TaskResult("a", "submitted")

        def host(ctx):
            core.inject(account(["b"]))
            stop.set()
            return core.TaskResult("host", "submitted")

        specs = {"a": core.TaskSpec("a", "a", plain), "host": core.TaskSpec("host", "host", host, hosts=True),
                 "b": core.TaskSpec("b", "b", lambda ctx: self.fail("停止后不该执行"))}
        self.assertEqual([r.status for r in self.go(specs, ["a"])], ["submitted"])
        res = self.go(specs, ["host"], stop=stop)
        self.assertEqual([(r.task, r.status) for r in res], [("host", "submitted"), ("b", "skipped")])

    def test_injected_uncertainty_stops_host_and_all_queued_writes(self):
        for phase in ("prelude", "run", "finale"):
            for slot in (1, 2):
                with self.subTest(phase=phase, slot=slot):
                    seen = []

                    def host(ctx):
                        core.inject(account(["b", "c"], slot=slot))
                        core.inject(account(["d"]))
                        ctx.wait(1)
                        seen.append("host continued")
                        return core.TaskResult("host", core.SUBMITTED)

                    def unsure(ctx):
                        seen.append("uncertain")
                        raise core.UncertainOutcome("结算超时")

                    def completed(ctx):
                        return core.TaskResult("b", core.SUBMITTED)

                    b = core.TaskSpec("b", "b", unsure if phase == "run" else completed,
                                      prelude=unsure if phase == "prelude" else None,
                                      finale=unsure if phase == "finale" else None)
                    specs = {"host": core.TaskSpec("host", "host", host, hosts=True), "b": b,
                             **{tid: core.TaskSpec(tid, tid, lambda ctx: self.fail("结果未知后不应再发请求")) for tid in ("c", "d")}}
                    result = self.go(specs, ["host"])
                    self.assertEqual(seen, ["uncertain"])
                    self.assertEqual(next(r.status for r in result if r.task == "host"), core.UNCERTAIN)
                    self.assertTrue(all(r.status == core.SKIPPED for r in result if r.task in ("c", "d")))

    def test_uncertain_host_does_not_drain_injected_queue_during_close(self):
        def host(ctx):
            core.inject(account(["b"]))
            raise core.UncertainOutcome("宿主结算结果未知")

        other = mock.Mock(return_value=core.TaskResult("b", core.SUBMITTED))
        specs = {"host": core.TaskSpec("host", "host", host, hosts=True), "b": core.TaskSpec("b", "b", other)}
        result = self.go(specs, ["host"])
        other.assert_not_called()
        self.assertEqual([(r.task, r.status) for r in result], [("host", core.UNCERTAIN), ("b", core.SKIPPED)])

    def test_late_injected_uncertainty_skips_host_finale(self):
        def host(ctx):
            core.inject(account(["b"]))
            return core.TaskResult("host", core.SUBMITTED)

        finale = mock.Mock(return_value=None)
        specs = {"host": core.TaskSpec("host", "host", host, hosts=True, finale=finale),
                 "b": core.TaskSpec("b", "b", lambda ctx: core.TaskResult("b", core.UNCERTAIN, "结算未确认"))}
        result = self.go(specs, ["host"])
        finale.assert_not_called()
        self.assertEqual([(r.task, r.status) for r in result], [("host", core.SUBMITTED), ("b", core.UNCERTAIN)])


class LendLoginTests(unittest.TestCase):
    """师徒试炼要登录的账号正在跑自己的任务：它做完手上的任务就把登录让出来，对方用完再登录回去。"""

    def session(self, name):
        s = core.Session(p.ClientConfig(uid=1, username=name))
        s._conn, s.owns_login = mock.MagicMock(), True
        s.peers = lambda user, slot: Account(user, p.ClientConfig(uid=2, username=user))
        self.assertTrue(core.login_lock(name).acquire(timeout=1))  # 像 run_account 那样拿着自己的登录锁
        self.addCleanup(lambda: core.login_lock(name).locked() and core.login_lock(name).release())
        return s

    def test_busy_account_lends_its_login_between_tasks_and_takes_it_back(self):
        a, b = self.session("lend_a"), self.session("lend_b")
        order, inside, leave = [], threading.Event(), threading.Event()

        def trial():  # a 的师徒试炼：要登录 b
            with a.open_peer("lend_b", 1):
                order.append("a 用上了 b")
                inside.set()
                leave.wait(2)
            order.append("a 用完")

        other = mock.MagicMock()
        other.__enter__.return_value = other
        with mock.patch.object(core.Session, "_reconnect", lambda self: order.append("b 重新登录")), mock.patch("zmxy.session.Session", return_value=other):
            self.assertFalse(b.lend())  # 没人等：不动
            t = threading.Thread(target=trial, daemon=True)
            t.start()
            while not core.wanted("lend_b"):
                time.sleep(0.01)
            self.assertFalse(inside.is_set())  # b 还占着：a 在等
            threading.Timer(0.2, leave.set).start()
            self.assertTrue(b.lend())  # b 做完手上的任务：让出去，等 a 用完再回来
            t.join(2)
        self.assertEqual(order, ["a 用上了 b", "a 用完", "b 重新登录"])
        self.assertTrue(core.login_lock("lend_b").locked())
        self.assertFalse(core.wanted("lend_b"))

    def test_mutual_wait_is_broken_by_lending_first(self):
        """b 的师徒试炼要登录 a，而 a 正等着登录 b：b 先把自己让给 a，再去等 a。"""
        b = self.session("lend_d")
        session_module._WANTS["lend_c"] = "lend_d"
        self.addCleanup(session_module._WANTS.pop, "lend_c", None)
        lent = []

        def lend(self):
            lent.append(self.config.username)
            session_module._WANTS.pop("lend_c", None)  # 对方拿到了
            return True

        other = mock.MagicMock()
        other.__enter__.return_value = other
        with mock.patch.object(core.Session, "lend", lend), mock.patch("zmxy.session.Session", return_value=other):
            with b.open_peer("lend_c", 1):
                self.assertEqual(lent, ["lend_d"])
        self.assertFalse(core.wanted("lend_c"))

    def test_run_lends_between_tasks(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        seen = []

        def first(ctx):
            session_module._WANTS["x"] = "u"  # 这时别的账号开始等本账号
            fake.lend.side_effect = lambda: (seen.append("lend"), session_module._WANTS.pop("x", None), True)[2]
            return core.TaskResult("a", "submitted")

        specs = {"a": core.TaskSpec("a", "a", first), "b": core.TaskSpec("b", "b", lambda ctx: (seen.append("b"), core.TaskResult("b", "submitted"))[1])}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            res = core.run_account(account(["a", "b"]))
        self.assertEqual(seen, ["lend", "b"])
        self.assertEqual([r.status for r in res], ["submitted", "submitted"])


class ScopedForceStopTests(unittest.TestCase):
    def test_force_stop_only_hits_the_run_with_that_stop_flag(self):
        mine, other = threading.Event(), threading.Event()
        hit, ready = {}, []

        def worker(name, stop):
            ident = threading.get_ident()
            activity._ACTIVE[ident], activity._ACTIVE_STOP[ident] = name, stop
            ready.append(name)
            try:
                while not hit.get("quit"):
                    time.sleep(0.01)
            except core.ForceStop:
                hit[name] = True
            finally:
                activity._ACTIVE.pop(ident, None)
                activity._ACTIVE_STOP.pop(ident, None)

        threads = [threading.Thread(target=worker, args=a, daemon=True) for a in (("a", mine), ("b", other))]
        for t in threads:
            t.start()
        while len(ready) < 2:
            time.sleep(0.01)
        self.assertEqual(core.active_tasks(mine), {"a"})
        self.assertEqual(core.force_stop(mine), 1)
        threads[0].join(2)
        self.assertEqual((hit.get("a"), hit.get("b"), other.is_set()), (True, None, False))
        hit["quit"] = True
        threads[1].join(2)

    def test_abort_connections_can_be_limited_to_owner_threads(self):
        import socket
        a, b = socket.socketpair()
        conn = p.PomeloConnection("x", 1, transport=a)
        conn._owns_socket = True
        self.assertEqual(p.abort_all_connections([conn._owner + 1]), 0)  # 别的线程的连接：不动
        a.send(b"x")
        self.assertEqual(b.recv(1), b"x")
        self.assertEqual(p.abort_all_connections([conn._owner]), 1)
        b.close()


if __name__ == "__main__":
    unittest.main()
