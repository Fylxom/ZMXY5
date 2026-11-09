"""刷图：领宠物经验、一场结束后计时、判去留、校准收益模型。"""
from __future__ import annotations

import copy
import time
from dataclasses import replace

from ... import data, dungeon, pickup, state
from . import decision, metrics, policy, task as rules
from .admission import Admission
from .ledger import Halt

PUSH_WAIT = 0.15  # 刷完一场等迟到升级推送的上限（秒）；一次查询约 0.26 秒


def _grow(before, gained, ceiling):
    """按经验表推算刷完一场后的 (等级, 当前经验)。要升到角色等级上限附近、表里查不到就返回 None。"""
    level, exp = before.get('level'), before.get('petEXP')
    if type(level) is not int or type(exp) is not int or type(gained) is not int or gained < 0 or ceiling is None:
        return None
    exp += gained
    while True:
        try:
            need = (data.row('PetEXP', level) or {}).get('upgradeEXP')
        except data.DataError:  # 读不到经验表：不推算
            return None
        if type(need) is not int or need <= 0:
            return None
        if exp < need:
            break
        exp -= need
        level += 1
        if level >= ceiling:  # 到上限时多出来的经验怎么算没核实过：交给查询
            return None
    return level, exp


def _overwrite(info, entity, pet):
    """把推算出的等级、经验、潜能写进快照里这只宠物的每一处副本。返回改了几处。"""
    count = 0
    stack = [info]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get('entityId') == entity and 'skill' in value and 'level' in value:
                value.update(level=pet['level'], petEXP=pet['petEXP'], skill=copy.deepcopy(pet['skill']))
                count += 1
                continue
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return count


class FieldRun(Admission):
    def field_request(self, route, body=None):
        reply = self.op('field_step', route, body or {})
        field = self.d['field']
        if route == dungeon.R_CREATE:
            field['availablePetDrops'] = field.get('availablePetDrops', 0) + sum(
                pickup.kind(drop) == 'pet_exp' for _, drop in dungeon._ground_drops(reply))
        if route == dungeon.R_FINISH:
            field['settled'] = True
        self.commit()
        return reply

    def field_batch(self, route, bodies):
        self.invalidate_snapshot()
        self.d['pending'] = {'kind': 'field_step', 'route': route, 'body': {}, 'meta': {}}
        self.save()
        started = time.monotonic()
        replies = self.ctx.session.request_many(route, bodies)
        if len(replies) != len(bodies) or any(isinstance(r, Exception) for r in replies):
            raise Halt('经验批次未全部确认，停止且不重发')
        elapsed = time.monotonic() - started
        stats = self.stats()
        metrics.cost(stats, 'pickup_batch', elapsed)
        timing = stats.setdefault('batchTimings', {}).setdefault(str(len(bodies)), {'count': 0, 'seconds': 0.0})
        timing['count'] += 1
        timing['seconds'] += elapsed
        self._tally_experience(self.d['field'], replies)
        self.commit()
        return replies

    def _tally_experience(self, field, replies):
        totals = field.setdefault('experience', {})
        for slot, count in metrics.response_exp(replies).items():
            totals[slot] = totals.get(slot, 0) + count
        for reply in replies:
            received = metrics.response_exp(reply)
            for slot in self.d['selected']:
                count = received.get(slot, 0)
                field.setdefault('experiencePlan', {}).setdefault(slot, []).append(count)
                units = field.setdefault('rewardUnits', {}).setdefault(slot, [])
                if count > 0 and count not in units:
                    units.append(count)
                    units.sort()
        field.setdefault('experienceBatchSizes', []).append(len(replies))
        field['pickupCount'] += len(replies)

    def before_batch(self):
        """dungeon 每批拾取前问一次：先收已到的升级推送，达标/满槽/封顶就不再领经验。"""
        if self.ctx.stopped or not self.needs_experience():
            return False
        self.after_batch((), self.ctx.session.drain_ready_pushes(('onPetUpgrade',)))
        return self.needs_experience()

    before_room = before_batch

    def needs_experience(self):
        for slot, entity in self.d['selected'].items():
            pet = self.live[entity]
            finished = self.finished(pet)
            ceiling = rules.player_level(self.ctx.info, int(slot))
            if ceiling is None:
                raise Halt('培养角色等级未知，停止领取经验')
            if finished or pet['level'] >= ceiling:
                return False
        return True

    def after_batch(self, replies, pushes):
        for pet in rules.pets_in(pushes):
            entity = pet.get('entityId')
            if entity not in self.live or state.num(pet.get('id')) != self.d['pets'][entity]['petId']:
                continue
            try:
                policy.perks(pet)
            except ValueError:
                continue
            if pet['level'] < self.live[entity]['level']:
                continue
            if metrics.state_key(pet) != metrics.state_key(self.live[entity]) and self.field_started is not None:
                self.d['field'].setdefault('transitions', {})[entity] = {
                    'key': metrics.state_key(pet), 'elapsed': max(0, time.monotonic()-self.field_started)}
            self.live[entity] = copy.deepcopy(pet)

    def _charge_time(self, rec, pet, field, before):
        """把本场耗时记到宠物当前的潜能状态上；``before`` 为 None 表示本场耗时归属不可靠。"""
        key = metrics.state_key(pet)
        if before is None:
            rec['stateSeconds'] = 0.0  # 不可靠的耗时不能触发经济性落选
            rec['growthIncomplete'] = True
        else:
            if metrics.state_key(before) == key:
                rec['stateSeconds'] = (rec.get('stateSeconds', 0) if rec.get('stateKey') == key else 0) + field['seconds']
            else:
                observed = field.get('transitions', {}).get(pet['entityId'], {})
                rec['stateSeconds'] = max(0, field['seconds']-observed['elapsed']) if observed.get('key') == key else 0.0
            rec['growthSeconds'] = rec.get('growthSeconds', 0) + field['seconds']
        rec['stateKey'] = key

    def _reject_reason(self, pet, rec, role, reliable, budget):
        levels = policy.perks(pet)
        if max(levels.values(), default=0) >= self.d['target']:
            return ''
        if len(levels) >= pet['capacity']:
            return '潜能满槽'
        if pet['level'] >= role['level']:
            return '角色等级上限'
        if not reliable:
            return ''
        verdict = None
        model = self.decision_model(pet) if len(self.active_roles()) == 1 else None
        if model is not None and type(pet.get('petEXP')) is int:
            lower, upper = decision.forecast(pet, model)
            rec['continuationForecast'] = {'lower': lower, 'upper': upper, 'rate': model['rate']}
            verdict = True if lower > 1e-9 else False if upper <= 1e-9 else None
        if verdict is False:
            return '剩余收益不足'
        return '状态超时' if verdict is None and rec['stateSeconds'] >= budget else ''

    def _record_growth(self, stats, pet, rec):
        """一只宠物培养到头时记一条完整耗时样本，每只只记一次。"""
        finished = self.finished(pet)
        if rec.get('growthRecorded') or not (finished or rec['phase'] == 'rejected'):
            return
        if (rec.get('growthKey') and rec.get('growthContext') == stats['key']
                and rec.get('growthEpoch') == stats.get('rewardEpoch', 0) and 'growthSeconds' in rec):
            metrics.growth(stats, rec['growthKey'], rec['growthSeconds'], finished and not rec.get('growthIncomplete'))
        rec['growthRecorded'] = True

    def _adopt_experience_plan(self, stats, field):
        metrics.calibrate(stats, field.get('rewardUnits', {}))
        complete = metrics.complete_experience_plan(field, self.d['selected'])
        if complete is not None:
            if stats.get('experiencePlan') != complete[0] or stats.get('experienceBatchSizes') != complete[1]:
                stats.pop('decisionModel', None)
            stats['experiencePlan'], stats['experienceBatchSizes'] = complete

    def settle_field_state(self):
        """一场结束（或断点里留着没结的一场）：计时、判去留、校准收益模型。"""
        field = self.d.get('field')
        if not field:
            return
        stats = self.stats()
        reliable = (field.get('settled') and not field.get('interrupted') and 'before' in field
                    and field.get('context') == stats['key'])
        predicted = bool(field.get('predicted'))
        rewards_changed = metrics.reward_changed(stats, field.get('rewardUnits', {}))
        opportunity = metrics.BASE_RATE[self.d['target']] if rewards_changed else metrics.rate(stats, self.d['target'])
        verified = bool(reliable)
        for slot, entity in self.d.get('selected', {}).items():
            pet, rec = self.pet(entity), self.d['pets'][entity]
            before = field.get('before', {}).get(entity)
            self._charge_time(rec, pet, field, before if reliable else None)
            rec['latest'] = copy.deepcopy(pet)
            rec['measurementContext'] = stats['key']
            role = self.ctx.info['p1' if slot == '0' else 'p2']
            delta = metrics.experience_delta(before or {}, pet,
                                             lambda level: (data.row('PetEXP', level) or {}).get('upgradeEXP'))
            verified = (verified and pet['level'] < role['level'] and delta is not None and delta > 0
                        and delta == field.get('experience', {}).get(slot))
            budget = policy.state_budget(pet, self.d['target'], opportunity)
            reason = self._reject_reason(pet, rec, role, reliable, budget)
            if reason:
                rec['phase'] = 'rejected'
                rec['rejectReason'] = reason
                rec['decision'] = {'stateSeconds': rec['stateSeconds'], 'budgetSeconds': budget}
            else:
                rec['phase'] = 'growing'
            if reliable:
                self._record_growth(stats, pet, rec)
        if field.get('predictedBefore') and not predicted and reliable and not verified:
            self.bump('predictionMismatches')  # 推算接着的这一场查出来对不上：推算有误（或经验核对本就不过）
            self.ctx.log('刷图后推算的宠物状态与查询不一致，已按查询结果继续')
        verified = verified and not predicted  # 推算出的状态不拿来校准收益模型
        if verified:
            self._adopt_experience_plan(stats, field)
        if field.get('settled'):
            self.d['fields'] += 1
            stats['fields'] += 1
        if verified:
            stats['verifiedFields'] += 1
        stats['lastExperience'] = {'replies': field.get('experience', {}), 'verified': verified}
        self.d['field'] = None
        self.save()

    def field(self):
        self.flush_formation_checks()
        lv = rules.latest_level(self.ctx.info)
        if self.d['map'] != lv.mid:
            self.ctx.log(f'自动切换培养主线：{lv.name}（{lv.mid}）')
        self.d['map'] = lv.mid
        self.live = {e: copy.deepcopy(self.pet(e)) for e in self.d['selected'].values()}
        self.d['field'] = {'settled': False, 'seconds': 0, 'pickupCount': 0,
                           'before': copy.deepcopy(self.live), 'experience': {}, 'transitions': {},
                           'context': self.stats()['key'], 'predictedBefore': self._predicted}
        self.save()
        started = self.field_started = time.monotonic()
        self.ctx.engage(True)
        try:
            dungeon._attempt(replace(self.ctx, session=_FieldSession(self)), lv, dungeon.Gains(), win=False,
                             keep_drop=lambda drop: pickup.kind(drop) == 'pet_exp', pickup_control=self)
        finally:
            self.d['field']['seconds'] = time.monotonic() - started
            self.save()
            self.ctx.engage(False)
        if not self._predict_after_field():
            self.refresh()
        if self.d['field']['pickupCount'] == 0:
            raise Halt('本场没有可确认的宠物经验拾取，停止以免空刷')
        self.settle_field_state()
        self.ctx.log(f"益灵花{self.d['harvested']}/{self.d['goal']}；已结算{self.d['fields']}场；" + '；'.join(
            self._slot_note(slot, entity) for slot, entity in self.d['selected'].items()))

    def _predict_after_field(self):
        """刷完一场先不查询：本场经验回执 + 升级推送推算出每只宠物的新状态。
        等级对得上最后一条升级推送、潜能只在升级时变、宠物明显还要接着练，才用推算结果；
        否则照常查询（没用推算的原因记在 stats['predictMisses']）。推算出的快照不算“刚查过”，放生、回收前一律重新查询。"""
        reason = self._predict_miss()
        if reason:
            misses = self.stats().setdefault('predictMisses', {})
            misses[reason] = misses.get(reason, 0) + 1
            return False
        self.bump('fieldQueriesSkipped')
        return True

    def _predict_miss(self):
        """能用推算就写进快照并返回空串，否则返回不用的原因。"""
        field = self.d['field']
        if (not field.get('settled') or field.get('interrupted') or self.halted or not self.can_release_fresh()
                or not field.get('pickupCount')):
            return '本场未正常结算'
        self.after_batch((), self.ctx.session.drain_ready_pushes(('onPetUpgrade',)))
        grown = {}
        for slot, entity in self.d['selected'].items():
            before = field['before'].get(entity)
            if before is None or self.live.get(entity) is None:
                return '缺少场前状态'
            grown[entity] = _grow(before, field.get('experience', {}).get(slot), rules.player_level(self.ctx.info, int(slot)))
            if grown[entity] is None:
                return '经验表或等级上限'
        self._await_upgrades(grown)
        predicted = {}
        for slot, entity in self.d['selected'].items():
            before, live = field['before'][entity], self.live[entity]
            if live.get('level') != grown[entity][0]:
                return '升级推送未到齐'
            level, exp = grown[entity]
            source = live if level > before['level'] else before
            if level == before['level'] and policy.perks(live) != policy.perks(before):
                return '未升级却变了潜能'
            pet = {**copy.deepcopy(before), 'level': level, 'petEXP': exp, 'skill': copy.deepcopy(source['skill'])}
            if self.finished(pet):
                return '达标或满槽'
            predicted[entity] = pet
        if any(_overwrite(self.ctx.info, entity, pet) == 0 for entity, pet in predicted.items()):
            raise Halt('推算状态写不进快照，停止')  # 上面刚从快照里读到过它，不会发生
        self.invalidate_snapshot()
        self._predicted = True
        field['predicted'] = True
        return ''

    def _await_upgrades(self, grown):
        """算出该升级、推送却还没到的，短暂等一下（比查询一次还短）；等不到就交给查询。"""
        deadline = time.monotonic() + PUSH_WAIT
        started = time.monotonic()
        while any(self.live[e].get('level', 0) < g[0] for e, g in grown.items()):
            remaining = deadline - time.monotonic()
            got = self.ctx.session.poll_push(('onPetUpgrade',), remaining) if remaining > 0 else None
            if got is None:
                break
            self.after_batch((), [got[1]])
        waited = time.monotonic() - started
        if waited > 0:
            stats = self.stats()
            stats['predictPushWait'] = stats.get('predictPushWait', 0.0) + waited

    def _slot_note(self, slot, entity):
        pet, rec = self.pet(entity), self.d['pets'][entity]
        levels = policy.perks(pet)
        note = f"{int(slot)+1}P {pet['level']}级，潜能{list(levels.values())}，槽位{len(levels)}/{pet['capacity']}"
        return note + (f"，落选：{rec['rejectReason']}" if rec['phase'] == 'rejected' else '')


class _FieldSession:
    """刷图时替换 ctx.session：dungeon 发的每个写请求都要过 Engine 的断点记录。"""

    def __init__(self, engine):
        self._engine = engine

    def __getattr__(self, key):
        return getattr(self._engine.ctx.session, key)

    def request(self, route, body=None):
        return self._engine.field_request(route, body)

    def request_many(self, route, bodies):
        return self._engine.field_batch(route, bodies)
