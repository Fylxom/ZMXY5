import unittest
from unittest import mock

from zmxy import core, protocol as p
from zmxy.store import Store
from zmxy.tasks import warehouse_sync


class TidyScheduleTests(unittest.TestCase):
    class FakeSession:
        def __init__(self, *a, **k):
            self.carry = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def bind(self, spec):
            pass

    def run_with(self, tidy, n=5):
        calls = []
        registry = {f"t{i}": core.TaskSpec(f"t{i}", f"t{i}", lambda ctx, i=i: calls.append(f"t{i}") or core.TaskResult(f"t{i}", "submitted"))
                    for i in range(n)}
        fake = core.TaskSpec(warehouse_sync.ID, warehouse_sync.ID, lambda ctx: calls.append("tidy") or core.TaskResult(warehouse_sync.ID, "submitted"))
        acc = core.Account("a", p.ClientConfig(uid=1, username="u"), tuple(registry), tidy=tidy)
        with mock.patch("zmxy.session.Session", self.FakeSession), mock.patch("zmxy.runner.load_tasks", return_value=registry), \
                mock.patch.object(warehouse_sync, "SPEC", fake):
            core.run_account(acc)
        return calls

    def test_off(self):
        self.assertEqual(self.run_with(None), ["t0", "t1", "t2", "t3", "t4"])

    def test_zero_runs_once_at_end(self):
        self.assertEqual(self.run_with(0), ["t0", "t1", "t2", "t3", "t4", "tidy"])

    def test_every_n(self):
        self.assertEqual(self.run_with(2), ["t0", "t1", "tidy", "t2", "t3", "tidy", "t4"])
        self.assertEqual(self.run_with(1, 2), ["t0", "tidy", "t1", "tidy"])

    def test_can_run_alone(self):
        calls = []
        fake = core.TaskSpec(warehouse_sync.ID, warehouse_sync.ID, lambda ctx: calls.append("tidy") or core.TaskResult(warehouse_sync.ID, "submitted"))
        acc = core.Account("a", p.ClientConfig(uid=1, username="u"), (warehouse_sync.ID,))
        with mock.patch("zmxy.session.Session", self.FakeSession), mock.patch("zmxy.runner.load_tasks", return_value={}),                 mock.patch.object(warehouse_sync, "SPEC", fake):
            out = core.run_account(acc)
        self.assertEqual(calls, ["tidy"])
        self.assertEqual([r.status for r in out], ["submitted"])


class TidyRunTests(unittest.TestCase):
    class Ctx:
        def __init__(self, resp):
            self.resp = resp
            self.sent = None

        def request(self, route, body):
            self.sent = (route, body)
            return self.resp

    def test_nothing_moved(self):
        ctx = self.Ctx({"moved": []})
        r = warehouse_sync.run(ctx)
        self.assertEqual(ctx.sent, (warehouse_sync.ROUTE, {"operateType": 1}))
        self.assertEqual((r.status, r.detail), ("skipped", "无相同道具"))

    def test_moved(self):
        r = warehouse_sync.run(self.Ctx({"moved": [{"id": 101026007, "count": 3}]}))
        self.assertEqual(r.status, "submitted")
        self.assertIn("x3", r.detail)


class TidyStoreTests(unittest.TestCase):
    def test_mapping(self):
        from zmxy import dungeon
        self.assertEqual(dungeon.item_name(210010015), "灵生·天市符")  # 符文不在 Prop 表里
        self.assertIsNone(dungeon.item_name(1))
        f = Store.tidy_every
        self.assertIsNone(f({}))
        self.assertIsNone(f({"tidy": {"on": False, "every": 3}}))
        self.assertEqual(f({"tidy": {"on": True, "every": 3}}), 3)
        self.assertEqual(f({"tidy": {"on": True, "every": 0}}), 0)
        self.assertEqual(f({"tidy": {"on": True}}), 0)


if __name__ == "__main__":
    unittest.main()
