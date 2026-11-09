"""培养断点账本：权威状态、写请求的 pending 记录和逐项核验。

每个写请求都走 ``Ledger.op``：先把 pending 存盘，再发请求，刷新后由 ``resolve_pending`` 核验。
结果未知时断点停在 pending，续跑只核验、绝不重发。``d`` 里的字段是磁盘格式，暂停中的轮次要能接着跑，勿改名。
"""
from __future__ import annotations

import copy
import time
from collections.abc import Mapping

from ... import api, protocol as p, state
from .. import pet_catch
from . import metrics, policy, task as rules

R_FROM = api.R_WAREHOUSE_TO_BAG
R_TO = api.R_BAG_TO_WAREHOUSE
GOURD_PACK = 9999
TRAINING = ('ready', 'growing')


class Halt(RuntimeError):
    """停在断点：不再发写请求，继续时从这里接。"""


def same_identity(pet, rec):
    return state.num(pet.get('id')) == rec['petId'] and pet.get('nickName') == rec['nickname']


def warehouse_count(info, item):
    rows = info.get('bag', {}).get('warehouseItem')
    if isinstance(rows, Mapping):
        rows = list(rows.values())
    if not isinstance(rows, list):
        raise Halt('读不到仓库物品，未取用去灵花')
    total = 0
    for row in rows:
        if isinstance(row, Mapping) and state.num(row.get('id')) == item:
            n = state.num(row.get('count'))
            if n is None or n < 0:
                raise Halt('仓库去灵花数量不明确')
            total += n
    return total


class Ledger:
    def __init__(self, ctx, journal):
        self.ctx, self.j, self.d = ctx, journal, journal.data
        self.live = {}
        self._verified_info = self._pet_info = self._capture_remaining = None
        self._admission_note = self.field_started = None
        self._predicted = False  # 快照里的培养宠状态是刷图后推算的，还没查询核对
        self._pet_index = {}
        self.clock_mark = time.monotonic()
        if 'activeSeconds' not in self.d:
            self.d.update(activeSeconds=0.0, timingStartHarvest=self.d['harvested'], timingStartPets=len(self.d['pets']))
        for stats in self.d.get('performance', {}).get('contexts', {}).values():
            stats.setdefault('activeSeconds', stats['seconds'])
            stats.setdefault('equipVerificationCount', stats.get('costs', {}).get('equip', {}).get('count', 0))

    @property
    def halted(self):
        return self.ctx.stopped or self.ctx.winding

    @property
    def capture_mid(self):
        return self.d.get('captureMap', rules.CAPTURE_PET)

    @property
    def goal_reached(self):
        return self.d['harvested'] >= self.d['goal']

    def tick(self):
        now = time.monotonic()
        elapsed = max(0.0, now-self.clock_mark)
        self.clock_mark = now
        self.d['activeSeconds'] += elapsed
        self.stats()['activeSeconds'] += elapsed

    def stats(self):
        stats = metrics.bucket(self.d, self.ctx.info, self.active_roles())
        stats.setdefault('activeSeconds', 0.0)
        return stats

    def bump(self, key):
        stats = self.stats()
        stats[key] = stats.get(key, 0) + 1

    def active_roles(self):
        return [] if self.goal_reached else self.d['roles']

    def growth_key(self, pet):
        return repr((pet['capacity'], sorted(policy.perks(pet).values())))

    def finished(self, pet):
        """达标或满槽：这只不会再有新进展。"""
        levels = policy.perks(pet)
        return max(levels.values(), default=0) >= self.d['target'] or len(levels) >= pet['capacity']

    def can_release_fresh(self):
        """没有穿插任务时才能跳过放生前刷新：穿插可能动背包，本地快照就不算数了。"""
        return self.ctx.session.background is None

    def training(self):
        return [e for e, r in self.d['pets'].items() if r['phase'] in TRAINING]

    def held(self):
        return sum(bool(r.get('captureUnverified')) for r in self.d['pets'].values())

    def debts(self):
        return sum(r['phase'] == 'released_unverified' for r in self.d['pets'].values())

    def bag(self, item):
        if item == pet_catch.GOURD_ID and self._capture_remaining is not None:
            return self._capture_remaining
        value = state.bag_count(self.ctx.info, item)
        if value is None:
            raise Halt('背包道具数量未知，停止')
        return value

    def formation(self, why):
        slots = rules.equipped_pets(self.ctx.info)
        if slots is None:
            raise Halt(why)
        return slots

    def formation_ids(self, why):
        return {str(i): pet['entityId'] for i, pet in self.formation(why)}

    def slot_pet(self, index):
        return dict(self.formation('阵容状态未知，未继续')).get(index)

    def save(self):
        self.ctx.progress['harvested'] = self.d['harvested']
        self.j.save()

    # ---- 权威状态 ----

    def refresh(self):
        self.invalidate_snapshot()
        started = time.monotonic()
        result = self.ctx.refresh()
        self._capture_remaining = None
        self._predicted = False
        self.verify_fresh_releases()
        self.verify_formation_checks()
        self._verified_info = self.ctx.info
        metrics.cost(self.stats(), 'refresh', time.monotonic() - started)
        return result

    def confirm(self):
        """写请求之后：刷新并核验 pending。"""
        self.refresh()
        self.resolve_pending()

    def owed(self):
        return bool(self.debts() or self.held())

    def refresh_if_owed(self):
        """还有短批欠账（放了没查、抓了没查）时先刷新结清。"""
        if self.owed():
            self.refresh()

    def flush_formation_checks(self):
        if self.d.get('formationChecks'):
            self.refresh()

    def invalidate_snapshot(self):
        self._verified_info = None
        self._pet_info = None
        self._pet_index = {}

    def pet(self, entity):
        """本轮宠物的最新实体；对不上就停，宁可保留也不处置错。"""
        if self._pet_info is not self.ctx.info:
            self._pet_index = {pet['entityId']: pet for pet in rules.pets_in(self.ctx.info)}
            self._pet_info = self.ctx.info
        pet = self._pet_index.get(entity)
        if pet is None or not same_identity(pet, self.d['pets'][entity]) or entity in self.d['originalIds']:
            raise Halt('本轮宠物实体、编号或昵称不符，保留并停止')
        policy.perks(pet)
        return pet

    def verify_fresh_releases(self):
        """结清短批欠账：放过的确实没了，先留着没查的候选确实没变。"""
        for entity, rec in self.d['pets'].items():
            if rec.get('captureUnverified'):
                self._verify_held_candidate(entity, rec)
            if rec['phase'] != 'released_unverified':
                continue
            if (rec.get('freshReleaseAck') is not True or entity in self.d['originalIds']
                    or rules.find_entity(self.ctx.info, entity) is not None):
                raise Halt('初筛放生短批结果未核验，保留断点且不重发')
            rec['phase'] = 'released'

    def _verify_held_candidate(self, entity, rec):
        pet = rules.find_entity(self.ctx.info, entity)
        initial = rec['initial']
        if (pet is None or entity in self.d['originalIds'] or not same_identity(pet, rec)
                or pet.get('level') != initial['level'] or pet.get('capacity') != initial['capacity']
                or pet.get('petEXP') != initial.get('petEXP')
                or policy.perks(pet) != initial['passivity']):
            raise Halt('捕捉短批候选实体未核验，保留并停止')
        rec['latest'] = copy.deepcopy(pet)
        rec.pop('captureUnverified')

    def verify_formation_checks(self):
        checks = self.d.get('formationChecks', [])
        if not checks:
            return
        slots = rules.equipped_pets(self.ctx.info)
        if slots is None or any(dict(slots).get(c['index'], {}).get('entityId') != c['entityId'] for c in checks):
            raise Halt('合并上阵结果未核验，保留断点且不重发')
        self.d['formationChecks'] = []
        self.bump('equipVerificationCount')
        self.save()

    # ---- 写请求：先存 pending，再发；回执之后的那次存盘（commit）才清掉 pending ----

    def op(self, kind, route, body, meta=None, action=None):
        if self.d.get('pending'):
            raise Halt('仍有待核验操作，不发送下一次写请求')
        self.invalidate_snapshot()
        self.d['pending'] = {'kind': kind, 'route': route, 'body': dict(body), 'meta': meta or {}}
        self.save()
        started = time.monotonic()
        try:
            response = action() if action else self.ctx.request(route, body)
        except p.AllianceError:
            # 明确拒绝或发出前被拦，才能清除待确认；结果未知是 UncertainOutcome，不走这里。
            self.commit()
            raise
        # 收到回执不单独存盘：续跑一律按最新状态核验 pending，用不到“收到过回执”这一笔。
        metrics.cost(self.stats(), kind, time.monotonic() - started)
        return response

    def commit(self):
        self.tick()
        self.d['pending'] = None
        self.save()

    def resolve_pending(self):
        """按最新状态核验上一个写请求，一律不重发。"""
        op = self.d.get('pending')
        if not op:
            return
        settle = getattr(self, f"_settle_{op['kind']}", None)
        if settle is None:
            raise Halt('断点操作类型无法核验，未执行')
        settle(op)
        self.commit()

    def _settle_transfer(self, op):
        meta = op['meta']
        moved = meta['count'] if op['route'] == R_FROM else -meta['count']
        if (self.bag(rules.FORGET_FLOWER) != meta['bag'] + moved
                or warehouse_count(self.ctx.info, rules.FORGET_FLOWER) != meta['warehouse'] - moved):
            raise Halt('仓库借还未核验成功，停止且不重发')
        self.d['loan'] += moved

    def _settle_buy(self, op):
        meta = op['meta']
        if (self.bag(pet_catch.GOURD_ID) != meta['gourds'] + GOURD_PACK
                or state.spirit(self.ctx.info) != meta['spirit'] - meta['price']*GOURD_PACK):
            raise Halt('葫芦补购结果未核验，停止且不重复购买')
        self.d['bought'] += GOURD_PACK

    def _settle_remove(self, op):
        meta, entity, sid = op['meta'], op['body']['entityId'], str(op['body']['skillId'])
        pet = self.pet(entity)
        levels, old = policy.perks(pet), meta['levels']
        delta = self.bag(rules.FLOWER) - meta['flowers']
        used = meta['forget'] - self.bag(rules.FORGET_FLOWER)
        if (levels.get(sid, 0) >= old[sid] or any(levels.get(k) != v for k, v in old.items() if k != sid)
                or any(k not in old for k in levels) or delta != meta['expected'] or used != 1):
            raise Halt('潜能、其他潜能、去灵花消耗或益灵花到账未全部核验，停止且不重发')
        own_used = min(self.d['ownFlowers'], used)
        self.d['ownFlowers'] -= own_used
        self.d['loan'] -= used - own_used
        if self.d['loan'] < 0:
            raise Halt('仓库借用材料账目异常')
        rec = self.d['pets'][entity]
        rec['receipt'] = {'skill': sid, 'level': old[sid], 'count': delta}
        rec['phase'] = 'recovered'
        rec['latest'] = copy.deepcopy(pet)
        current = self.stats()
        stats = self.d['performance']['contexts'].get(rec.get('measurementContext', ''), current)
        if entity not in stats['targets']:
            stats['targets'].append(entity)
        self.d['harvested'] += delta

    def _settle_free(self, op):
        entity = op['body']['entityId']
        if rules.find_entity(self.ctx.info, entity) is not None:
            raise Halt('放生结果未核验，停止且不重发')
        self.d['pets'][entity]['phase'] = 'released'

    def _settle_equip(self, op):
        current = self.slot_pet(op['body']['index'])
        if current is None or current.get('entityId') != op['body']['entityId']:
            raise Halt('上阵结果未核验，停止且不重发')

    def _settle_takeoff(self, op):
        if self.slot_pet(op['body']['index']) is not None:
            raise Halt('卸下结果未核验，停止且不重发')

    def _settle_capture(self, op):
        # 没收到成功实体的捕捉不能进放生名单：新增的来源不明实体当旧宠保留，另开新场。
        self.d['originalIds'] = sorted(set(self.d['originalIds']) | {
            pet['entityId'] for pet in rules.pets_in(self.ctx.info) if pet['entityId'] not in self.d['pets']})
        self.ctx.log('上次捕捉响应未确认：保留来源不明实体，不重发旧捕捉，按最新葫芦继续')

    def _settle_field_step(self, op):
        # 主线无入场消耗；按最新宠物状态接续，绝不复用旧 rnd 或重发旧拾取/结算。
        if self.d.get('field'):
            self.d['field']['interrupted'] = True
