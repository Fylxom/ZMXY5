import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from zmxy import runner as runner_module
from dataclasses import replace
from zmxy import core, protocol as p, state
from zmxy.core import Account, Session, TaskContext, UncertainOutcome, GuardViolation, load_tasks
from zmxy.tasks import vip_gift, alliance
from tests.support import FakeConn, ts


def make(spec_id, conn, info=None, options=None):
    s = Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s.info = conn, info or {}
    spec = load_tasks()[spec_id]
    s.bind(spec)
    return spec, TaskContext(s, options or {})


class GuardTests(unittest.TestCase):
    def test_undeclared_route_rejected(self):
        conn = FakeConn()
        _, ctx = make("vip_gift", conn)
        with self.assertRaises(GuardViolation):
            ctx.request("scene.shopHandler.buy", {})
        self.assertEqual(conn.sent, [])

    def test_write_timeout_is_uncertain_and_not_retried(self):
        conn = FakeConn(exc=TimeoutError("t"))
        _, ctx = make("vip_gift", conn)
        with self.assertRaises(UncertainOutcome):
            ctx.request(vip_gift.ROUTE_VIP_GIFT, {"giftId": 3})
        self.assertEqual(len(conn.sent), 1)

    def test_registry_has_tasks(self):
        self.assertEqual({"alliance", "vip_gift"} <= set(load_tasks()), True)


class VipGiftTests(unittest.TestCase):
    def test_no_vip(self):
        conn = FakeConn()
        spec, ctx = make("vip_gift", conn, {"vip": {"level": 0}})
        self.assertEqual(spec.run(ctx).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_unreadable_vip_blocks(self):
        conn = FakeConn()
        spec, ctx = make("vip_gift", conn, {})
        self.assertEqual(spec.run(ctx).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_submit_sends_level_gift(self):  # VipMediator：等级礼包 vipGiftBagId 没领先领它，领过才领每日的 giftBagId
        conn = FakeConn()
        spec, ctx = make("vip_gift", conn, {"vip": {"level": 5}})
        self.assertEqual(spec.run(ctx).status, "submitted")
        self.assertEqual(conn.sent, [(vip_gift.ROUTE_VIP_GIFT, {"giftId": 12})])

    def test_daily_gift_follows_the_claimed_level_gift(self):
        conn = FakeConn()
        spec, ctx = make("vip_gift", conn, {"vip": {"level": 5}, "gift": {"12": 1}})
        self.assertEqual(spec.run(ctx).status, "submitted")
        self.assertEqual(conn.sent, [(vip_gift.ROUTE_VIP_GIFT, {"giftId": 5})])

    def test_gift_id_must_not_become_claim_flag(self):
        self.assertIsNone(vip_gift.claim_state({"vip": {"level": 1, "vipGiftBagId": 1}}))


class AllianceTests(unittest.TestCase):
    def info(self, count, spirit=5000):
        return {"spirit": spirit, "league": {"leagueId": 7, "leagueDevote": {"1": count}}}

    def context(self, info=None, now=ts(2026, 10, 24), responses=None):
        clock = SimpleNamespace(server=now, elapsed=0)

        class ServerSession(Session):
            @property
            def now(self):
                return int(clock.server) if clock.server is not None else None

        session = ServerSession(p.ClientConfig(uid=1, username="u"))
        conn = FakeConn(responses or {p.ROUTE_DEVOTE: {"self": self.info(1)["league"]}})
        session._conn, session.info = conn, self.info(0) if info is None else info
        session.bind(load_tasks()["alliance"])
        ctx = TaskContext(session, log=mock.Mock())
        ctx.refresh = mock.Mock(side_effect=lambda: session.info)

        def sleep(seconds):
            clock.elapsed += seconds
            if clock.server is not None:
                clock.server += seconds
            return True

        ctx.sleep = mock.Mock(side_effect=sleep)
        patch = mock.patch.object(alliance, "time", SimpleNamespace(monotonic=lambda: clock.elapsed))
        patch.start()
        self.addCleanup(patch.stop)
        return ctx, conn, clock

    def test_missing_count_blocks_instead_of_assuming_zero(self):
        ctx, conn, _clock = self.context({"league": {"leagueId": 7}})
        with mock.patch.object(state, "league", return_value={"leagueId": 7}), \
                mock.patch.object(state, "league_daily_count", return_value=None):
            self.assertEqual(alliance.run(ctx).status, "blocked")
        self.assertNotIn(p.ROUTE_DEVOTE, conn.routes())

    def test_server_clock_controls_midnight_wait_and_refreshes_yesterdays_count(self):
        ctx, conn, clock = self.context(self.info(1), now=ts(2026, 10, 24, 0) + 14 * 60 + 59)

        def refresh():
            ctx.session.info = self.info(0 if clock.elapsed else 1)
            return ctx.info

        ctx.refresh.side_effect = refresh
        result = alliance.run(ctx)
        self.assertEqual(result.status, core.SUBMITTED)
        ctx.sleep.assert_called_once_with(31)
        self.assertEqual(ctx.refresh.call_count, 2)
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)
        self.assertIn("服务器 00:15:30", result.detail)
        self.assertIn("本地", result.detail)
        self.assertIn("服务器－本地", result.detail)

    def test_missing_server_time_does_not_use_local_time_or_submit(self):
        ctx, conn, _clock = self.context(now=None)
        result = alliance.run(ctx)
        self.assertEqual(result.status, core.BLOCKED)
        self.assertIn("服务器未返回时间", result.detail)
        self.assertEqual(conn.sent, [])

    def test_server_ready_boundary_and_normal_day_do_not_wait(self):
        for now in (ts(2026, 10, 24, 0) + 15 * 60 + 30, ts(2026, 10, 24)):
            with self.subTest(now=now):
                ctx, conn, _clock = self.context(now=now)
                self.assertEqual(alliance.run(ctx).status, core.SUBMITTED)
                ctx.sleep.assert_not_called()
                ctx.refresh.assert_called_once()
                self.assertEqual(conn.bodies(p.ROUTE_DEVOTE), [{"devoteId": 1, "password": ""}])

    def test_refreshed_completed_state_prevents_submit(self):
        ctx, conn, _clock = self.context()

        def refresh():
            ctx.session.info = self.info(1)
            return ctx.info

        ctx.refresh.side_effect = refresh
        self.assertEqual(alliance.run(ctx).status, core.SKIPPED)
        self.assertEqual(conn.sent, [])

    def test_explicit_settlement_refusal_waits_then_refreshes_and_submits(self):
        replies = iter(({"code": 500, "error": "联盟数据处理中，请在0点15分后再操作"},
                        {"self": self.info(1)["league"]}))
        ctx, conn, _clock = self.context(responses={p.ROUTE_DEVOTE: lambda body: next(replies)})
        self.assertEqual(alliance.run(ctx).status, core.SUBMITTED)
        self.assertEqual(ctx.refresh.call_count, 2)
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 2)
        ctx.sleep.assert_called_once_with(30)

    def test_settlement_wait_does_not_replay_after_fresh_state_is_completed(self):
        ctx, conn, clock = self.context(responses={p.ROUTE_DEVOTE: {"code": 500, "error": "联盟数据处理中"}})

        def refresh():
            ctx.session.info = self.info(1 if clock.elapsed else 0)
            return ctx.info

        ctx.refresh.side_effect = refresh
        self.assertEqual(alliance.run(ctx).status, core.SKIPPED)
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)
        ctx.sleep.assert_called_once_with(30)

    def test_league_read_settlement_refusal_waits_instead_of_trying_three_bodies(self):
        ctx, conn, clock = self.context({"spirit": 5000, "league": {"leagueId": 7}}, responses={
            p.ROUTE_LEAGUE_INFO: {"code": 500, "error": "联盟数据处理中"},
            p.ROUTE_DEVOTE: {"self": self.info(1)["league"]},
        })

        def refresh():
            if clock.elapsed:
                ctx.session.info = self.info(0)
            return ctx.info

        ctx.refresh.side_effect = refresh
        self.assertEqual(alliance.run(ctx).status, core.SUBMITTED)
        self.assertEqual(len(conn.bodies(p.ROUTE_LEAGUE_INFO)), 1)
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)
        ctx.sleep.assert_called_once_with(30)

    def test_permanent_settlement_refusal_stops_after_five_minutes(self):
        ctx, conn, clock = self.context(responses={p.ROUTE_DEVOTE: {"code": 500, "error": "联盟数据处理中"}})
        result = alliance.run(ctx)
        self.assertEqual(result.status, core.BLOCKED)
        self.assertIn("5 分钟", result.detail)
        self.assertEqual(clock.elapsed, 300)
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 10)

    def test_user_stop_or_connection_loss_while_waiting_never_resubmits(self):
        for user_stop in (False, True):
            with self.subTest(user_stop=user_stop):
                ctx, conn, _clock = self.context(responses={p.ROUTE_DEVOTE: {"code": 500, "error": "联盟数据处理中"}})
                ctx.stop = threading.Event()

                def stop_wait(seconds):
                    if user_stop:
                        ctx.stop.set()
                    return False

                ctx.sleep.side_effect = stop_wait
                result = alliance.run(ctx)
                self.assertEqual(result.status, core.SKIPPED if user_stop else core.BLOCKED)
                self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)
                ctx.refresh.assert_called_once()

    def test_other_server_errors_and_write_timeouts_are_never_retried(self):
        ctx, conn, _clock = self.context(responses={p.ROUTE_DEVOTE: {"code": 500, "error": "其他服务器错误"}})
        with self.assertRaises(p.AllianceError):
            alliance.run(ctx)
        ctx.sleep.assert_not_called()
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)

        ctx, conn, _clock = self.context()
        conn.exc = p.AllianceError("等待响应超时")
        with self.assertRaises(UncertainOutcome):
            alliance.run(ctx)
        ctx.sleep.assert_not_called()
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)

    def test_success_without_count_or_unchanged_count_is_not_retried(self):
        for reply, expected in (({}, core.SUBMITTED), ({"self": self.info(0)["league"]}, core.BLOCKED)):
            with self.subTest(reply=reply):
                ctx, conn, _clock = self.context(responses={p.ROUTE_DEVOTE: reply})
                self.assertEqual(alliance.run(ctx).status, expected)
                self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)
                ctx.sleep.assert_not_called()

    def test_server_day_change_during_league_read_requires_new_snapshot(self):
        ctx, conn, clock = self.context({"spirit": 5000, "league": {"leagueId": 7}}, now=ts(2026, 10, 23, 23) + 3599)

        def league_read(body):
            clock.server += 1
            return {"league": self.info(0)["league"]}

        conn.responses[p.ROUTE_LEAGUE_INFO] = league_read

        def refresh():
            if ctx.refresh.call_count > 1:
                ctx.session.info = self.info(0)
            return ctx.info

        ctx.refresh.side_effect = refresh
        self.assertEqual(alliance.run(ctx).status, core.SUBMITTED)
        ctx.sleep.assert_called_once_with(15 * 60 + 30)
        self.assertEqual(ctx.refresh.call_count, 3)
        self.assertEqual(len(conn.bodies(p.ROUTE_DEVOTE)), 1)

    def test_negative_or_fractional_contribution_count_is_unknown(self):
        for count in (-1, 1.5):
            with self.subTest(count=count):
                self.assertIsNone(state.league_daily_count(
                    {"leagueId": 7, "leagueDevote": {"1": count}}, 1
                ))

    def test_non_finite_or_negative_spirit_is_unknown(self):
        for spirit in (-1, float("nan"), float("inf")):
            with self.subTest(spirit=spirit):
                self.assertIsNone(state.spirit({"spirit": spirit}))


class RunnerTests(unittest.TestCase):
    def test_reported_final_metadata_is_saved_without_another_log(self):
        events = []
        account = Account("a", p.ClientConfig(uid=1, username="u"))
        runner = runner_module._AccountRun(account, None, lambda n, r: events.append((n, r)))
        first = core.TaskResult("pet_arena", core.SUBMITTED, "斗宠大会已挑战 5/5 次")
        runner.show("a", first, True)
        updated = replace(first, account_profile={"updated": True})
        runner.record(updated)
        self.assertEqual([r.log for _, r in events], [True, False])
        self.assertEqual(events[-1][1].account_profile, {"updated": True})
        self.assertEqual(runner.results, [updated])

    def test_final_dedup_respects_account_and_actual_result_changes(self):
        events = []
        account = Account("a", p.ClientConfig(uid=1, username="u"))
        runner = runner_module._AccountRun(account, None, lambda n, r: events.append((n, r)))
        first = core.TaskResult("arena", core.SUBMITTED, "已挑战 5/5 次")
        runner.show("a", first, True)
        runner.emit_result("b", first)
        runner.emit_result("a", replace(first, detail="已挑战 5/5 次；额外领奖完成"))
        runner.emit_result("a", first)  # 上次报告已收束，下一次独立执行不可吞掉
        self.assertEqual([n for n, _ in events], ["a", "b", "a", "a"])
        self.assertTrue(all(r.log for _, r in events))

    def test_returned_uncertain_stops_remaining_tasks(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        later = mock.Mock(return_value=core.TaskResult("b", core.SUBMITTED))
        specs = {"a": core.TaskSpec("a", "a", lambda ctx: core.TaskResult("a", core.UNCERTAIN, "未确认结算")),
                 "b": core.TaskSpec("b", "b", later)}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            result = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a", "b")))
        later.assert_not_called()
        self.assertEqual([(r.task, r.status) for r in result], [("a", core.UNCERTAIN)])

    def _staged_specs(self, calls):
        def step(name, status=core.SUBMITTED):
            def fn(ctx):
                calls.append((ctx.session.tag, name))
                return core.TaskResult(name.split(".")[0], status, name)
            return fn
        return {
            "x": core.TaskSpec("x", "x", step("x")),
            "f": core.TaskSpec("f", "f", step("f"), last=True, stage=step("f.stage")),
            "w": core.TaskSpec("w", "w", step("w"), terminal=True, after_saves=True),
        }

    def _fake_session(self):
        def make(config, *args, **kwargs):
            fake = mock.MagicMock()
            fake.__enter__.return_value = fake
            fake.tag = config.record_index
            return fake
        return make

    def test_single_save_runs_stage_after_other_tasks_before_terminal(self):
        calls = []
        with mock.patch("zmxy.runner.load_tasks", return_value=self._staged_specs(calls)),              mock.patch("zmxy.session.Session", side_effect=self._fake_session()):
            result = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("w", "f", "x")))
        self.assertEqual([c[1] for c in calls], ["x", "f", "f.stage", "w"])
        self.assertEqual([(r.task, r.detail) for r in result], [("x", "x"), ("f", "f；f.stage"), ("w", "w")])

    def test_multi_save_stage_and_after_saves_wait_for_all_saves(self):
        calls = []
        accounts = [Account(f"u{i}", p.ClientConfig(uid=1, username="u", record_index=i), tasks=("x", "f", "w"))
                    for i in (1, 2)]
        with mock.patch("zmxy.runner.load_tasks", return_value=self._staged_specs(calls)),              mock.patch("zmxy.session.Session", side_effect=self._fake_session()):
            result = core.run_batch(accounts)
        self.assertEqual(calls, [(1, "x"), (1, "f"), (2, "x"), (2, "f"),
                                 (1, "f.stage"), (1, "w"), (2, "f.stage"), (2, "w")])
        for name in ("u1", "u2"):
            self.assertEqual([(r.task, r.detail) for r in result[name]], [("x", "x"), ("f", "f；f.stage"), ("w", "w")])

    def test_batch_uncertain_stops_same_login_saves_only(self):
        accounts = [Account(name, p.ClientConfig(uid=1, username=user, record_index=index), tasks=("a",))
                    for name, user, index in (("u1", "u", 0), ("u2", "u", 1), ("v1", "v", 0))]
        seen = []

        def execute(account, **kwargs):
            seen.append(account.name)
            return [core.TaskResult("a", core.UNCERTAIN if account.name == "u1" else core.SUBMITTED)]

        with mock.patch("zmxy.runner.run_account", side_effect=execute):
            result = core.run_batch(accounts)
        self.assertEqual(set(seen), {"u1", "v1"})
        self.assertEqual(set(result), {"u1", "v1"})

    def test_uncertain_login_has_no_resume_entries_for_later_saves(self):
        accounts = [Account(name, p.ClientConfig(uid=1, username=user, record_index=index), tasks=("dungeon_main",))
                    for name, user, index in (("u1", "u", 0), ("u2", "u", 1), ("v1", "v", 0))]
        results = {"u1": [core.TaskResult("dungeon_main", core.UNCERTAIN)],
                   "v1": [core.TaskResult("dungeon_main", core.PAUSED)]}
        self.assertEqual(core.pending(iter(accounts), results), {"v1": ["dungeon_main"]})

    def _later_run(self, a_results):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        calls, queue = [], list(a_results)

        def a(ctx):
            calls.append("a")
            return queue.pop(0)

        def b(ctx):
            calls.append("b")
            return core.TaskResult("b", "submitted")

        specs = {k: core.TaskSpec(k, k, f) for k, f in (("a", a), ("b", b))}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            res = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a", "b")))
        return calls, res

    def test_retry_in_runs_later_tasks_first_then_comes_back(self):
        """逃跑禁赛：先做后面的任务，到时间再重跑，结果整个换掉占位。"""
        wait = core.TaskResult("a", core.SKIPPED, "逃跑禁赛", pending=True, retry_in=0.05)
        calls, res = self._later_run([wait, core.TaskResult("a", "submitted", "打完了")])
        self.assertEqual(calls, ["a", "b", "a"])
        self.assertEqual([(r.task, r.status, r.detail) for r in res], [("a", "submitted", "打完了"), ("b", "submitted", "")])

    def test_retry_in_gives_up_after_max_rounds(self):
        wait = core.TaskResult("a", core.SKIPPED, "逃跑禁赛", pending=True, retry_in=0.01)
        calls, res = self._later_run([wait] * (runner_module.LATER_MAX + 1))
        self.assertEqual(calls.count("a"), runner_module.LATER_MAX + 1)
        self.assertEqual((res[0].status, res[0].pending), (core.BLOCKED, False))
        self.assertIn("不再等", res[0].detail)

    def test_uncertain_stops_remaining_tasks_and_error_does_not(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        calls = []

        def boom(ctx):
            calls.append("a")
            raise RuntimeError("x")

        def unsure(ctx):
            calls.append("b")
            raise UncertainOutcome("?")

        def never(ctx):
            calls.append("c")
            return core.TaskResult("c", "submitted")

        specs = {k: core.TaskSpec(k, k, f) for k, f in (("a", boom), ("b", unsure), ("c", never))}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            acc = Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a", "b", "c"))
            res = core.run_account(acc)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual([r.status for r in res], ["error", "uncertain"])

    def test_swallowed_bag_full_pauses_and_stops_remaining(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        calls = []

        def swallow(ctx):  # 任务把服务器的背包满拒绝吞成备注，仍返回已提交
            calls.append("a")
            ctx.session.bag_full = True
            return core.TaskResult("a", "submitted", "领取失败：背包空间不足")

        def never(ctx):
            calls.append("b")
            return core.TaskResult("b", "submitted")

        specs = {k: core.TaskSpec(k, k, f) for k, f in (("a", swallow), ("b", never))}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            res = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a", "b")))
        self.assertEqual(calls, ["a"])
        self.assertEqual([r.status for r in res], ["paused"])
        self.assertIn(core.BAG_FULL_HINT, res[0].detail)

    def test_kicked_session_pauses_like_bag_full(self):
        for exc in (core.UncertainOutcome("scene.shopHandler.buy 请求结果未知（Pomelo 连接尚未完成握手），请登录游戏核对后再运行"),
                    p.AllianceError("服务器踢出了当前会话")):
            fake = mock.MagicMock()
            fake.__enter__.return_value = fake
            calls = []

            def work(ctx, exc=exc):
                calls.append("a")
                ctx.progress["n"] = 3
                raise exc

            specs = {"a": core.TaskSpec("a", "a", work), "b": core.TaskSpec("b", "b", lambda c: calls.append("b"))}
            with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
                res = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a", "b")))
            self.assertEqual(calls, ["a"])
            self.assertEqual((res[0].status, dict(res[0].progress)), ("paused", {"n": 3}))
            self.assertEqual(res[0].detail, str(exc))  # 日志保持原样

    def test_paused_result_carries_ctx_progress(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake

        def work(ctx):
            ctx.progress["n"] = 2
            ctx.session.bag_full = True
            return core.TaskResult("a", "submitted")

        specs = {"a": core.TaskSpec("a", "a", work)}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            res = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a",)))
        self.assertEqual((res[0].status, dict(res[0].progress)), ("paused", {"n": 2}))

    def test_stop_event_skips(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        stop = threading.Event()
        stop.set()
        specs = {"a": core.TaskSpec("a", "a", lambda c: core.TaskResult("a", "submitted"))}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            res = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a",)), stop=stop)
        self.assertEqual(res[0].status, "skipped")


if __name__ == "__main__":
    unittest.main()
