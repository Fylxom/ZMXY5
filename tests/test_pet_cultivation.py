"""刷益灵花流程的离线回归测试。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zmxy import core, dungeon, protocol as p
from zmxy.tasks import pet_catch
from zmxy.tasks.pet_cultivation import harvest, journal, ledger, task as cultivation


class World:
    """具备真实物品和宠物状态变化的离线服务器，支持写入后断线。"""
    def __init__(self, vip=6, dual=False, forget=5, warehouse=0, gourds=50, spirit=100000):
        self.info = {'vip': {'level': vip}, 'spirit': spirit, 'bag': {'pet': {}, 'petMaxNum': 36,
            'item': {}, 'warehouseItem': {}}, 'p1': {'level': 164, 'equipments': {}}, 'dungeon': {}}
        if dual:
            self.info['p2'] = {'level': 164, 'equipments': {}}
        for item, n in ((cultivation.FORGET_FLOWER, forget), (cultivation.FLOWER, 0), (pet_catch.GOURD_ID, gourds)):
            self.info['bag']['item'][str(item)] = {'id': item, 'count': n}
        self.info['bag']['warehouseItem'][str(cultivation.FORGET_FLOWER)] = {'id': cultivation.FORGET_FLOWER, 'count': warehouse}
        self.info['bag']['pet']['old'] = self.raw('old', level=50, nickName='旧宠')
        self.info['p1']['equipments']['pet'] = self.info['bag']['pet']['old']
        self.sent, self.pushes, self.capture_count, self.field_entries, self.freed = [], [], 0, [], []
        self.seeds, self.growth = [], {}
        self.refund, self.drop_count = 1, 2
        self.lost_after, self.lost_before, self.fail_refresh_after = None, None, None
        self.fail_refresh = False
        self.events = []

    @staticmethod
    def raw(entity, **over):
        return {'entityId': entity, 'id': 170111101, 'quality': 1, 'sex': 1, 'level': 1, 'petEXP': 0,
                'capacity': 4, 'hpQualification': 500, 'nickName': '', 'equipments': {}, 'aoyiSkill': {},
                'skill': {'pet': {'4114120': 1}, 'passivity': {'8200610': 1}}, **over}

    def count(self, item, warehouse=False):
        return self.info['bag']['warehouseItem' if warehouse else 'item'][str(item)]['count']

    def add(self, item, n, warehouse=False):
        self.info['bag']['warehouseItem' if warehouse else 'item'][str(item)]['count'] += n

    def request(self, route, body):
        self.events.append(('write', route))
        self.sent.append((route, dict(body)))
        if self.lost_before == route:
            self.lost_before = None
            raise p.AllianceError('服务器踢出了当前会话')
        result = {}
        if route == dungeon.R_CREATE:
            if body['mid'] == 79:
                self.field_entries.append({'captures': self.capture_count,
                    'equipped': [v['equipments'].get('pet', {}).get('entityId') for k, v in self.info.items() if k in ('p1', 'p2')]})
                result = {'rnd': 7, 'drops': [{'monsterId': 233103, 'type': 'petAttr', 'subType': 'petEXP', 'count': 100} for _ in range(self.drop_count)]}
            else:
                result = {'rnd': 7, 'drops': []}
        elif route == pet_catch.R_CAPTURE:
            self.add(pet_catch.GOURD_ID, -1)
            self.capture_count += 1
            entity = f'new-{self.capture_count}'
            seed = self.seeds.pop(0) if self.seeds else {}
            self.info['bag']['pet'][entity] = self.raw(entity, **seed)
            result = {'success': 1, 'pet': copy.deepcopy(self.info['bag']['pet'][entity])}
        elif route == cultivation.R_EQUIP:
            self.info['p1' if body['index'] == 0 else 'p2']['equipments']['pet'] = self.info['bag']['pet'][body['entityId']]
        elif route == cultivation.R_TAKEOFF:
            self.info['p1' if body['index'] == 0 else 'p2']['equipments'].pop('pet', None)
        elif route == dungeon.R_PICKUP:
            give = []
            target = 3 if self.info['vip']['level'] >= 3 else 4
            for i, key in enumerate(('p1', 'p2')):
                if key not in self.info:
                    continue
                pet = self.info[key]['equipments'].get('pet')
                if pet is None:
                    continue
                if pet['entityId'] == 'old':
                    raise AssertionError('旧宠不得收到培养经验')
                pet['level'], pet['petEXP'] = 10, pet['petEXP'] + 100
                pet['skill']['passivity'] = self.growth.get(pet['entityId'], {'8200610': target, '8200710': 1}).copy()
                give.append({'subType': 'petEXP', 'entityId': f'index:{i}', 'count': 100})
                self.pushes.append(SimpleNamespace(body={'data': copy.deepcopy(pet)}, route='onPetUpgrade'))
            result = {'give': give}
        elif route == cultivation.R_REMOVE:
            pet = self.info['bag']['pet'][body['entityId']]
            pet['skill']['passivity'].pop(str(body['skillId']))
            self.add(cultivation.FORGET_FLOWER, -1)
            self.add(cultivation.FLOWER, self.refund)
            result = {'give': [{'subType': str(cultivation.FLOWER), 'count': self.refund}]}
        elif route == pet_catch.R_FREE:
            self.freed.append(copy.deepcopy(self.info['bag']['pet'][body['entityId']]))
            self.info['bag']['pet'].pop(body['entityId'])
        elif route in (ledger.R_FROM, ledger.R_TO):
            n = body['count'] if route == ledger.R_FROM else -body['count']
            self.add(body['itemId'], n)
            self.add(body['itemId'], -n, warehouse=True)
        elif route == pet_catch.R_BUY:
            self.add(pet_catch.GOURD_ID, body['count'])
            self.info['spirit'] -= body['count'] * 3
        if self.fail_refresh_after == route:
            self.fail_refresh_after, self.fail_refresh = None, True
        if self.lost_after == route:
            self.lost_after = None
            raise p.AllianceError('服务器踢出了当前会话')
        return {'code': 200, **result}

    def request_many(self, route, bodies):
        return [self.request(route, body) for body in bodies]

    def drain_pushes(self):
        out, self.pushes = self.pushes, []
        return out


class ReadyWorld(World):
    """经验应答先到，升级帧延迟到下批前才可读的离线连接。"""
    def __init__(self, *, available=True, **kwargs):
        super().__init__(**kwargs)
        self.drop_count = 49
        self.ready = []
        self.available = available

    def drain_pushes(self):
        out = super().drain_pushes()
        self.ready.extend(m for m in out if m.route == 'onPetUpgrade')
        return [m for m in out if m.route != 'onPetUpgrade']

    def drain_ready_pushes(self, routes, *, limit=64):
        if not self.available:return []
        result = [m for m in self.ready if m.route in routes][:limit]
        self.ready = [m for m in self.ready if m not in result]
        return result


class TableWorld(World):
    """按经验表升级（每级 150）、只在升级时推送的离线服务器；升到 target_at 级时出 3 级潜能。"""
    def __init__(self, target_at=5, hold_pushes=False, arrive_on_poll=False, with_next_request=False, **kwargs):
        super().__init__(**kwargs)
        self.target_at, self.hold_pushes, self.held = target_at, hold_pushes, []
        self.arrive_on_poll, self.polls = arrive_on_poll, 0
        self.with_next_request = with_next_request

    def poll_push(self, routes, timeout):
        """迟到的升级推送：arrive_on_poll 时等一下就到，否则一直不到。"""
        self.polls += 1
        if self.arrive_on_poll and self.held:
            return self.held.pop(0)
        return None

    def request(self, route, body):
        if route != dungeon.R_PICKUP:
            if self.with_next_request and self.held:  # 迟到的升级推送跟着下一个请求（结算）一起到
                self.pushes.extend(self.held)
                self.held = []
            return super().request(route, body)
        self.events.append(('write', route))
        self.sent.append((route, dict(body)))
        give = []
        for i, key in enumerate(('p1', 'p2')):
            pet = self.info.get(key, {}).get('equipments', {}).get('pet')
            if pet is None:
                continue
            pet['petEXP'] += 100
            leveled = False
            while pet['petEXP'] >= 150:
                pet['petEXP'] -= 150
                pet['level'] += 1
                leveled = True
            if pet['level'] >= self.target_at:
                pet['skill']['passivity'] = {'8200610': 3, '8200710': 1}
            give.append({'subType': 'petEXP', 'entityId': f'index:{i}', 'count': 100})
            if leveled:
                (self.held if self.hold_pushes else self.pushes).append(
                    SimpleNamespace(body={'data': copy.deepcopy(pet)}, route='onPetUpgrade'))
        return {'code': 200, 'give': give}


class HarvestFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            mock.patch.object(journal, 'RUN_ROOT', Path(self.temp.name)),
            mock.patch.object(cultivation, 'map_catalog', return_value=[(79, '辞衡堂')]),
            mock.patch.object(dungeon, 'level', side_effect=lambda mid: dungeon.Level(mid, dungeon.MAIN, '七星殿' if mid == 80 else '辞衡堂', (mid,))),
            mock.patch.object(cultivation.quota, 'locked', return_value=None),
            mock.patch.object(cultivation.quota, 'of', return_value=SimpleNamespace(left=float('inf'))),
            mock.patch.object(pet_catch, 'target', return_value=('天马', 170111101, (pet_catch.GOURD_ID,))),
            mock.patch.object(pet_catch, 'gourd_price', return_value=3),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, world, goal=1, resume=None, **options):
        session = core.Session(p.ClientConfig(uid=1, username='harvest-test', record_index=0))
        session._conn = world
        session.info = copy.deepcopy(world.info)
        def refresh():
            world.events.append(('refresh', None))
            if world.fail_refresh:
                world.fail_refresh = False
                raise p.AllianceError('服务器踢出了当前会话')
            session.info = copy.deepcopy(world.info)
            session._pets.clear()
            session._note_pets(session.info)
            return session.info
        session.refresh = refresh
        spec = core.load_tasks()['pet_cultivation']
        session.bind(spec)
        settings = {'levels': [79], 'harvest_times': goal, **options}
        if resume:
            settings['resume_done'] = resume
        ctx = core.TaskContext(session, settings)
        self.last_ctx = ctx
        with mock.patch.object(cultivation, 'pet_return_count', side_effect=lambda info, level: world.refund):
            try:
                return spec.run(ctx)
            finally:
                ctx.engage(False)

    @staticmethod
    def calls(world, route):
        return [body for r, body in world.sent if r == route]

    def test_single_role_counts_flowers_and_preserves_other_perks(self):
        w = World()
        r = self.execute(w, goal=2)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.count(cultivation.FLOWER), 2)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 2)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')
        self.assertEqual(w.info['bag']['pet']['new-1']['skill']['passivity'], {'8200710': 1})
        self.assertEqual(w.freed, [])

    def test_ready_upgrade_skips_remaining_batches_and_recovers_normally(self):
        w = ReadyWorld()
        result = self.execute(w)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 24)
        self.assertEqual(len(w.field_entries), 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')

    def test_unavailable_push_does_not_wait_and_scene_end_query_still_recovers(self):
        w = ReadyWorld(available=False)
        result = self.execute(w)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 49)
        self.assertEqual(len(w.field_entries), 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_ready_dual_upgrades_keep_both_rewards_and_original_formation(self):
        w = ReadyWorld(dual=True)
        result = self.execute(w, goal=2)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 24)
        self.assertEqual(w.count(cultivation.FLOWER), 2)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')
        self.assertNotIn('pet', w.info['p2']['equipments'])

    def test_ready_read_disconnect_resumes_without_replaying_experience(self):
        class DisconnectedReadyWorld(ReadyWorld):
            disconnected = False
            def drain_ready_pushes(self, routes, *, limit=64):
                if self.ready and not self.disconnected:
                    self.disconnected = True
                    raise p.AllianceError('补收时断线')
                return super().drain_ready_pushes(routes, limit=limit)
        w = DisconnectedReadyWorld()
        result = self.execute(w)
        self.assertEqual(result.status, core.PAUSED, result.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 24)
        resumed = self.execute(w, resume=result.progress)
        self.assertEqual(resumed.status, core.SUBMITTED, resumed.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 24)
        self.assertEqual(w.capture_count, 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')

    def test_linghua_run_cleans_reject_and_recovered_new_pet_only(self):
        w = World()
        w.seeds = [{'id': 170511101, 'capacity': 2, 'skill': {'passivity': {}}},
                   {'id': 170521101, 'quality': 2, 'capacity': 5}]
        with mock.patch.object(pet_catch, 'target', return_value=('灵花', 170511101, (pet_catch.GOURD_ID,))):
            r = self.execute(w, capture_pet=10002, auto_release=True, cleanup_rejected=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1', 'new-2'])
        self.assertEqual([b['mid'] for b in self.calls(w, dungeon.R_CREATE)], [10002, 10002, 79])
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertEqual(saved['captureMap'], 10002)
        self.assertEqual(saved['pets']['new-2']['initial']['petId'], 170521101)

    def test_linghua_run_refuses_wrong_family_without_freeing(self):
        w = World()
        r = self.execute(w, capture_pet=10002, auto_release=True, cleanup_rejected=True)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(w.freed, [])
        self.assertEqual(self.calls(w, cultivation.R_REMOVE), [])

    def test_invalid_capture_family_blocks_before_game_writes(self):
        w = World()
        r = self.execute(w, capture_pet=10003)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertEqual(w.sent, [])

    def test_linghua_resume_reconciles_without_replaying_experience(self):
        w = World()
        w.seeds = [{'id': 170511101}]
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, capture_pet=10002, auto_release=True)
        r = self.execute(w, resume=dict(self.last_ctx.progress), capture_pet=10002, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 1)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 1)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])

    def test_resume_cannot_change_capture_family(self):
        w = World()
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        before = len(w.sent)
        r = self.execute(w, resume=dict(self.last_ctx.progress), capture_pet=10002)
        self.assertEqual(r.status, core.PAUSED)
        self.assertIn('种类与断点不符', r.detail)
        self.assertEqual(len(w.sent), before)

    def test_capture_initial_snapshot_survives_growth_and_recovery(self):
        w = World()
        w.seeds = [{'skill': {'pet': {'4114120': 1}, 'passivity': {'8200610': 2}}}]
        r = self.execute(w, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        rec = saved['pets']['new-1']
        self.assertEqual(rec['initial']['level'], 1)
        self.assertEqual(rec['initial']['passivity'], {'8200610': 2})
        self.assertEqual(rec['initial']['captureAttempt'], 1)
        self.assertEqual(rec['phase'], 'released')
        self.assertNotEqual(rec['latest']['skill']['passivity'], rec['initial']['passivity'])

    def test_old_checkpoint_does_not_fabricate_initial_snapshot_from_grown_pet(self):
        w = World()
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        progress = dict(self.last_ctx.progress)
        path = next(Path(self.temp.name).rglob('gpt_*.json'))
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['pets']['new-1'].pop('initial')
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        r = self.execute(w, resume=progress)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertNotIn('initial', saved['pets']['new-1'])

    def test_real_string_pet_id_can_grow_recover_and_release(self):
        w = World()
        w.drop_count = 50
        w.seeds = [{'id': '170121101', 'quality': 2}]
        r = self.execute(w, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 1)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 24)
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertEqual(saved['pets']['new-1']['petId'], 170121101)

    def test_journal_lock_after_free_response_does_not_resend_free(self):
        w = World()
        replace = journal.os.replace
        denied = []
        sent_free = []
        def locked_after_free_once(source, destination):
            pending = json.loads(Path(source).read_text(encoding='utf-8')).get('pending') or {}
            if sent_free and not denied:  # 放生回执之后的第一次存盘被占用
                denied.append(True)
                raise PermissionError('temporary scanner lock')
            if pending.get('kind') == 'free':
                sent_free.append(True)
            return replace(source, destination)
        with mock.patch.object(journal.os, 'replace', side_effect=locked_after_free_once), mock.patch.object(journal.time, 'sleep'):
            r = self.execute(w, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(denied, [True])
        self.assertEqual(len(self.calls(w, pet_catch.R_FREE)), 1)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_string_pet_id_resumes_same_entity_without_recapture(self):
        w = World()
        w.seeds = [{'id': '170111101'}]
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        r = self.execute(w, resume=dict(self.last_ctx.progress), auto_release=False)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_string_unknown_pet_id_is_not_adopted_or_released(self):
        w = World()
        w.seeds = [{'id': '999999999'}]
        r = self.execute(w, cleanup_rejected=True, auto_release=True)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(w.freed, [])
        self.assertIn('new-1', w.info['bag']['pet'])

    def test_multiple_flowers_credit_only_one_pet_in_persisted_metrics(self):
        w = World()
        w.refund = 2
        result = self.execute(w, goal=2)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        paths = list(Path(self.temp.name).rglob('gpt_*.json'))
        self.assertEqual(len(paths), 1)
        saved = journal.load_file(paths[0])[0]
        contexts = saved['performance']['contexts'].values()
        self.assertEqual(sum(len(c['targets']) for c in contexts), 1)
        self.assertEqual(saved['harvested'], 2)

    def test_dual_role_prepares_two_before_first_upgrade(self):
        w = World(dual=True)
        r = self.execute(w, goal=2)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.field_entries[0]['captures'], 2)
        self.assertEqual(set(w.field_entries[0]['equipped']), {'new-1', 'new-2'})
        self.assertEqual(w.count(cultivation.FLOWER), 2)

    def test_dual_role_one_remaining_still_prepares_two_and_recovers_only_one(self):
        w = World(dual=True)
        r = self.execute(w)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 2)
        self.assertEqual(w.field_entries[0]['equipped'], ['new-1', 'new-2'])
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
        self.assertEqual(w.info['bag']['pet']['new-2']['skill']['passivity']['8200610'], 3)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')
        self.assertNotIn('pet', w.info['p2']['equipments'])

    def test_dual_role_odd_goal_keeps_two_in_last_round(self):
        w = World(dual=True)
        r = self.execute(w, goal=3)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 4)
        self.assertEqual(w.field_entries[0]['equipped'], ['new-1', 'new-2'])
        self.assertEqual(w.field_entries[-1]['equipped'], ['new-3', 'new-4'])
        self.assertEqual(w.count(cultivation.FLOWER), 3)
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')

    def test_dual_odd_goal_after_resume_keeps_two_in_last_round(self):
        w = World(dual=True)
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, goal=3)
        result = self.execute(w, goal=3, resume=dict(self.last_ctx.progress))
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(w.capture_count, 4)
        self.assertEqual(w.field_entries[-1]['equipped'], ['new-3', 'new-4'])
        self.assertEqual(w.count(cultivation.FLOWER), 3)

    def test_last_dual_companion_cleanup_respects_both_switches(self):
        for reached in (False, True):
            for auto in (False, True):
                for cleanup in (False, True):
                    with self.subTest(reached=reached, auto=auto, cleanup=cleanup):
                        w = World(dual=True)
                        w.info['bag']['pet']['old-2'] = w.raw('old-2', level=60, nickName='旧宠2')
                        w.info['p2']['equipments']['pet'] = w.info['bag']['pet']['old-2']
                        originals = copy.deepcopy(w.info['bag']['pet'])
                        if not reached:
                            w.growth['new-2'] = {'8200610': 1}
                        result = self.execute(w, auto_release=auto, cleanup_rejected=cleanup)
                        self.assertEqual(result.status, core.SUBMITTED, result.detail)
                        self.assertEqual(w.capture_count, 2)
                        self.assertEqual(w.count(cultivation.FLOWER), 1)
                        self.assertEqual([b['entityId'] for b in self.calls(w, cultivation.R_REMOVE)], ['new-1'])
                        self.assertEqual([pet['entityId'] for pet in w.freed],
                                         (['new-1'] if auto else [])+(['new-2'] if cleanup else []))
                        for entity, before in originals.items():
                            self.assertEqual(w.info['bag']['pet'][entity], before)
                        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')
                        self.assertEqual(w.info['p2']['equipments']['pet']['entityId'], 'old-2')

    def test_dual_one_goal_recovers_p2_when_only_second_pet_reaches_target(self):
        w = World(dual=True)
        w.growth['new-1'] = {'8200610': 1}
        result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual([b['entityId'] for b in self.calls(w, cultivation.R_REMOVE)], ['new-2'])
        self.assertEqual(set(pet['entityId'] for pet in w.freed), {'new-1', 'new-2'})
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(w.capture_count, 2)

    def test_completed_dual_goal_cleanup_resumes_without_repeating_free_or_remove(self):
        for reached in (False, True):
            for failure in ('lost_after', 'fail_refresh_after'):
                with self.subTest(reached=reached, failure=failure):
                    w = World(dual=True, forget=1)
                    if not reached:
                        w.growth['new-2'] = {'8200610': 1}
                    setattr(w, failure, pet_catch.R_FREE)
                    if failure == 'lost_after':
                        with self.assertRaises(core.UncertainOutcome):
                            self.execute(w, auto_release=False, cleanup_rejected=True)
                        progress = dict(self.last_ctx.progress)
                    else:
                        paused = self.execute(w, auto_release=False, cleanup_rejected=True)
                        self.assertEqual(paused.status, core.PAUSED, paused.detail)
                        progress = paused.progress
                    result = self.execute(w, resume=progress, auto_release=False, cleanup_rejected=True)
                    self.assertEqual(result.status, core.SUBMITTED, result.detail)
                    self.assertEqual(w.capture_count, 2)
                    self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
                    self.assertEqual([pet['entityId'] for pet in w.freed], ['new-2'])
                    self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')
                    self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_ambiguous_completed_goal_cleanup_stops_without_repeating_free(self):
        w = World(dual=True)
        w.lost_before = pet_catch.R_FREE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, cleanup_rejected=True)
        progress = dict(self.last_ctx.progress)
        sent = len(w.sent)
        result = self.execute(w, resume=progress, cleanup_rejected=True)
        self.assertEqual(result.status, core.PAUSED, result.detail)
        self.assertEqual(len(w.sent), sent)
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(w.freed, [])

    def test_last_dual_goal_lost_recovery_response_does_not_recover_companion(self):
        w = World(dual=True)
        w.lost_after = cultivation.R_REMOVE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, auto_release=True, cleanup_rejected=True)
        result = self.execute(w, resume=dict(self.last_ctx.progress), auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(w.capture_count, 2)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
        self.assertEqual([pet['entityId'] for pet in w.freed], ['new-1', 'new-2'])
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_legacy_last_single_pet_checkpoint_fills_second_slot_on_resume(self):
        w = World(dual=True)
        def old_roles(engine):
            return engine.d['roles'][:max(0, engine.d['goal']-engine.d['harvested'])]
        with mock.patch.object(harvest.Engine, 'active_roles', old_roles), mock.patch.object(
                harvest.Engine, 'field', side_effect=p.AllianceError('旧版本上阵后中断')):
            paused = self.execute(w)
        self.assertEqual(paused.status, core.PAUSED, paused.detail)
        self.assertEqual(w.capture_count, 1)
        result = self.execute(w, resume=paused.progress)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(w.capture_count, 2)
        self.assertEqual(w.field_entries[0]['equipped'], ['new-1', 'new-2'])
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_dual_vip_below_three_last_goal_recovers_only_one_four(self):
        w = World(dual=True, vip=2)
        result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(w.capture_count, 2)
        self.assertEqual([b['skLev'] for b in self.calls(w, cultivation.R_REMOVE)], [4])
        self.assertEqual([pet['entityId'] for pet in w.freed], ['new-1', 'new-2'])
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_missing_forget_flower_stops_before_capture_or_buy(self):
        w = World(forget=0, warehouse=0, gourds=0)
        r = self.execute(w)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertEqual(w.sent, [])

    def test_warehouse_borrow_consumption_and_unused_return(self):
        w = World(forget=0, warehouse=10)
        w.refund = 2
        r = self.execute(w, goal=3)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.count(cultivation.FLOWER), 4)  # 每朵计次，一次回收多朵不可拆分。
        self.assertEqual(w.count(cultivation.FORGET_FLOWER), 0)
        self.assertEqual(w.count(cultivation.FORGET_FLOWER, warehouse=True), 8)
        self.assertEqual(self.calls(w, ledger.R_TO)[0]['count'], 1)

    def test_auto_release_only_confirmed_recovered_pet(self):
        w = World()
        r = self.execute(w, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        self.assertEqual(w.freed[0]['skill']['passivity'], {'8200710': 1})
        self.assertIn('old', w.info['bag']['pet'])

    def test_cleanup_rejects_is_separate_from_auto_release(self):
        w = World()
        w.seeds = [{'capacity': 2, 'skill': {'passivity': {'8200610': 1, '8200710': 1}}}]
        r = self.execute(w, cleanup_rejected=True, auto_release=False)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        self.assertIn('new-2', w.info['bag']['pet'])

    def test_fresh_reject_is_freed_after_room_settlement_before_first_refresh(self):
        w = World()
        w.seeds = [{'capacity': 1}]
        r = self.execute(w, cleanup_rejected=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        free = w.events.index(('write', pet_catch.R_FREE))
        finish = w.events.index(('write', dungeon.R_FINISH))
        self.assertLess(finish, free)
        self.assertNotIn(('refresh', None), w.events[finish+1:free])
        self.assertEqual(w.events[free+1], ('write', dungeon.R_CREATE))
        training = next(i for i, event in enumerate(w.events) if event == ('write', cultivation.R_EQUIP))
        self.assertIn(('refresh', None), w.events[free+1:training])
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertEqual(saved['freshCaptureReleases'], 1)
        self.assertEqual(saved['pets']['new-1']['phase'], 'released')
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        self.assertEqual(w.info['bag']['pet']['old']['level'], 50)

    def test_target_in_first_room_does_not_create_remaining_rooms(self):
        w = World()
        with mock.patch.object(dungeon, 'level', return_value=dungeon.Level(79, dungeon.MAIN, '辞衡堂', (79, 179))):
            result = self.execute(w)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertNotIn(179, [b['mid'] for r, b in w.sent if r == dungeon.R_CREATE])

    def test_fresh_reject_without_cleanup_is_retained_and_queried(self):
        w = World();w.seeds = [{'capacity': 1}]
        r = self.execute(w, cleanup_rejected=False, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertIn('new-1', w.info['bag']['pet'])
        self.assertEqual(w.freed[0]['entityId'], 'new-2')
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertNotIn('freshCaptureReleases', saved)

    def test_background_interleave_requires_query_before_free(self):
        w = World();w.seeds = [{'capacity': 1}]
        pump = core.Session.pump
        def background(session):
            session.hook('test_background', lambda: None)
            return pump(session)
        with mock.patch.object(core.Session, 'pump', background):
            result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        finish = w.events.index(('write', dungeon.R_FINISH))
        free = w.events.index(('write', pet_catch.R_FREE))
        self.assertIn(('refresh', None), w.events[finish+1:free])
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertNotIn('freshCaptureReleases', saved)

    def test_fresh_reject_lost_free_response_reconciles_without_resending(self):
        w = World();w.seeds = [{'capacity': 1}];w.lost_after = pet_catch.R_FREE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, cleanup_rejected=True)
        resume = dict(self.last_ctx.progress)
        result = self.execute(w, resume=resume, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        self.assertEqual(w.capture_count, 2)

    def test_fresh_reject_ack_before_failed_refresh_is_not_freed_twice(self):
        w = World();w.seeds = [{'capacity': 1}];w.fail_refresh_after = pet_catch.R_FREE
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.PAUSED)
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertNotEqual((saved['pending'] or {}).get('kind'), 'free')
        self.assertEqual(saved['pets']['new-1']['phase'], 'released_unverified')
        self.assertTrue(saved['pets']['new-1']['freshReleaseAck'])
        result = self.execute(w, resume=dict(self.last_ctx.progress), cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])

    def test_fresh_release_window_queries_after_eight_before_next_capture(self):
        w = World();w.seeds = [{'capacity': 1} for _ in range(9)]
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        frees = [i for i, event in enumerate(w.events) if event == ('write', pet_catch.R_FREE)]
        self.assertEqual(w.events[frees[7]+1], ('refresh', None))
        self.assertNotIn(('refresh', None), w.events[frees[0]+1:frees[7]])
        self.assertEqual([p['entityId'] for p in w.freed], [f'new-{n}' for n in range(1, 10)])

    def test_dual_candidates_share_one_query_and_do_not_overcapture(self):
        w = World(dual=True)
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        captures = [i for i, e in enumerate(w.events) if e == ('write', pet_catch.R_CAPTURE)]
        takeoff = w.events.index(('write', cultivation.R_TAKEOFF))
        self.assertEqual(w.capture_count, 2)
        self.assertNotIn(('refresh', None), w.events[captures[0]+1:takeoff])  # 两只候选都留到上阵时一起查
        self.assertEqual(w.events[takeoff+1], ('refresh', None))

    def test_single_slot_candidate_is_verified_by_the_equip_query(self):
        w = World();w.info['p1']['equipments'].pop('pet');w.seeds = [{'capacity': 1}]
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        capture = [i for i, e in enumerate(w.events) if e == ('write', pet_catch.R_CAPTURE)][1]
        equip = w.events.index(('write', cultivation.R_EQUIP))
        # 落选的 new-1 放了没查、候选 new-2 抓了没查，都留给上阵后那一次查询
        self.assertNotIn(('refresh', None), w.events[capture+1:equip])
        pickup = w.events.index(('write', dungeon.R_PICKUP))
        self.assertEqual(w.events[equip+1:pickup].count(('refresh', None)), 1)
        self.assertEqual([p['entityId'] for p in w.freed][:1], ['new-1'])

    def test_finished_pet_is_swapped_out_by_next_candidate_without_takeoff(self):
        w = World()
        w.growth = {'new-1': {'8200610': 1, '8200710': 1, '8200810': 1, '8200910': 1}}  # 一场后满槽落选
        result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        writes = [(i, e[1]) for i, e in enumerate(w.events) if e[0] == 'write']
        equips = [i for i, route in writes if route == cultivation.R_EQUIP]
        first_pickup = w.events.index(('write', dungeon.R_PICKUP))
        between = [route for i, route in writes if first_pickup < i < equips[1]]
        self.assertNotIn(cultivation.R_TAKEOFF, between)  # new-1 没先卸
        frees = [i for i, e in enumerate(w.events) if e == ('write', pet_catch.R_FREE)]
        free_new1 = frees[[p['entityId'] for p in w.freed].index('new-1')]
        self.assertGreater(free_new1, equips[1])  # 换下来以后才放生
        self.assertIn(('refresh', None), w.events[equips[1]+1:free_new1])  # 放生前有一次查询核验
        self.assertEqual(w.info['p1']['equipments']['pet']['entityId'], 'old')

    def _with_exp_table(self):
        from zmxy import data
        real = data.row
        return mock.patch.object(data, 'row', side_effect=lambda table, key: {'upgradeEXP': 150} if table == 'PetEXP' else real(table, key))

    def _field_gaps(self, w):
        """每场结束（结算）到下一场开始之间有没有查询。"""
        creates = [i for i, e in enumerate(w.events) if e == ('write', dungeon.R_CREATE)]
        fields = [i for i, (r, b) in zip(creates, [x for x in w.sent if x[0] == dungeon.R_CREATE]) if b['mid'] == 79]
        return [(('refresh', None) in w.events[a:b]) for a, b in zip(fields, fields[1:])]

    def test_continuing_pet_skips_query_after_field_when_prediction_matches(self):
        w = TableWorld(target_at=5)
        with self._with_exp_table():
            result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        gaps = self._field_gaps(w)
        self.assertGreaterEqual(len(gaps), 2)
        self.assertIn(False, gaps)  # 至少有一场后面没查询就接着刷
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        contexts = saved['performance']['contexts'].values()
        self.assertGreater(sum(c.get('fieldQueriesSkipped', 0) for c in contexts), 0)
        self.assertEqual(sum(c.get('predictionMismatches', 0) for c in contexts), 0)
        self.assertEqual(w.count(cultivation.FLOWER), 1)  # 达标那场照常查询并回收

    def test_upgrade_push_arriving_during_short_wait_still_skips_query(self):
        w = TableWorld(target_at=5, hold_pushes=True, arrive_on_poll=True)
        with self._with_exp_table():
            result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertGreater(w.polls, 0)
        self.assertIn(False, self._field_gaps(w))

    def test_upgrade_push_arriving_with_settlement_still_skips_query(self):
        w = TableWorld(target_at=5, hold_pushes=True, with_next_request=True)
        with self._with_exp_table():
            result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(w.polls, 0)  # 推送跟着结算请求到了，不用再等
        self.assertIn(False, self._field_gaps(w))

    def test_late_upgrade_push_falls_back_to_query(self):
        w = TableWorld(target_at=5, hold_pushes=True)  # 升级推送一直不到
        with self._with_exp_table():
            result = self.execute(w, auto_release=True, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertNotIn(False, self._field_gaps(w))  # 每场后都查询

    def test_changed_single_slot_candidate_stops_before_field(self):
        w = World();w.info['p1']['equipments'].pop('pet')
        request = w.request
        def change(route, body):
            result = request(route, body)
            if route == dungeon.R_FINISH and w.capture_count == 1:
                w.info['bag']['pet']['new-1']['skill']['passivity']['8200610'] = 2
            return result
        w.request = change
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.PAUSED)
        self.assertIn('短批候选实体未核验', result.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 0)
        self.assertEqual(w.freed, [])
        self.assertIn('new-1', w.info['bag']['pet'])

    def test_unverified_first_candidate_resumes_without_recapturing_it(self):
        w = World(dual=True)
        request = w.request
        def disconnect(route, body):
            result = request(route, body)
            if route == dungeon.R_FINISH and w.capture_count == 1:
                w.lost_before = dungeon.R_CREATE
            return result
        w.request = disconnect
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, cleanup_rejected=True)
        saved = journal.load_file(next(Path(self.temp.name).rglob('gpt_*.json')))[0]
        self.assertTrue(saved['pets']['new-1']['captureUnverified'])
        again = self.execute(w, resume=dict(self.last_ctx.progress), cleanup_rejected=True)
        self.assertEqual(again.status, core.SUBMITTED, again.detail)
        self.assertEqual(w.capture_count, 2)

    def test_changed_candidate_in_shared_query_stops_before_equipping(self):
        w = World(dual=True)
        request = w.request
        def change(route, body):
            result = request(route, body)
            if route == dungeon.R_FINISH and w.capture_count == 1:
                w.info['bag']['pet']['new-1']['skill']['passivity']['8200610'] = 2
            return result
        w.request = change
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.PAUSED)
        self.assertIn('短批候选实体未核验', result.detail)
        self.assertEqual(len(self.calls(w, cultivation.R_EQUIP)), 0)
        self.assertIn('new-1', w.info['bag']['pet'])

    def test_fresh_release_window_is_bounded_by_free_bag_slots(self):
        w = World();w.info['bag']['petMaxNum'] = 3
        w.seeds = [{'capacity': 1} for _ in range(3)]
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        frees = [i for i, event in enumerate(w.events) if event == ('write', pet_catch.R_FREE)]
        self.assertEqual(w.events[frees[1]+1], ('refresh', None))

    def test_gourds_remain_conservative_without_per_capture_refresh(self):
        w = World(gourds=3);w.seeds = [{'capacity': 1} for _ in range(3)]
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(w.capture_count, 4)
        self.assertEqual(len(self.calls(w, pet_catch.R_BUY)), 1)
        self.assertEqual(self.calls(w, pet_catch.R_BUY)[0]['count'], 9999)
        buy = w.events.index(('write', pet_catch.R_BUY))
        self.assertEqual(sum(event == ('write', pet_catch.R_CAPTURE) for event in w.events[:buy]), 3)

    def test_acknowledged_but_retained_fresh_pet_blocks_before_training_and_no_replay(self):
        w = World();w.seeds = [{'capacity': 1}]
        request = w.request
        def retain(route, body):
            if route == pet_catch.R_FREE:
                w.events.append(('write', route));w.sent.append((route, dict(body)))
                return {'code': 200}
            return request(route, body)
        w.request = retain
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.PAUSED)
        self.assertIn('短批结果未核验', result.detail)
        self.assertEqual(len(self.calls(w, cultivation.R_EQUIP)), 0)
        before = len(w.sent)
        again = self.execute(w, resume=dict(self.last_ctx.progress), cleanup_rejected=True)
        self.assertEqual(again.status, core.PAUSED)
        self.assertEqual(len(w.sent), before)
        self.assertIn('new-1', w.info['bag']['pet'])

    def test_exhausted_local_gourd_budget_refreshes_before_purchase(self):
        w = World(gourds=3);w.seeds = [{'capacity': 1} for _ in range(3)]
        request = w.request
        def conservative(route, body):
            response = request(route, body)
            if route == pet_catch.R_CAPTURE and w.capture_count <= 3:
                w.add(pet_catch.GOURD_ID, 1)
            return response
        w.request = conservative
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(len(self.calls(w, pet_catch.R_BUY)), 0)

    def test_changed_fresh_entity_is_preserved_before_free(self):
        w = World();w.seeds = [{'capacity': 1}]
        original = core.Session.pet_entity
        def altered(session, entity):
            raw = original(session, entity)
            if entity == 'new-1' and raw:
                raw = copy.deepcopy(raw);raw['capacity'] = 5
            return raw
        with mock.patch.object(core.Session, 'pet_entity', altered):
            result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.PAUSED)
        self.assertEqual(w.freed, [])
        self.assertIn('new-1', w.info['bag']['pet'])

    def test_role_cap_skips_later_batches_and_replaces_only_capped_pet(self):
        w = World();w.info['p1']['level'] = 10;w.drop_count = 49
        w.growth['new-1'] = {'8200610': 1}
        result = self.execute(w, cleanup_rejected=True)
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        first_main = next(i for i, (r, b) in enumerate(w.sent) if r == dungeon.R_CREATE and b['mid'] == 79)
        finish = next(i for i in range(first_main+1, len(w.sent)) if w.sent[i][0] == dungeon.R_FINISH)
        self.assertEqual(sum(r == dungeon.R_PICKUP for r, _ in w.sent[first_main:finish]), 24)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])

    def test_vip_below_three_uses_four_and_three_is_reject_not_protected(self):
        w = World(vip=2)
        w.growth['new-1'] = {'8200610': 3, '8200710': 1, '8200810': 1, '8200910': 1}
        r = self.execute(w, cleanup_rejected=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        self.assertEqual(self.calls(w, cultivation.R_REMOVE)[0]['skLev'], 4)

    def test_no_old_counter_or_combined_release_option_is_used(self):
        w = World()
        r = self.execute(w, times=0, capture_times=0, release_after_100=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.count(cultivation.FLOWER), 1)
        self.assertEqual(w.freed, [])

    def test_auto_buy_9999_gourds_with_spirit(self):
        w = World(gourds=0)
        r = self.execute(w)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        buy = self.calls(w, pet_catch.R_BUY)
        self.assertEqual([(b['shopId'], b['count']) for b in buy], [(1, 9999)])
        self.assertEqual(w.info['spirit'], 100000 - 9999*3)

    def test_not_enough_spirit_does_not_buy_partial_or_premium(self):
        w = World(gourds=0, spirit=1)
        r = self.execute(w)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(w.sent, [])

    def test_count_is_receipt_not_number_of_removals(self):
        w = World()
        w.refund = 2
        r = self.execute(w, goal=2)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
        self.assertEqual(w.count(cultivation.FLOWER), 2)

    def test_kick_after_remove_before_refresh_resumes_without_duplicate(self):
        w = World()
        w.fail_refresh_after = cultivation.R_REMOVE
        r = self.execute(w, auto_release=True)
        self.assertEqual(r.status, core.PAUSED)
        from zmxy.store import Store
        store = Store(Path(self.temp.name) / 'settings.json')
        account = {'name': '测试', 'username': 'harvest-test', 'uid': 1,
                   'saves': {'1': {'tasks': ['pet_cultivation'], 'options': {
                       'pet_cultivation': {'levels': [79], 'harvest_times': 1, 'auto_release': True}}}}}
        store.data['accounts'] = [account]
        store.add_resume(account, 1, ['pet_cultivation'])
        store.add_progress(account, 1, 'pet_cultivation', dict(r.progress))
        store.save()
        restored = Store(store.path).resume_accounts({}, lambda acc: None)
        self.assertEqual(len(restored), 1)
        progress = restored[0].options['pet_cultivation']['resume_done']
        self.assertTrue(any(k.startswith(journal.PREFIX) for k in progress))
        r = self.execute(w, resume=progress, auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
        self.assertEqual(len(w.freed), 1)

    def test_lost_remove_response_reconciles_from_state(self):
        w = World()
        w.lost_after = cultivation.R_REMOVE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        r = self.execute(w, resume=dict(self.last_ctx.progress))
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, cultivation.R_REMOVE)), 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_ambiguous_remove_is_not_automatically_retried(self):
        w = World()
        w.lost_before = cultivation.R_REMOVE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        n = len(w.sent)
        r = self.execute(w, resume=dict(self.last_ctx.progress))
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(len(w.sent), n)

    def test_lost_purchase_response_does_not_buy_twice(self):
        w = World(gourds=0)
        w.lost_after = pet_catch.R_BUY
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        r = self.execute(w, resume=dict(self.last_ctx.progress))
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, pet_catch.R_BUY)), 1)

    def test_lost_free_response_does_not_free_another_pet(self):
        w = World()
        w.lost_after = pet_catch.R_FREE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, auto_release=True)
        r = self.execute(w, resume=dict(self.last_ctx.progress), auto_release=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])

    def test_unconfirmed_capture_entity_is_preserved_on_resume(self):
        w = World()
        w.lost_after = pet_catch.R_CAPTURE
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, auto_release=True, cleanup_rejected=True)
        r = self.execute(w, resume=dict(self.last_ctx.progress), auto_release=True, cleanup_rejected=True)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertIn('new-1', w.info['bag']['pet'])
        self.assertEqual([p['entityId'] for p in w.freed], ['new-2'])

    def test_batch_stops_after_complete_target_push_without_per_drop_queries(self):
        w = World()
        w.drop_count = 50
        r = self.execute(w)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 24)

    def test_ui_has_only_harvest_count_two_independent_disposal_switches(self):
        ch = core.load_tasks()['pet_cultivation'].choices
        self.assertFalse(ch.times)
        self.assertIsNone(ch.items)
        self.assertEqual(ch.button, '设置')
        self.assertEqual([x.key for x in ch.picks], ['harvest_times'])
        self.assertEqual({k for k, _ in ch.toggles}, {'auto_release', 'cleanup_rejected'})
        self.assertIn('VIP3 及以上 3 级，以下 4 级', ch.note)
        self.assertIn('理论约 120 只', ch.note)
        self.assertIn('4 级：理论约 1500 只', ch.note)
        self.assertNotIn('89只', ch.note)
        self.assertNotIn('1064只', ch.note)
        self.assertNotIn('约3分钟', ch.note)
        self.assertNotIn('约33分钟', ch.note)
        self.assertNotIn('93只', ch.note)
        self.assertNotIn('999只', ch.note)
        self.assertIn('不含没捕到的', ch.note)
        self.assertNotIn('10倍', ch.note)

    def test_old_manual_qixing_selection_is_ignored(self):
        w = World()
        r = self.execute(w, levels=[80])
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual([b['mid'] for b in self.calls(w, dungeon.R_CREATE) if b['mid'] != cultivation.CAPTURE_PET], [79])

    def test_qixing_is_absent_from_cultivation_catalog(self):
        # setUp替换的是自动选择的候选目录，直接调用原函数验证真实过滤规则。
        original = self.patches[1].temp_original
        with mock.patch.object(dungeon, 'catalog', return_value=[(78, '蓬丘'), (79, '辞衡堂'), (80, '七星殿')]):
            self.assertEqual(original(), [(79, '辞衡堂')])

    def test_latest_unlocked_mainline_skips_known_locks(self):
        with mock.patch.object(cultivation, 'map_catalog', return_value=[(77, '旧关'), (79, '新关')]), \
             mock.patch.object(cultivation.quota, 'locked', side_effect=lambda info, mid: '前置关卡77未通关' if mid == 79 else None):
            self.assertEqual(cultivation.latest_level(World().info).mid, 77)

    def test_unknown_newest_unlock_state_blocks_without_side_effects(self):
        w = World()
        with mock.patch.object(cultivation.quota, 'locked', return_value='无法读取通关记录（dungeon）'):
            r = self.execute(w)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertEqual(w.sent, [])

    def test_no_eligible_mainline_blocks_before_material_withdrawal(self):
        w = World(forget=0, warehouse=5)
        with mock.patch.object(cultivation, 'map_catalog', return_value=[]):
            r = self.execute(w)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertEqual(w.sent, [])

    def test_field_reselects_latest_instead_of_freezing_startup_map(self):
        w = World()
        levels = [dungeon.Level(77, dungeon.MAIN, '旧关', (77,)), dungeon.Level(79, dungeon.MAIN, '辞衡堂', (79,))]
        with mock.patch.object(cultivation, 'latest_level', side_effect=levels) as choose:
            r = self.execute(w)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(choose.call_count, 2)
        self.assertEqual([b['mid'] for b in self.calls(w, dungeon.R_CREATE) if b['mid'] != cultivation.CAPTURE_PET], [79])

    def test_old_checkpoint_excluded_map_preserves_pet_and_resumes_automatically(self):
        w = World()
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        progress = dict(self.last_ctx.progress)
        path = next(Path(self.temp.name).rglob('gpt_*.json'))
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['map'] = 61  # 旧手选蓬丘断点不可再锁定后续培养关卡。
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        r = self.execute(w, resume=progress)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 1)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 1)
        self.assertEqual(w.count(cultivation.FLOWER), 1)

    def test_unknown_auto_map_quota_stops_before_capture(self):
        w = World()
        with mock.patch.object(cultivation.quota, 'of', return_value=SimpleNamespace(left=None)):
            r = self.execute(w)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertEqual(w.sent, [])

    def test_vip_three_boundary_uses_rank_three(self):
        w = World(vip=3)
        r = self.execute(w)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(self.calls(w, cultivation.R_REMOVE)[0]['skLev'], 3)

    def test_interrupted_experience_batch_resumes_same_pet_without_replaying(self):
        w = World()
        w.lost_after = dungeon.R_PICKUP
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        r = self.execute(w, resume=dict(self.last_ctx.progress))
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.capture_count, 1)
        self.assertEqual(len(self.calls(w, dungeon.R_PICKUP)), 1)

    def test_lost_warehouse_withdrawal_is_not_repeated(self):
        w = World(forget=0, warehouse=5)
        w.lost_after = ledger.R_FROM
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w)
        r = self.execute(w, resume=dict(self.last_ctx.progress))
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, ledger.R_FROM)), 1)

    def test_lost_warehouse_return_is_not_repeated(self):
        w = World(forget=0, warehouse=10)
        w.refund = 2
        w.lost_after = ledger.R_TO
        with self.assertRaises(core.UncertainOutcome):
            self.execute(w, goal=3)
        r = self.execute(w, goal=3, resume=dict(self.last_ctx.progress))
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(len(self.calls(w, ledger.R_TO)), 1)

    def test_non_target_perk_change_blocks_count_and_auto_release(self):
        w = World()
        original = w.request
        def changed(route, body):
            reply = original(route, body)
            if route == cultivation.R_REMOVE:
                w.info['bag']['pet'][body['entityId']]['skill']['passivity']['8200710'] = 2
            return reply
        w.request = changed
        r = self.execute(w, auto_release=True)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(self.last_ctx.progress['harvested'], 0)
        self.assertEqual(w.freed, [])

    def test_resume_never_adopts_an_old_pet_into_disposal_list(self):
        w = World()
        w.fail_refresh_after = cultivation.R_REMOVE
        r = self.execute(w, auto_release=True)
        path = next(Path(self.temp.name).rglob('gpt_*.json'))
        import json
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['pets']['old'] = {'entityId': 'old', 'petId': 170111101, 'nickname': '旧宠', 'phase': 'recovered'}
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        r = self.execute(w, resume=r.progress, auto_release=True)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(w.freed, [])

    def test_full_pet_storage_stops_before_auto_purchase(self):
        w = World(gourds=0)
        w.info['bag']['petMaxNum'] = 1
        r = self.execute(w)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual(w.sent, [])

    def test_locked_map_stops_before_withdrawal_or_capture(self):
        w = World(forget=0, warehouse=5)
        with mock.patch.object(cultivation.quota, 'locked', return_value='未解锁'):
            r = self.execute(w)
        self.assertEqual(r.status, core.BLOCKED)
        self.assertEqual(w.sent, [])

    def test_existing_flowers_do_not_count_as_new_harvests(self):
        w = World()
        w.add(cultivation.FLOWER, 10)
        r = self.execute(w, goal=2)
        self.assertEqual(r.status, core.SUBMITTED, r.detail)
        self.assertEqual(w.count(cultivation.FLOWER), 12)
        self.assertIn('益灵花2/2', r.detail)

    def test_resumed_recovered_pet_can_be_released_before_material_exhaustion_stop(self):
        w = World(forget=1)
        w.fail_refresh_after = cultivation.R_REMOVE
        r = self.execute(w, goal=2, auto_release=True)
        self.assertEqual(r.status, core.PAUSED)
        r = self.execute(w, goal=2, resume=r.progress, auto_release=True)
        self.assertEqual(r.status, core.PAUSED)
        self.assertEqual([p['entityId'] for p in w.freed], ['new-1'])
        self.assertEqual(w.capture_count, 1)



class RestoreFormationTests(unittest.TestCase):
    def test_unreadable_lineup_after_takeoff_halts_with_reason(self):
        run = object.__new__(harvest.Engine)
        info = {"p1": {"equipments": {"pet": {"entityId": "new"}}}}
        run.ctx = SimpleNamespace(info=info)
        run.d = {"originalFormation": {"0": "old"}, "pets": {"new": {}}}
        run.takeoff = lambda slot: setattr(run.ctx, "info", {})   # 卸下后服务器给的角色信息里没有阵容
        run.equip = mock.Mock()
        with self.assertRaises(ledger.Halt) as caught:
            run.restore_formation()
        self.assertIn("读不到出战宠物", str(caught.exception))
        run.equip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
