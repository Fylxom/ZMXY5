"""双十一免费抽卡、动态档期和结果未知停止的离线回归。"""

import threading
import unittest
from unittest import mock

from zmxy import data, protocol as p
from zmxy.core import Session, UncertainOutcome, load_tasks
from zmxy.tasks.events import double11_dog as d
from tests.support import Fake, run, ts


class Double11DogTests(unittest.TestCase):
    def execute(self, used=0, extra=0, *, info=None, fault=None, stop=None):
        info = info if info is not None else {"everyDaySetting": {
            "double11LotteryCnt": used, "addDouble11LotteryCnt": extra}}
        cards = {"dog_1": 2}
        self.refreshes = 0
        session = Session(p.ClientConfig(uid=1, username="u"))

        def refresh():
            self.refreshes += 1
            if fault == "refresh" and self.refreshes > 1:
                raise p.AllianceError("刷新失败")
            return session.info

        def handler(route, body):
            self.assertEqual(body, {})
            if route == d.R_DATA:
                return {} if fault == "data" else {"data": dict(cards)}
            self.assertEqual(route, d.R_DRAW)
            if fault == "timeout":
                raise TimeoutError("断线")
            if fault == "reject":
                return "次数不足"
            info["everyDaySetting"]["double11LotteryCnt"] = info["everyDaySetting"].get("double11LotteryCnt", 0) + 1
            if fault != "card":
                cards["dog_1"] += 1
            if fault == "counter":
                info["everyDaySetting"]["double11LotteryCnt"] -= 1
            if stop is not None:
                stop.set()
            if fault == "give":
                return {}
            return {"give": [{"type": "coin", "subType": "dog_1", "count": 1}]}

        self.conn = Fake(handler)
        old_row = data.row

        def row(table, key):
            if table == "Double11Common":
                return {"startActivityId": 1823, "rewardGetActivityId": 1825, "dailyDrawNum": 1,
                        "type": "coin", "subType": "dog_1"}
            if table == "Activity" and str(key) in ("1823", "1825"):
                return {"showShelfTime": "2026/11/1-00:00:00" if str(key) == "1823" else "2026/12/1-00:00:00",
                        "showUnderTime": "2026/11/30-23:59:59" if str(key) == "1823" else "2026/12/7-23:59:59"}
            return old_row(table, key)

        with mock.patch.object(data, "row", side_effect=row), \
                mock.patch.object(d.state, "table_rows", return_value=[{"consume_subType": "dog_1"}]), \
                mock.patch.object(session, "refresh", side_effect=refresh):
            return run(d.ID, self.conn, info, session=session, now=ts(2026, 11, 5), stop=stop)

    def test_registered_limited_without_paid_routes_or_options(self):
        spec = load_tasks()[d.ID]
        self.assertEqual(spec.group, "活动")
        self.assertEqual(spec.writes, frozenset({d.R_DRAW}))
        self.assertIsNone(spec.choices)

    def test_old_activity_table_sends_nothing(self):
        conn = Fake()
        result = run(d.ID, conn, {}, now=ts(2026, 10, 28))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(conn.sent, [])
        self.assertFalse(d.opened(None))

    def test_one_free_draw_and_authoritative_confirmation(self):
        result = self.execute()
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(len(self.conn.bodies("double11Lottery")), 1)
        self.assertEqual(self.refreshes, 2)

    def test_extra_attempts_are_used_without_buying(self):
        result = self.execute(used=1, extra=3)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(len(self.conn.bodies("double11Lottery")), 3)

    def test_exhausted_skips(self):
        self.assertEqual(self.execute(used=1).status, "skipped")
        self.assertFalse(self.conn.bodies("double11Lottery"))

    def test_missing_or_corrupt_daily_state_blocks_without_draw(self):
        for info in ({}, {"everyDaySetting": {"double11LotteryCnt": -1}},
                     {"everyDaySetting": {"addDouble11LotteryCnt": "bad"}},
                     {"everyDaySetting": {"double11LotteryCnt": 5}}):
            with self.subTest(info=info):
                self.assertEqual(self.execute(info=info).status, "blocked")
                self.assertFalse(self.conn.bodies("double11Lottery"))

    def test_valid_daily_object_omits_zero_fields_like_client(self):
        self.assertEqual(self.execute(info={"everyDaySetting": {}}).status, "submitted")

    def test_unknown_results_stop_without_retry(self):
        for fault in ("timeout", "give", "card", "counter", "refresh"):
            with self.subTest(fault=fault):
                with self.assertRaises(UncertainOutcome):
                    self.execute(extra=2, fault=fault)
                self.assertEqual(len(self.conn.bodies("double11Lottery")), 1)

    def test_explicit_rejection_blocks_without_retry(self):
        self.assertEqual(self.execute(extra=2, fault="reject").status, "blocked")
        self.assertEqual(len(self.conn.bodies("double11Lottery")), 1)

    def test_stop_after_first_draw_still_confirms_it(self):
        stop = threading.Event()
        self.assertEqual(self.execute(extra=2, stop=stop).status, "blocked")
        self.assertEqual(len(self.conn.bodies("double11Lottery")), 1)
        self.assertEqual(self.refreshes, 2)

    def test_stop_before_any_draw_is_skipped(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.execute(extra=2, stop=stop).status, "skipped")
        self.assertFalse(self.conn.bodies("double11Lottery"))

    def test_missing_or_changed_config_fails_closed(self):
        old_row = data.row
        for replacement in (None, {"dailyDrawNum": -1}, {"dailyDrawNum": True}):
            with mock.patch.object(data, "row", side_effect=lambda t, k: replacement if t == "Double11Common" else old_row(t, k)):
                self.assertFalse(d.opened(ts(2026, 11, 5)))

    def test_missing_dog_data_blocks_before_draw(self):
        self.assertEqual(self.execute(fault="data").status, "blocked")
        self.assertFalse(self.conn.bodies("double11Lottery"))

    def test_future_activity_ids_and_reward_phase_use_current_table(self):
        rows = {
            "Double11Common": {"startActivityId": 9991, "rewardGetActivityId": 9992,
                               "dailyDrawNum": 1, "type": "coin", "subType": "dog_1"},
            9991: {"showShelfTime": "2026/11/1-00:00:00", "showUnderTime": "2026/12/7-23:59:59"},
            9992: {"showShelfTime": "2026/12/1-00:00:00", "showUnderTime": "2026/12/7-23:59:59"},
        }
        def row(table, key):
            return rows.get("Double11Common" if table == "Double11Common" else key)
        with mock.patch.object(data, "row", side_effect=row), \
                mock.patch.object(d.state, "table_rows", return_value=[{"consume_subType": "dog_1"}]):
            self.assertFalse(d.opened(ts(2026, 10, 31)))
            self.assertTrue(d.opened(ts(2026, 11, 5)))
            self.assertFalse(d.opened(ts(2026, 12, 1)))
            self.assertFalse(d.opened(ts(2026, 12, 8)))


if __name__ == "__main__":
    unittest.main()
