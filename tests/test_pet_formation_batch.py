"""双槽上阵合并查询、权威核验及续跑不重发。"""
import json
import unittest
from pathlib import Path
from tests import test_pet_cultivation as fixtures
from zmxy import core
from zmxy.tasks.pet_cultivation import journal, task


class AckWorld(fixtures.World):
    corrupt = False
    fail_once = False

    def request(self, route, body):
        reply = super().request(route, body)
        if route == task.R_EQUIP and body['entityId'].startswith('new-'):
            if self.fail_once and body['index'] == 1:
                self.fail_once = False
                self.fail_refresh = True
            if self.corrupt:
                self.info['p1' if body['index'] == 0 else 'p2']['equipments'].pop('pet', None)
            return {'code': 200, 'index': body['index'], 'equipEntityId': body['entityId'], 'type': 'pet'}
        return reply


class FormationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HarvestFlowTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_two_equips_share_query_without_changing_rewards(self):
        base = fixtures.World(dual=True)
        self.assertEqual(self.f.execute(base).status, core.SUBMITTED)
        fast = AckWorld(dual=True)
        self.assertEqual(self.f.execute(fast).status, core.SUBMITTED)
        self.assertEqual(base.capture_count, fast.capture_count)
        self.assertEqual(base.count(task.FLOWER), fast.count(task.FLOWER))
        self.assertEqual(base.events.count(('refresh', None))-fast.events.count(('refresh', None)), 1)
        indices = [i for i,e in enumerate(fast.events) if e == ('write', task.R_EQUIP)]
        self.assertNotIn(('refresh', None), fast.events[indices[0]+1:indices[1]])
        self.assertEqual(fast.info['p1']['equipments']['pet']['entityId'], 'old')

    def test_forged_success_stops_before_experience_and_preserves_checks(self):
        w = AckWorld(dual=True)
        w.corrupt = True
        result = self.f.execute(w)
        self.assertEqual(result.status, core.PAUSED)
        self.assertFalse(w.field_entries)
        saved = journal.load_file(next(Path(self.f.temp.name).rglob('gpt_*.json')))[0]
        self.assertEqual(len(saved['formationChecks']), 2)
        before = len(w.sent)
        again = self.f.execute(w, resume=result.progress)
        self.assertEqual(again.status, core.PAUSED)
        self.assertEqual(len(w.sent), before)

    def test_lost_shared_query_resumes_without_repeating_equip(self):
        w = AckWorld(dual=True)
        w.fail_once = True
        first = self.f.execute(w)
        self.assertEqual(first.status, core.PAUSED)
        self.assertFalse(w.field_entries)
        second = self.f.execute(w, resume=first.progress)
        self.assertEqual(second.status, core.SUBMITTED, second.detail)
        calls = [b for r,b in w.sent if r == task.R_EQUIP and b['entityId'].startswith('new-')]
        self.assertEqual(len(calls), 2)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')


if __name__ == '__main__':
    unittest.main()
