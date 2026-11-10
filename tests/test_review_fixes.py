"""2026-10-05 全面审查修掉的问题的回归测试。全部离线。"""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from zmxy import core, dungeon, login, online, protocol as p, quota
from zmxy.core import Account, Session, TaskContext, load_tasks
from zmxy.store import Store
from zmxy.tasks import boss_raid, special, tongtian
from tests.support import Fake, elite_info


_UNLOCK_PATCH = None


def setUpModule():
    """这个文件的用例不关心解锁状态（解锁判断另有 tests/test_unlock.py）。"""
    global _UNLOCK_PATCH
    from unittest import mock
    _UNLOCK_PATCH = mock.patch("zmxy.quota.locked", return_value=None)
    _UNLOCK_PATCH.start()


def tearDownModule():
    _UNLOCK_PATCH.stop()


def session_with(conn, info=None, entry=None):
    s = Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s.info = conn, info or {}
    s._link = login.Link(conn, s.config, [{"index": 0}], entry or {})
    return s


class SpentAfterRefreshTests(unittest.TestCase):
    """剩余次数 = 快照 - spent。刷新后的快照已含本连接用掉的，spent 不清掉就会扣两遍。"""

    def test_refresh_clears_spent(self):
        s = session_with(Fake(lambda route, body: {"code": 200, "info": {}} if route == p.ROUTE_ENTER else None))
        s.spent[("daily", 30600)] += 1
        s.refresh()
        self.assertEqual(sum(s.spent.values()), 0)

    def test_second_free_elite_run_survives_a_refresh(self):
        used = [0]

        def server(route, body):
            if route == p.ROUTE_ENTER:
                return {"code": 200, "info": elite_info(used[0])}
            if route == dungeon.R_CREATE:
                used[0] += 1
            return None

        conn = Fake(server)
        s = session_with(conn, elite_info(0))
        spec = load_tasks()["dungeon_elite"]
        s.bind(spec)
        options = {"levels": [30600], "times": 1}
        self.assertEqual(spec.run(TaskContext(s, options)).status, "submitted")
        s.refresh()  # 服务器记了 1 次；精英每关每天 2 次，还剩 1 次
        again = spec.run(TaskContext(s, options))
        self.assertEqual(again.status, "submitted", again.detail)
        self.assertEqual(spec.run(TaskContext(s, options)).status, "skipped")  # 不刷新：本连接记着已用完
        self.assertEqual(conn.routes().count("createSinglePlayerCopy"), 2)


class SharedGroupGuardTests(unittest.TestCase):
    def test_member_of_an_unreadable_group_is_blocked(self):
        with mock.patch.object(quota, "_helper_group", return_value=None):
            q = quota.of({"dungeon": {}, "vip": {"level": 0}}, 100101)  # 九宫玄塔一层：DungeonHelper 共享组成员
        self.assertIsNone(q.left)
        self.assertIn("共享次数组", q.why)


class KeepAliveSleepTests(unittest.TestCase):
    def live(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        conn = p.PomeloConnection("x", 1, transport=a)
        conn._connected = True
        return session_with(conn), b

    def test_heartbeat_is_answered_while_sleeping(self):
        s, peer = self.live()
        s._conn.heartbeat_seconds = 0.1  # 用短协商间隔验证等待中的延时回复
        peer.send(p.PackageCodec.encode(3))
        began = time.monotonic()
        self.assertTrue(TaskContext(s, {}).sleep(0.3))
        self.assertGreaterEqual(time.monotonic() - began, 0.25)
        peer.settimeout(1)
        self.assertEqual(peer.recv(16), p.PackageCodec.encode(3))
        self.assertTrue(s.live)

    def test_stop_wakes_the_sleep(self):
        s, _ = self.live()
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        began = time.monotonic()
        self.assertFalse(TaskContext(s, {}, stop=stop).sleep(30))
        self.assertLess(time.monotonic() - began, 3)

    def test_dead_connection_interrupts_wait_without_raising(self):
        s, peer = self.live()
        peer.close()
        began = time.monotonic()
        self.assertFalse(TaskContext(s, {}).sleep(30))
        self.assertFalse(s.live)
        self.assertTrue(s.idle_error)
        self.assertLess(time.monotonic() - began, 3)

    def test_fake_connection_keeps_the_plain_sleep(self):
        s = session_with(Fake())
        self.assertFalse(s.live)
        with mock.patch("time.sleep") as plain:
            self.assertTrue(TaskContext(s, {}).sleep(900))
        plain.assert_called_once_with(900)


class ServerClockTests(unittest.TestCase):
    def test_now_moves_on_after_entering(self):
        s = session_with(Fake(), entry={"time": 1_700_000_000})
        self.assertEqual(s.now, 1_700_000_000)
        s._link.at = time.monotonic() - 100
        self.assertIn(s.now, (1_700_000_100, 1_700_000_101))

    def test_no_server_time_stays_none(self):
        self.assertIsNone(session_with(Fake()).now)


class TongtianPollCapTests(unittest.TestCase):
    def ctx(self, conn):
        s = session_with(conn)
        s.bind(load_tasks()["tongtian_consult"])
        return TaskContext(s, {})

    @mock.patch("time.sleep", lambda s: None)
    def test_target_that_never_becomes_ready_is_dropped(self):
        conn = Fake(lambda route, body: {"code": 500, "error": "不可请教"} if route.endswith("beginAttack") else None)
        with mock.patch.object(tongtian, "MAX_POLL_SECONDS", -1):
            out = tongtian.consult(self.ctx(conn), {"uid": 7}, dungeon.Gains(), time.monotonic() + 60)
        self.assertEqual(out, "busy")
        self.assertEqual(conn.routes(), ["beginAttack"])

    @mock.patch("time.sleep", lambda s: None)
    def test_short_wait_for_the_zone_still_works(self):
        tries = [0]

        def server(route, body):
            if route.endswith("beginAttack"):
                tries[0] += 1
                return {"code": 500, "error": "未到请教区域"} if tries[0] < 3 else {"code": 200, "escortData": {"uid": 7}}
            return None

        conn = Fake(server)
        self.assertEqual(tongtian.consult(self.ctx(conn), {"uid": 7}, dungeon.Gains(), time.monotonic() + 60), "done")
        self.assertEqual(conn.routes(), ["beginAttack"] * 3 + ["createSinglePlayerCopy", "endAttack"])


class SmallFixTests(unittest.TestCase):
    def test_unknown_tower_floor_name_does_not_crash(self):
        self.assertEqual(special.short_name("九宫玄塔一层"), "普通·1层")
        self.assertEqual(special.short_name("九宫玄塔十层"), "九宫玄塔十层")

    def test_unreadable_settings_file_is_kept_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_bytes(b'{"accounts": [{"username": "u"')  # 写了一半
            store = Store(path)
            self.assertEqual(store.accounts, [])
            self.assertEqual(path.with_suffix(".bad").read_bytes(), b'{"accounts": [{"username": "u"')
            store.save()  # 之后的保存不影响那份备份
            self.assertTrue(path.with_suffix(".bad").exists())

    def test_missing_settings_file_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            Store(path)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_task_returning_nothing_is_an_error_not_a_crash(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        specs = {"a": core.TaskSpec("a", "a", lambda ctx: None),
                 "b": core.TaskSpec("b", "b", lambda ctx: core.TaskResult("b", "submitted"))}
        with mock.patch("zmxy.runner.load_tasks", return_value=specs), mock.patch("zmxy.session.Session", return_value=fake):
            res = core.run_account(Account("n", p.ClientConfig(uid=1, username="u"), tasks=("a", "b")))
        self.assertEqual([r.status for r in res], ["error", "submitted"])

    def test_cdn_probe_gives_up_after_its_budget(self):
        calls = []
        with mock.patch.dict(online._current, {}, clear=True), mock.patch.object(online, "exact_tag", None), \
                mock.patch.object(online, "PROBE_BUDGET", -1):
            cg = online.client_cg(1_700_000_000, lambda url: calls.append(url))
        self.assertEqual(len(calls), 1)  # 连不上时不把 84 个候选地址挨个等到超时
        self.assertEqual(cg, online.local_cg())


if __name__ == "__main__":
    unittest.main()
