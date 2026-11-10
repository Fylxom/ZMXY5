"""验证培养计时归属、独立经验与保守运行校准，不连接游戏。"""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from zmxy import core, protocol as protocol
from zmxy.tasks.pet_cultivation import task as cultivation
from zmxy.tasks.pet_cultivation import metrics
from zmxy.tasks.pet_cultivation import policy
from zmxy.tasks.pet_cultivation.harvest import Engine


def pet(ranks, level=1, exp=0):
    return {'entityId': 'new', 'id': 170111101, 'nickName': '', 'capacity': 5, 'hpQualification': 500,
            'level': level, 'petEXP': exp, 'skill': {'passivity': {str(i): n for i, n in enumerate(ranks)}}}


class MetricsTests(unittest.TestCase):
    def make_engine(self, ranks=(1, 1, 1, 1), changed=True, interrupted=False):
        current = pet(ranks, 30)
        before = pet((1,) if changed else ranks, 29)
        info = {'p1': {'level': 100}, 'bag': {'pet': {'new': current}}}
        rec = {'petId': current['id'], 'nickname': '', 'stateKey': metrics.state_key(before),
               'stateSeconds': 0.0, 'phase': 'growing', 'latest': copy.deepcopy(before)}
        d = {'map': 79, 'target': 4, 'goal': 3, 'harvested': 0, 'roles': ['0'], 'originalIds': [],
             'selected': {'0': 'new'}, 'pets': {'new': rec}, 'fields': 0}
        ctx = SimpleNamespace(info=info, progress={})
        engine = Engine(ctx, SimpleNamespace(data=d, save=mock.Mock()))
        d['field'] = {'settled': True, 'seconds': 3.0, 'before': {'new': before},
                      'experience': {'0': 100}, 'context': engine.stats()['key'], 'interrupted': interrupted}
        return engine, rec

    def test_new_state_does_not_inherit_old_field_time(self):
        engine, rec = self.make_engine()
        engine.settle_field_state()
        self.assertEqual(rec['stateSeconds'], 0)
        self.assertEqual(rec['phase'], 'growing')  # 旧实现会用3秒超过2.309秒预算而淘汰。

    def test_complete_push_charges_only_observed_new_state_tail(self):
        engine, rec = self.make_engine()
        engine.d['field']['transitions'] = {'new': {'key': metrics.state_key(engine.pet('new')), 'elapsed': 2.5}}
        engine.settle_field_state()
        self.assertEqual(rec['stateSeconds'], .5)
        self.assertEqual(rec['phase'], 'growing')

    def test_unchanged_state_accumulates_and_still_has_finite_budget(self):
        engine, rec = self.make_engine(changed=False)
        engine.settle_field_state()
        self.assertEqual(rec['stateSeconds'], 3)
        self.assertEqual(rec['phase'], 'rejected')

    def test_interruption_does_not_trigger_economic_rejection(self):
        engine, rec = self.make_engine(changed=False, interrupted=True)
        engine.d['field']['seconds'] = 100000
        engine.settle_field_state()
        self.assertEqual(rec['stateSeconds'], 0)
        self.assertEqual(rec['phase'], 'growing')
        self.assertEqual(engine.stats()['verifiedFields'], 0)

    def test_old_checkpoint_without_before_snapshot_cold_starts(self):
        engine, rec = self.make_engine(changed=False)
        engine.d['field'].pop('before')
        rec['stateSeconds'] = 500
        engine.settle_field_state()
        self.assertEqual(rec['phase'], 'growing')
        self.assertEqual(rec['stateSeconds'], 0)

    def test_cap_limit_still_rejects_after_interruption(self):
        engine, rec = self.make_engine(interrupted=True)
        engine.ctx.info['p1']['level'] = 30
        engine.settle_field_state()
        self.assertEqual(rec['phase'], 'rejected')

    def test_verified_experience_uses_level_threshold_not_level_count(self):
        engine, rec = self.make_engine()
        with mock.patch.object(cultivation.data, 'row', return_value={'upgradeEXP': 100}):
            engine.settle_field_state()
        self.assertEqual(engine.stats()['verifiedFields'], 1)
        self.assertTrue(engine.stats()['lastExperience']['verified'])

    def test_missing_exp_table_only_disables_calibration(self):
        engine, rec = self.make_engine()
        with mock.patch.object(cultivation.data, 'row', side_effect=cultivation.data.DataError('missing')):
            engine.settle_field_state()
        self.assertEqual(rec['phase'], 'growing')
        self.assertFalse(engine.stats()['lastExperience']['verified'])

    def test_response_exp_separates_roles_and_ignores_unrelated_rewards(self):
        rows = [{'give': [{'subType': 'petEXP', 'entityId': 'index:0', 'count': 100},
                         {'subType': 'petEXP', 'entityId': 'index:1', 'count': 50},
                         {'subType': 'exp', 'entityId': 'index:0', 'count': 900}]}]
        self.assertEqual(metrics.response_exp(rows), {'0': 100, '1': 50})

    def test_context_separates_single_dual_and_buff_changes(self):
        data = {'map': 79, 'target': 3}
        info = {'p1': {'level': 100}, 'p2': {'level': 90}}
        a = metrics.bucket(data, info, ['0'])
        b = metrics.bucket(data, info, ['0', '1'])
        self.assertIsNot(a, b)
        info['buffs'] = {'extra': 2}
        self.assertIsNot(a, metrics.bucket(data, info, ['0']))

    def test_slow_observed_run_relaxes_budget_after_warmup(self):
        stats = metrics.bucket({'map': 79, 'target': 3}, {'p1': {'level': 100}}, ['0'])
        metrics.cost(stats, 'pickup_batch', 1000)
        self.assertEqual(metrics.rate(stats, 3), metrics.BASE_RATE[3])
        stats.update(captures=8, verifiedFields=3)
        self.assertLess(metrics.rate(stats, 3), metrics.BASE_RATE[3])
        p = pet((2, 1))
        self.assertGreater(policy.state_budget(p, 3, metrics.rate(stats, 3)), policy.state_budget(p, 3))

    def test_censored_growth_not_used_as_fast_complete_estimate(self):
        stats = {'growth': {}}
        for seconds in (3, 4, 5):
            metrics.growth(stats, 'example', seconds, True)
        self.assertAlmostEqual(metrics.estimate(stats, 'example'), 5.35)
        metrics.growth(stats, 'example', 10, False)
        self.assertIsNone(metrics.estimate(stats, 'example'))

    def test_new_actual_reward_spec_discards_old_speed_calibration(self):
        stats = metrics.bucket({'map': 79, 'target': 3}, {'p1': {'level': 100}}, ['0'])
        metrics.calibrate(stats, {'0': [100]})
        stats.update(captures=20, verifiedFields=20, seconds=1000, targets=['a'])
        metrics.calibrate(stats, {'0': [500]})
        self.assertEqual(stats['captures'], 0)
        self.assertEqual(stats['verifiedFields'], 0)
        self.assertEqual(stats['targets'], [])
        self.assertEqual(metrics.rate(stats, 3), metrics.BASE_RATE[3])

    def test_normal_base_and_boss_drop_mix_does_not_reset_calibration(self):
        stats = metrics.bucket({'map': 79, 'target': 3}, {'p1': {'level': 100}}, ['0'])
        metrics.calibrate(stats, {'0': [11634]})
        stats.update(captures=100, fields=30, verifiedFields=30, seconds=300, targets=['one'])
        for signature in ({'0': [11634, 143139]}, {'0': [11634]}, {'0': [11634, 143139]}):
            self.assertFalse(metrics.reward_changed(stats, signature))
            metrics.calibrate(stats, signature)
        self.assertEqual(stats['captures'], 100)
        self.assertEqual(stats['verifiedFields'], 30)
        self.assertEqual(stats['targets'], ['one'])
        self.assertEqual(stats.get('rewardEpoch', 0), 0)

    def test_real_reward_change_keeps_previous_epoch_summary(self):
        stats = metrics.bucket({'map': 79, 'target': 3}, {'p1': {'level': 100}}, ['0'])
        metrics.calibrate(stats, {'0': [100]})
        stats.update(captures=12, fields=3, verifiedFields=3, seconds=50, targets=['one'])
        metrics.calibrate(stats, {'0': [200]})
        self.assertEqual(stats['previousEpochs']['captures'], 12)
        self.assertEqual(stats['previousEpochs']['targets'], 1)
        self.assertEqual(stats['previousEpochs']['seconds'], 50)

    def test_activity_clock_survives_resume_but_excludes_pause_gap(self):
        engine, _ = self.make_engine()
        engine.clock_mark = 100
        with mock.patch('zmxy.tasks.pet_cultivation.ledger.time.monotonic', return_value=110):
            engine.tick()
        self.assertEqual(engine.d['activeSeconds'], 10)
        with mock.patch('zmxy.tasks.pet_cultivation.ledger.time.monotonic', return_value=500):
            resumed = Engine(engine.ctx, engine.j)
        with mock.patch('zmxy.tasks.pet_cultivation.ledger.time.monotonic', return_value=505):
            resumed.tick()
        self.assertEqual(resumed.d['activeSeconds'], 15)

    def test_explicit_reject_reason_is_saved_for_audit(self):
        engine, rec = self.make_engine(changed=False)
        engine.settle_field_state()
        self.assertEqual(rec['rejectReason'], '状态超时')
        self.assertGreaterEqual(rec['decision']['stateSeconds'], rec['decision']['budgetSeconds'])

    def test_future_state_push_mismatch_cannot_charge_time_to_current_state(self):
        engine, rec = self.make_engine()
        engine.d['field']['transitions'] = {'new': {'key': 'different', 'elapsed': 0}}
        engine.settle_field_state()
        self.assertEqual(rec['stateSeconds'], 0)


class ReadyBatchTests(unittest.TestCase):
    def engine(self, *, target=3, dual=False):
        current = pet((1,), level=20)
        session = core.Session(protocol.ClientConfig(uid=1, username='ready-pet-test'))
        connection = SimpleNamespace(drain_ready_pushes=mock.Mock(return_value=[]),
            request=mock.Mock(), poll_push=mock.Mock(), wait_for_push=mock.Mock())
        session._conn = connection
        ctx = SimpleNamespace(info={'p1': {'level': 164}}, session=session, progress={}, stopped=False)
        d = {'map': 79, 'target': target, 'roles': ['0'], 'harvested': 0,
             'selected': {'0': 'new'}, 'pets': {'new': {'petId': 170111101}}, 'field': {'transitions': {}}}
        engine = Engine(ctx, SimpleNamespace(data=d, save=mock.Mock()))
        engine.live = {'new': copy.deepcopy(current)}
        if dual:
            ctx.info['p2'] = {'level': 164}
            other = copy.deepcopy(current);other['entityId'] = 'second'
            engine.live['second'] = other;d['pets']['second'] = {'petId': 170111101}
            d['roles'].append('1');d['selected']['1'] = 'second'
        return engine, connection

    def ready(self, connection, raw):
        connection.drain_ready_pushes.return_value = [SimpleNamespace(body={'data': raw})]

    def test_ready_target_stops_next_batch_and_updates_pet_cache(self):
        engine, connection = self.engine();raw = pet((3,), level=21)
        self.ready(connection, raw)
        self.assertFalse(engine.before_batch())
        self.assertEqual(engine.live['new']['skill']['passivity'], raw['skill']['passivity'])
        self.assertEqual(engine.ctx.session.pet_entity('new')['level'], 21)
        raw['skill']['passivity']['0'] = 1
        self.assertEqual(engine.live['new']['skill']['passivity']['0'], 3)

    def test_not_ready_continues_without_wait_or_query(self):
        engine, connection = self.engine()
        self.assertTrue(engine.before_batch())
        connection.drain_ready_pushes.assert_called_once_with(('onPetUpgrade',), limit=64)
        connection.poll_push.assert_not_called();connection.wait_for_push.assert_not_called()
        connection.request.assert_not_called()

    def test_full_second_pet_stops_dual_batch(self):
        engine, connection = self.engine(dual=True)
        other = pet((1, 1, 1, 1, 1), level=21);other['entityId'] = 'second'
        self.ready(connection, other)
        self.assertFalse(engine.before_batch())
        self.assertEqual(engine.live['new']['level'], 20)

    def test_rank_three_still_grows_for_rank_four_target(self):
        engine, connection = self.engine(target=4)
        self.ready(connection, pet((3,), level=21))
        self.assertTrue(engine.before_batch())

    def test_ready_role_ceiling_stops_even_if_potential_has_empty_slots(self):
        engine, connection = self.engine()
        self.ready(connection, pet((1,), level=164))
        self.assertFalse(engine.before_batch())
        connection.request.assert_not_called()

    def test_one_dual_pet_at_own_role_ceiling_stops_shared_batch(self):
        engine, connection = self.engine(dual=True)
        engine.ctx.info['p2']['level'] = 40
        raw = pet((1,), level=40);raw['entityId'] = 'second'
        self.ready(connection, raw)
        self.assertFalse(engine.before_batch())
        self.assertEqual(engine.live['new']['level'], 20)

    def test_foreign_wrong_family_and_older_pushes_cannot_stop_current_pet(self):
        for change in ('foreign', 'family', 'older'):
            with self.subTest(change=change):
                engine, connection = self.engine();raw = pet((3,), level=21)
                if change == 'foreign':raw['entityId'] = 'old'
                if change == 'family':raw['id'] = 170511101
                if change == 'older':raw['level'] = 19
                self.ready(connection, raw)
                self.assertTrue(engine.before_batch())
                self.assertEqual(engine.live['new']['level'], 20)

    def test_stopped_or_already_terminal_does_not_read_connection(self):
        for stopped in (False, True):
            engine, connection = self.engine()
            engine.ctx.stopped = stopped
            if not stopped:engine.live['new'] = pet((3,), level=20)
            self.assertFalse(engine.before_batch())
            connection.drain_ready_pushes.assert_not_called()

    def test_read_disconnect_stops_without_writes_or_retries(self):
        engine, connection = self.engine()
        connection.drain_ready_pushes.side_effect = protocol.AllianceError('disconnected')
        with self.assertRaises(protocol.AllianceError):engine.before_batch()
        self.assertEqual(connection.drain_ready_pushes.call_count, 1)
        connection.request.assert_not_called()


class SnapshotIndexTests(unittest.TestCase):
    def engine(self):
        raw = pet((1,), level=1)
        info = {'p1': {'level': 100, 'equipments': {}}, 'bag': {'pet': {'new': raw}}}
        ctx = SimpleNamespace(info=info, progress={}, refresh=mock.Mock(return_value=info),
                              request=mock.Mock(return_value={}), stopped=False, winding=False)
        d = {'map': 79, 'target': 3, 'goal': 2, 'harvested': 0, 'roles': ['0'], 'originalIds': [],
             'pets': {'new': {'petId': 170111101, 'nickname': '', 'phase': 'rejected'}},
             'selected': {}, 'pending': None, 'autoRelease': True, 'cleanupRejected': True}
        return Engine(ctx, SimpleNamespace(data=d, save=mock.Mock(), run_id='a' * 32))

    def test_entity_index_scans_once_per_snapshot(self):
        engine = self.engine()
        with mock.patch.object(cultivation, 'pets_in', wraps=cultivation.pets_in) as scan:
            for _ in range(5):
                self.assertEqual(engine.pet('new')['entityId'], 'new')
            self.assertEqual(scan.call_count, 1)
            engine.invalidate_snapshot()
            engine.pet('new')
            self.assertEqual(scan.call_count, 2)

    def test_replaced_info_invalidates_index_and_missing_pet_stops(self):
        engine = self.engine()
        engine.pet('new')
        engine.ctx.info = {'p1': {'level': 100}, 'bag': {'pet': {}}}
        with self.assertRaisesRegex(RuntimeError, '实体'):
            engine.pet('new')

    def test_refresh_rebuilds_index_even_if_context_reuses_same_mapping(self):
        engine = self.engine()
        engine.pet('new')
        replacement = copy.deepcopy(engine.ctx.info['bag']['pet']['new'])
        replacement['level'] = 20
        engine.ctx.info['bag']['pet']['new'] = replacement
        engine.refresh()
        self.assertEqual(engine.pet('new')['level'], 20)

    def test_write_invalidates_freshness_before_sending_and_keeps_pending(self):
        engine = self.engine()
        engine.refresh()
        def send(*args):
            self.assertIsNone(engine._verified_info)
            self.assertIsNone(engine._pet_info)
            self.assertEqual(engine.d['pending']['kind'], 'equip')
            return {}
        engine.ctx.request.side_effect = send
        engine.op('equip', 'test.route', {})
        self.assertEqual(engine.d['pending']['kind'], 'equip')  # 回执后仍留着，等核验后 commit

    def test_failed_refresh_cannot_leave_a_reusable_snapshot(self):
        engine = self.engine()
        engine.refresh()
        engine.ctx.refresh.side_effect = protocol.AllianceError('disconnected')
        with self.assertRaises(protocol.AllianceError):
            engine.refresh()
        self.assertIsNone(engine._verified_info)

    def test_fresh_discard_skips_only_prequery_and_still_verifies_free(self):
        engine = self.engine()
        engine.refresh()
        engine.ctx.session = SimpleNamespace(release_cultivation_discard=mock.Mock(return_value={}))
        def refreshed():
            engine.ctx.info['bag']['pet'].clear()
            return engine.ctx.info
        engine.ctx.refresh.side_effect = refreshed
        engine.discard('new', 'rejected')
        self.assertEqual(engine.ctx.refresh.call_count, 2)  # initial + mandatory post-free query
        engine.ctx.session.release_cultivation_discard.assert_called_once()
        self.assertEqual(engine.d['pets']['new']['phase'], 'released')
        self.assertEqual(engine.d['refreshReuses'], 1)

    def test_replaced_snapshot_requires_prequery_before_free(self):
        engine = self.engine()
        engine.refresh()
        engine.ctx.info = copy.deepcopy(engine.ctx.info)
        engine.ctx.session = SimpleNamespace(release_cultivation_discard=mock.Mock(return_value={}))
        reads = []
        def refreshed():
            reads.append(True)
            if len(reads) == 2:
                engine.ctx.info['bag']['pet'].clear()
            return engine.ctx.info
        engine.ctx.refresh.side_effect = refreshed
        engine.discard('new', 'rejected')
        self.assertEqual(len(reads), 2)
        self.assertNotIn('refreshReuses', engine.d)


class StableFormationTests(unittest.TestCase):
    def engine(self, *, equal_levels=True, equal_exp=True, verified=True, goal=2):
        a, b = pet((1, 1)), pet((2,))
        a['entityId'], b['entityId'] = 'a', 'b'
        info = {'p1': {'level': 100, 'equipments': {'pet': a}},
                'p2': {'level': 100 if equal_levels else 99, 'equipments': {'pet': b}},
                'bag': {'pet': {'a': a, 'b': b}}}
        d = {'map': 79, 'target': 3, 'goal': goal, 'harvested': 0, 'roles': ['0', '1'], 'originalIds': [],
             'originalFormation': {}, 'pets': {p['entityId']: {'petId': p['id'], 'nickname': '',
                 'phase': 'growing'} for p in (a, b)}}
        engine = Engine(SimpleNamespace(info=info, progress={}), SimpleNamespace(data=d, save=mock.Mock()))
        engine.stats().update(verifiedFields=int(verified), rewardSignature={'0': [100], '1': [100 if equal_exp else 120]})
        engine.takeoff, engine.equip = mock.Mock(), mock.Mock()
        return engine

    def test_equal_verified_slots_keep_survivors_without_four_swap_requests(self):
        engine = self.engine()
        engine.arrange()
        self.assertEqual(engine.d['selected'], {'0': 'a', '1': 'b'})
        engine.takeoff.assert_not_called()
        engine.equip.assert_not_called()

    def test_different_level_or_exp_or_unverified_slots_keep_original_priority_rule(self):
        for options in ({'equal_levels': False}, {'equal_exp': False}, {'verified': False}):
            with self.subTest(options=options):
                engine = self.engine(**options)
                engine.arrange()
                self.assertEqual(engine.d['selected'], {'0': 'b', '1': 'a'})
                self.assertEqual(engine.takeoff.call_count, 2)
                self.assertEqual(engine.equip.call_count, 2)

    def test_last_one_keeps_both_verified_slots_without_swapping(self):
        engine = self.engine(goal=1)
        engine.arrange()
        self.assertEqual(engine.d['selected'], {'0': 'a', '1': 'b'})
        engine.takeoff.assert_not_called()
        engine.equip.assert_not_called()

    def test_duplicate_runtime_entity_stops_before_any_formation_write(self):
        engine = self.engine()
        engine.ctx.info['p2']['equipments']['pet'] = engine.ctx.info['p1']['equipments']['pet']
        with self.assertRaisesRegex(RuntimeError, '重复宠物实体'):
            engine.arrange()
        engine.takeoff.assert_not_called()
        engine.equip.assert_not_called()

    def test_replacing_one_reject_preserves_surviving_pet_slot(self):
        engine = self.engine()
        engine.d['pets']['a']['phase'] = 'rejected'
        new = pet((1,))
        new['entityId'] = 'c'
        engine.ctx.info['bag']['pet']['c'] = new
        engine.d['pets']['c'] = {'petId': new['id'], 'nickname': '', 'phase': 'ready'}
        engine.terminal = mock.Mock()
        engine.arrange()
        self.assertEqual(engine.d['selected'], {'1': 'b', '0': 'c'})
        engine.takeoff.assert_not_called()  # 落选的 a 不先卸，c 直接上阵换下它
        engine.equip.assert_called_once_with(0, 'c', defer=False)
        engine.terminal.assert_called_once_with()  # 换下来的 a 随后按原规则处置


class PetRoomTests(unittest.TestCase):
    def test_occupied_counts_captures_after_snapshot_and_unverified_releases(self):
        engine = Engine.__new__(Engine)
        engine.d = {'pets': {'a': {'phase': 'released'}, 'b': {'phase': 'ready'},
                             'c': {'phase': 'released_unverified'}, 'd': {'phase': 'rejected'}}}
        snapshot = {'old': {'entityId': 'old'}, 'a': {'entityId': 'a'}, 'c': {'entityId': 'c'}, 'd': {'entityId': 'd'}}
        # old、d 在快照里；b 是快照之后抓的；c 放了还没查仍算占着；a 已确认放生
        self.assertEqual(engine._occupied(snapshot), 4)


class WorkflowDecisionTests(unittest.TestCase):
    def setUp(self):
        import json
        from pathlib import Path
        from zmxy.tasks import pet_catch
        from zmxy.tasks.pet_cultivation import decision
        fixture = json.loads((Path(__file__).parent/'tables/gpt宠物决策经验样本.json').read_text('utf8'))['upgradeEXP']
        original = cultivation.data.row
        def row(name, key):
            if name == 'PetEXP' and 1 <= int(key) <= len(fixture):
                return {'upgradeEXP': fixture[int(key)-1]}
            if name == 'PetCatch' and int(key) == 170111101:
                return {'catchUpQualityRate': '0.15;0.33;1'}
            return original(name, key)
        for method in (decision.thresholds, decision.stages, decision.bounds, decision.compile_model):
            method.cache_clear()
            self.addCleanup(method.cache_clear)
        for patcher in (mock.patch.object(cultivation.data, 'row', side_effect=row),
                mock.patch.object(cultivation, 'growth_probability', return_value=.1),
                mock.patch.object(pet_catch, 'target', return_value=('天马', 170111101, (pet_catch.GOURD_ID,)))):
            patcher.start();self.addCleanup(patcher.stop)

    def model(self):
        from zmxy.tasks.pet_cultivation import decision
        return decision.compile_model(3, .1, 164, tuple([10538]*48+[129655]), (.133, .133, .087),
                                      .4781, .3696, .4547, .3510, 1.3413, .15)

    def test_two_attempt_growth_distribution_matches_direct_probability(self):
        from zmxy.tasks.pet_cultivation import decision
        outcomes = dict(decision.attempts((), 4, 3, .1, 2))
        self.assertAlmostEqual(outcomes[()], .9**2)
        self.assertAlmostEqual(outcomes[(1,)], 2*.1*.9)
        self.assertAlmostEqual(outcomes[(2,)], .1**2/18)
        self.assertAlmostEqual(outcomes[(1, 1)], .1**2*17/18)
        self.assertAlmostEqual(sum(outcomes.values()), 1)

    def test_filling_last_slot_prevents_later_attempts_learning(self):
        from zmxy.tasks.pet_cultivation import decision
        outcomes = dict(decision.attempts((2,), 2, 3, .1, 2))
        self.assertAlmostEqual(outcomes[decision.WIN], (1-.9**2)/18)
        self.assertAlmostEqual(outcomes[decision.FULL], (1-.9**2)*17/18)

    def test_experience_requests_remain_independent_inside_pipeline(self):
        from zmxy.tasks.pet_cultivation import decision
        large = decision.stages(0, 164, (10538,), 'D+1', 1)
        small = tuple(cultivation.data.row('PetEXP', i)['upgradeEXP'] for i in range(1, 11))
        divided = decision.stages(0, 164, small, 'D+1', 1)
        self.assertEqual(large[0][0], (11,))
        self.assertEqual(divided[0][0], (20,))

    def test_high_level_weak_state_can_be_rejected_without_waiting_old_age(self):
        from zmxy.tasks.pet_cultivation import decision
        raw = pet((1, 1, 1), level=50);raw['capacity'] = 4
        self.assertEqual(decision.forecast(raw, self.model()), (0., 0.))

    def test_good_current_state_retains_positive_remaining_value(self):
        from zmxy.tasks.pet_cultivation import decision
        raw = pet((2,), level=1);raw['capacity'] = 4
        lower, upper = decision.forecast(raw, self.model())
        self.assertGreater(lower, 0)
        self.assertGreaterEqual(upper, lower)

    def test_only_profitable_three_slot_single_one_can_expand_entry(self):
        from zmxy.tasks.pet_cultivation import decision
        raw = pet((1,), level=1);raw['capacity'] = 3
        self.assertTrue(decision.extra_candidate(raw, self.model()))
        raw['skill']['passivity'] = {'0': 1, '1': 1}
        self.assertFalse(decision.extra_candidate(raw, self.model()))
        raw['skill']['passivity'] = {}
        self.assertFalse(decision.extra_candidate(raw, self.model()))

    def test_same_three_slot_candidate_is_rejected_when_cost_is_high(self):
        from zmxy.tasks.pet_cultivation import decision
        model = dict(self.model());model['fixed'] *= 10
        raw = pet((1,), level=1);raw['capacity'] = 3
        self.assertFalse(decision.extra_candidate(raw, model))

    def calibrated_engine(self):
        engine, rec = MetricsTests().make_engine(ranks=(1,), changed=False)
        engine.ctx.session = SimpleNamespace(background=None, _hooks={})
        engine.d['target'] = 3
        engine.d.update(autoRelease=True, cleanupRejected=True)
        engine.ctx.info['p1']['level'] = 164
        stats = engine.stats()
        stats.update(captures=8, verifiedFields=3, experiencePlan={'0': [10538]*48+[129655]},
            batchTimings={'24': {'count': 4, 'seconds': .532}, '1': {'count': 2, 'seconds': .174}},
            costs={'capture': {'count': 16, 'seconds': 2.}, 'field_step': {'count': 20, 'seconds': 2.},
                   'refresh': {'count': 30, 'seconds': 7.5}, 'free': {'count': 6, 'seconds': .54}})
        engine.d['field']['context'] = stats['key']
        return engine, stats

    def test_remaining_value_can_override_spent_time_for_good_pet(self):
        engine, _ = self.calibrated_engine()
        engine.d['pets']['new']['stateSeconds'] = 10000.
        engine.settle_field_state()
        self.assertEqual(engine.d['pets']['new']['phase'], 'growing')
        self.assertGreater(engine.d['pets']['new']['continuationForecast']['lower'], 0)

    def test_low_remaining_value_stops_before_old_time_budget(self):
        engine, stats = self.calibrated_engine()
        raw = engine.ctx.info['bag']['pet']['new']
        raw.update(level=50, capacity=4, skill={'passivity': {'0': 1, '1': 1, '2': 1}})
        engine.d['field']['before']['new'] = copy.deepcopy(raw)
        engine.d['field']['seconds'] = .01
        engine.d['pets']['new']['stateKey'] = metrics.state_key(raw)
        engine.settle_field_state()
        rec = engine.d['pets']['new']
        self.assertEqual(rec['rejectReason'], '剩余收益不足')
        self.assertLess(rec['decision']['stateSeconds'], rec['decision']['budgetSeconds'])

    def test_engine_requires_verified_full_experience_plan_before_new_entry(self):
        engine, stats = self.calibrated_engine()
        raw = pet((1,), level=1);raw['capacity'] = 3
        self.assertTrue(engine.admissible(raw))
        stats.pop('experiencePlan')
        self.assertFalse(engine.admissible(raw))

    def test_nonrequest_local_cost_is_included_in_capture_and_growth_prices(self):
        engine, stats = self.calibrated_engine()
        stats['seconds'] = sum(r['seconds'] for r in stats['costs'].values())
        operations = sum(r['count'] for k, r in stats['costs'].items() if k != 'refresh')
        stats['activeSeconds'] = stats['seconds']+operations*.02
        model = engine.decision_model(pet((1,), level=1))
        self.assertAlmostEqual(model['overheadPerOperation'], .02)
        self.assertAlmostEqual(model['capture'], .53)
        self.assertAlmostEqual(model['fixed'], .49)
        self.assertEqual(model['version'], 3)

    def test_capture_decision_prices_use_verified_release_query_sharing(self):
        engine, stats = self.calibrated_engine()
        stats.update(freshReleaseCount=16, freshVerificationCount=2)
        model = engine.decision_model(pet((1,), level=1))
        self.assertAlmostEqual(model['reject'], .54/6+.25/8)
        self.assertEqual(model['freshReleaseWindow'], 8)
        model['freshReleaseWindow'] = 1
        updated = engine.decision_model(pet((1,), level=1))
        self.assertEqual(updated['freshReleaseWindow'], 8)

    def test_dual_different_experience_or_levels_does_not_use_equal_sharing_model(self):
        engine, stats = self.calibrated_engine()
        engine.d['roles'] = ['0', '1'];engine.ctx.info['p2'] = {'level': 164}
        stats = engine.stats();stats.update(captures=8, verifiedFields=3,
            experiencePlan={'0': [10538]*48+[129655], '1': [20000]})
        raw = pet((1,), level=1);raw['capacity'] = 3
        self.assertIsNone(engine.decision_model(raw))

    def test_reward_change_clears_decision_and_batch_calibration(self):
        _, stats = self.calibrated_engine()
        stats['decisionModel'] = self.model()
        stats['rewardSignature'] = {'0': [10538]}
        metrics.calibrate(stats, {'0': [20000]})
        self.assertNotIn('experiencePlan', stats)
        self.assertNotIn('decisionModel', stats)
        self.assertNotIn('batchTimings', stats)

    def test_split_dual_receipts_enable_verified_plan_without_dividing_experience(self):
        engine, _ = self.calibrated_engine()
        engine.d['roles'] = ['0', '1']
        engine.ctx.info['p2'] = {'level': 164}
        second = copy.deepcopy(engine.ctx.info['bag']['pet']['new'])
        second['entityId'] = 'second'
        engine.ctx.info['bag']['pet']['second'] = second
        engine.d['pets']['second'] = copy.deepcopy(engine.d['pets']['new'])
        engine.d['selected']['1'] = 'second'
        before = pet((1,), level=1)
        other_before = copy.deepcopy(before);other_before['entityId'] = 'second'
        gain = cultivation.data.row('PetEXP', 1)['upgradeEXP']
        for raw in (engine.ctx.info['bag']['pet']['new'], second):
            raw.update(level=2, petEXP=0)
        engine.d['field'] = {'settled': True, 'seconds': .1, 'pickupCount': 0,
            'availablePetDrops': 2, 'before': {'new': before, 'second': other_before},
            'context': engine.stats()['key']}
        replies = [{'give': [{'subType': 'petEXP', 'entityId': f'index:{slot}', 'count': gain}]}
                   for slot in (0, 1)]
        engine.ctx.session.request_many = mock.Mock(return_value=replies)
        engine.field_batch('scene.copyHandler.syncDropInfo', [{}, {}])
        # 每个请求只给一只，但两只各得完整gain；旧len检查在这里每角色1 != 总请求2。
        self.assertEqual(engine.d['field']['experience'], {'0': gain, '1': gain})
        self.assertEqual(engine.d['field']['experiencePlan'], {'0': [gain, 0], '1': [0, gain]})
        engine.settle_field_state()
        stats = engine.stats()
        self.assertTrue(stats['lastExperience']['verified'])
        self.assertEqual(stats['experiencePlan'], {'0': [gain, 0], '1': [gain, 0]})
        self.assertEqual(stats['experienceBatchSizes'], [2])

    def test_dual_full_plan_keeps_actual_98_request_batches_in_model(self):
        engine, _ = self.calibrated_engine()
        engine.d['roles'] = ['0', '1'];engine.ctx.info['p2'] = {'level': 164}
        left = [x for amount in [10538]*48+[129655] for x in (amount, 0)]
        right = [x for amount in [10538]*48+[129655] for x in (0, amount)]
        full = metrics.complete_experience_plan({'pickupCount': 98, 'availablePetDrops': 98,
            'experiencePlan': {'0': left, '1': right}, 'experienceBatchSizes': [24]*4+[2],
            'experience': {'0': sum(left), '1': sum(right)}}, ['0', '1'])
        self.assertIsNotNone(full)
        stats = engine.stats()
        stats.update(captures=8, verifiedFields=3, experiencePlan=full[0], experienceBatchSizes=full[1],
            batchTimings={'24': {'count': 4, 'seconds': .532}, '2': {'count': 3, 'seconds': .3}},
            costs={'capture': {'count': 16, 'seconds': 2.}, 'field_step': {'count': 20, 'seconds': 2.},
                   'refresh': {'count': 30, 'seconds': 7.5}, 'free': {'count': 6, 'seconds': .54}})
        model = engine.decision_model(pet((1,), level=1))
        self.assertIsNotNone(model)
        self.assertEqual(model['batchSizes'], [24, 24, 24, 24, 2])
        self.assertEqual(len(model['batchTimes']), 5)
        self.assertEqual(sum(model['units']), 48*10538+129655)
        self.assertEqual(stats['decisionStatus'], 'ready')

    def test_complete_plan_rejects_partial_missing_or_mismatched_role_receipts(self):
        full = {'pickupCount': 2, 'availablePetDrops': 2, 'experienceBatchSizes': [2],
                'experiencePlan': {'0': [100, 0], '1': [0, 100]}, 'experience': {'0': 100, '1': 100}}
        for change in ('partial', 'missing', 'wrong_sum', 'bad_size', 'unknown_units'):
            with self.subTest(change=change):
                value = copy.deepcopy(full)
                if change == 'partial':value['availablePetDrops'] = 4
                elif change == 'missing':value['experiencePlan']['1'] = [0, 0]
                elif change == 'wrong_sum':value['experience']['1'] = 200
                elif change == 'bad_size':value['experienceBatchSizes'] = [1]
                else:value['experiencePlan']['0'] = [True, 0]
                self.assertIsNone(metrics.complete_experience_plan(value, ['0', '1']))

    def test_multiroom_short_batch_boundary_is_not_merged_with_next_room(self):
        from zmxy.tasks.pet_cultivation import decision
        units = (cultivation.data.row('PetEXP', 1)['upgradeEXP'],
                 cultivation.data.row('PetEXP', 2)['upgradeEXP'])
        actual = decision.stages(0, 164, units, 'D+1', 1, (1, 1))
        self.assertEqual(actual[0][0], (2, 2))
        self.assertEqual(decision.stages(0, 164, units, 'D+1', 1)[0][0], (4,))

    def test_zero_alignment_never_moves_experience_across_batch_boundaries(self):
        value = {'pickupCount': 4, 'availablePetDrops': 4, 'experienceBatchSizes': [2, 2],
                 'experiencePlan': {'0': [0, 100, 0, 200]}, 'experience': {'0': 300}}
        plan, sizes = metrics.complete_experience_plan(value, ['0'])
        self.assertEqual(plan['0'], [100, 0, 200, 0])
        self.assertEqual(sizes, [2, 2])

    def test_same_verified_full_plan_retains_cached_model(self):
        engine, _ = MetricsTests().make_engine(ranks=(1,), changed=False)
        gain = cultivation.data.row('PetEXP', 29)['upgradeEXP']
        engine.d['field'].update(pickupCount=2, availablePetDrops=2,
            experience={'0': gain}, experiencePlan={'0': [gain, 0]}, experienceBatchSizes=[2])
        stats = engine.stats()
        cached = {'version': 3, 'captureMark': 8}
        stats.update(experiencePlan={'0': [gain, 0]}, experienceBatchSizes=[2], decisionModel=cached)
        engine.settle_field_state()
        self.assertTrue(stats['lastExperience']['verified'])
        self.assertIs(stats['decisionModel'], cached)

    def test_full_release_cost_model_is_not_used_when_disposal_switch_is_off(self):
        engine, _ = self.calibrated_engine()
        engine.d['autoRelease'] = False
        self.assertIsNone(engine.decision_model(pet((1,), level=1)))
        engine.d.update(autoRelease=True, cleanupRejected=False)
        self.assertIsNone(engine.decision_model(pet((1,), level=1)))


if __name__ == '__main__':
    unittest.main()
