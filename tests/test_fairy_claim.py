"""藏宝洞仙魄领取时间来源与缺失记录的离线回归。"""
import unittest
from unittest import mock

from zmxy import core, login, protocol as p
from zmxy.tasks import treasure_cave as ff  # 领取仙魄 2026-11-08 从妖幡卷商店挪到藏宝洞
from tests.support import FakeConn

NOW = 1800000000


class FairyClaimTests(unittest.TestCase):
    def claim(self, entry, history=True, rows=None):
        info = {'landformPublish': {'1': {'time': (NOW-7200)*1000}}} if history is True else (
            {} if history is None else {'landformPublish': history})
        conn = FakeConn({ff.R_PUBLISHED: {'data': rows if rows is not None else [{'id': 1, 'entityId': 'cave-1'}]},
                         ff.R_REWARD: {'give': [{'subType': 'fairyMoney', 'count': 10}]}})
        session = core.Session(p.ClientConfig(uid=1, username='offline'))
        session._conn, session.info = conn, info
        session._link = login.Link(conn, session.config, [{'index': 0, '_id': 'record-1'}], entry)
        session.bind(core.load_tasks()[ff.ID])
        with mock.patch.object(session, 'refresh', return_value=info):
            result = ff.claim_fairy(core.TaskContext(session, {}))
        return result, conn

    def test_nested_server_time_is_accepted(self):
        r, conn = self.claim({'data': {'serverTime': NOW*1000}})
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(conn.bodies(ff.R_REWARD)[0]['entityId'], 'cave-1')

    def test_top_level_time_still_works(self):
        r, conn = self.claim({'time': NOW})
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(conn.bodies(ff.R_REWARD)), 1)

    def test_client_info_server_time_works_even_with_history_time_first(self):
        r, conn = self.claim({'info': {'landformPublish': {'9': {'time': 1000}},
                                      'serverTime': NOW*1000}})
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(conn.bodies(ff.R_REWARD)), 1)

    def test_invalid_client_server_time_never_uses_history_time(self):
        for value in (None, '', 'bad', 0, -1, True):
            with self.subTest(value=value):
                r, conn = self.claim({'info': {'serverTime': value,
                                              'landformPublish': {'9': {'time': NOW*1000}}}})
                self.assertEqual(r.status, core.BLOCKED, r.detail)
                self.assertNotIn(ff.R_REWARD, conn.routes())

    def test_verified_clock_advances_with_monotonic_elapsed(self):
        session = core.Session(p.ClientConfig(uid=1, username='offline'))
        session._link = login.Link(None, session.config, [], {'info': {'serverTime': NOW*1000}}, at=100)
        with mock.patch.object(ff.time, 'monotonic', return_value=3700):
            self.assertEqual(ff.server_clock(core.TaskContext(session, {})), NOW + 3600)

    def test_missing_history_reports_exact_field_and_never_claims(self):
        r, conn = self.claim({'time': NOW}, history=None)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertIn('landformPublish', r.detail)
        self.assertNotIn(ff.R_REWARD, conn.routes())

    def test_missing_time_does_not_fall_back_to_local_clock(self):
        r, conn = self.claim({})
        self.assertEqual(r.status, core.BLOCKED)
        self.assertIn('服务器时间', r.detail)
        self.assertNotIn(ff.R_REWARD, conn.routes())

    def test_role_history_time_cannot_be_used_as_server_clock(self):
        r, conn = self.claim({'info': {'landformPublish': {'9': {'time': NOW*1000}}}})
        self.assertEqual(r.status, core.BLOCKED)
        self.assertIn('服务器时间', r.detail)
        self.assertNotIn(ff.R_REWARD, conn.routes())

    def test_recent_reward_resets_one_hour_wait(self):
        r, conn = self.claim({'time': NOW}, history={'1': {'time': (NOW-7200)*1000,
                                                                  'rewardTime': (NOW-10)*1000}})
        self.assertEqual(r.status, core.SKIPPED, r.detail)
        self.assertNotIn(ff.R_REWARD, conn.routes())

    def test_unknown_booth_history_is_not_treated_as_zero(self):
        r, conn = self.claim({'time': NOW}, history={})
        self.assertEqual(r.status, core.BLOCKED)
        self.assertNotIn(ff.R_REWARD, conn.routes())

    def test_exact_hour_boundary_can_claim(self):
        r, conn = self.claim({'time': NOW}, history={'1': {'time': (NOW-3600)*1000}})
        self.assertEqual(r.status, core.SUBMITTED, r.detail)

    def test_empty_published_list_does_not_need_history_or_claim(self):
        r, conn = self.claim({}, history=None, rows=[])
        self.assertEqual(r.status, core.SKIPPED)
        self.assertNotIn(ff.R_REWARD, conn.routes())
