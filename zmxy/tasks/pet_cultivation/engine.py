"""流程：材料、阵容、捕捉、回收、放生、收尾。放生只走 ``release``，核验顺序不要动。"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from functools import partial

from ... import api, dungeon, release_guard, state
from .. import pet_catch
from . import decision, metrics, policy, task as rules
from .field import FieldRun
from .ledger import GOURD_PACK, R_FROM, R_TO, TRAINING, Halt, same_identity, warehouse_count


class Engine(FieldRun):
    def material(self):
        if self.bag(rules.FORGET_FLOWER) > 0:
            return
        count = warehouse_count(self.ctx.info, rules.FORGET_FLOWER)
        if count <= 0:
            raise Halt('背包和仓库都没有去灵花，已停止刷取')
        self.transfer(R_FROM, min(count, max(1, self.d['goal'] - self.d['harvested'])))

    def transfer(self, route, count):
        meta = {'bag': self.bag(rules.FORGET_FLOWER),
                'warehouse': warehouse_count(self.ctx.info, rules.FORGET_FLOWER), 'count': count}
        api.move_warehouse(partial(self.op, 'transfer', meta=meta), rules.FORGET_FLOWER, count,
                           to_bag=route == R_FROM)
        self.confirm()

    def ensure_gourds(self):
        if self.bag(pet_catch.GOURD_ID) > 0:
            return
        price, balance = pet_catch.gourd_price(), state.spirit(self.ctx.info)
        if price is None or balance is None:
            raise Halt('木灵葫芦灵魂价格或余额未知，未购买')
        if balance < price * GOURD_PACK:
            raise Halt('灵魂不足以补购9999个木灵葫芦，已停止')
        meta = {'gourds': 0, 'spirit': balance, 'price': price}
        api.buy_shop(partial(self.op, 'buy', meta=meta), pet_catch.SHOP_ID, GOURD_PACK)
        self.confirm()

    # ---- 阵容 ----

    def takeoff(self, slot):
        self.op('takeoff', rules.R_TAKEOFF, {'part': 'pet', 'type': 'pet', 'index': slot})
        self.confirm()

    def equip(self, slot, entity, *, defer=False):
        reply = self.op('equip', rules.R_EQUIP, {'entityId': entity, 'type': 'pet', 'index': slot})
        if (defer and self.can_release_fresh() and isinstance(reply, Mapping)
                and reply.get('code') == 200 and type(reply.get('index')) is int
                and reply['index'] == slot and reply.get('equipEntityId') == entity and reply.get('type') == 'pet'):
            # 回执可信时两次上阵共用一次查询，下次刷新统一核验。
            self.d.setdefault('formationChecks', []).append({'index': slot, 'entityId': entity})
            self.commit()
            return
        self.confirm()
        self.bump('equipVerificationCount')

    def _rank(self, entity):
        rec = self.d['pets'][entity]
        pet = rec['latest'] if rec.get('captureUnverified') else self.pet(entity)
        return policy.score(pet, self.d['target'], metrics.estimate(self.stats(), self.growth_key(pet)))

    def _same_reward_both_sides(self, roles):
        stats = self.stats()
        units = stats.get('rewardSignature', {})
        return (len(roles) == 2 and self.ctx.info['p1']['level'] == self.ctx.info['p2']['level']
                and stats.get('verifiedFields', 0) > 0 and units.get('0') and units.get('0') == units.get('1'))

    def _keep_in_place(self, roles, candidates, selected, current):
        """两边收益相同，留在场上的不换位，省下四次装卸。"""
        chosen = set(selected.values())
        kept = {slot: current[slot] for slot in roles if current.get(slot) in chosen}
        remaining = iter(e for e in candidates[:len(roles)] if e not in kept.values())
        for slot in roles:
            if slot not in kept:
                kept[slot] = next(remaining)
        return kept

    def arrange(self):
        """排阵容。短批欠账（放了没查、抓了没查）不先查，留给上阵后那次查询一起核验；
        核验不过就停在刷图之前。有穿插任务时本地快照不算数，先查。"""
        if self.owed() and not self.can_release_fresh():
            self.refresh()
        roles = self.active_roles()
        candidates = sorted(self.training(), key=self._rank, reverse=True)
        selected = dict(zip(roles, candidates))
        current = self.formation_ids('上阵前无法确认原阵容')
        if len(set(current.values())) != len(current):
            raise Halt('当前阵容存在重复宠物实体，未装卸或继续培养')
        if self._same_reward_both_sides(roles):
            selected = self._keep_in_place(roles, candidates, selected, current)
        swapped = False
        for slot, entity in list(current.items()):
            if entity not in self.d['pets'] and entity not in self.d['originalFormation'].values():
                raise Halt('当前阵容出现断点以外的宠物，停止以免覆盖其他宠物')
            if selected.get(slot) != entity:
                if slot in selected and self._finished_on_field(entity):
                    swapped = True  # 培养完的宠物不先卸：新宠直接上阵把它换下来
                else:
                    self.takeoff(int(slot))
        changes = [(slot, entity) for slot, entity in selected.items() if current.get(slot) != entity]
        defer = len(changes) > 1 or self.owed()
        for slot, entity in changes:
            self.equip(int(slot), entity, defer=defer)
        self.flush_formation_checks()
        self.refresh_if_owed()  # 没上阵也要在刷图前结清
        self.d['selected'] = selected
        self.save()
        if swapped:
            self.terminal()  # 被换下来的宠物：按原规则核验后处置

    # ---- 捕捉 ----

    def add_captured(self, raw):
        entity, pet_id = raw.get('entityId'), state.num(raw.get('id'))
        if (not isinstance(entity, str) or entity in self.d['originalIds'] or entity in self.d['pets']
                or not self.ctx.session.cultivation_captured(entity)
                or pet_id not in release_guard.CULTIVATION_FAMILIES[self.capture_mid]
                or not isinstance(raw.get('nickName'), str)):
            raise Halt('捕捉未能核对本轮唯一新实体，保留并停止')
        levels = policy.perks(raw)
        accepted = self.admissible(raw)
        stats = self.stats()
        rec = {'entityId': entity, 'petId': pet_id, 'nickname': raw['nickName'],
               'phase': 'ready' if accepted else 'rejected',
               'entryDecision': copy.deepcopy(self._admission_note),
               # 与 latest 分开：后续升级/回收不得覆盖捕获时的证据。
               'initial': {'entityId': entity, 'petId': pet_id, 'level': raw['level'], 'petEXP': raw.get('petEXP'),
                           'capacity': raw['capacity'], 'sex': raw.get('sex'), 'passivity': levels,
                           'captureAttempt': self.d['captureAttempts'],
                           'source': 'confirmed_capture_response_or_push'},
               'latest': copy.deepcopy(raw), 'stateKey': metrics.state_key(raw), 'stateSeconds': 0.0,
               'growthKey': self.growth_key(raw), 'growthSeconds': 0.0,
               'growthContext': stats['key'], 'growthEpoch': stats.get('rewardEpoch', 0)}
        if not accepted:
            rec['rejectReason'] = '初筛不合格'
        self.d['pets'][entity] = rec
        stats['captures'] += 1
        return entity

    def _pet_room(self):
        """开捕前的宠物仓位；读不准或满了就停。"""
        bag = self.ctx.info.get('bag', {})
        pets = bag.get('pet')
        if not isinstance(pets, (Mapping, list)) or state.num(bag.get('petMaxNum')) is None:
            raise Halt('宠物仓位未知，未捕捉')
        if self._occupied(pets) >= state.num(bag['petMaxNum']) and self.owed():
            self.refresh()  # 放了还没查的先查清，可能就有空位了
            bag = self.ctx.info.get('bag', {})
            pets = bag.get('pet')
            if not isinstance(pets, (Mapping, list)) or state.num(bag.get('petMaxNum')) is None:
                raise Halt('宠物仓位未知，未捕捉')
        if self._occupied(pets) >= state.num(bag['petMaxNum']):
            raise Halt('宠物仓位已满，清理空间后可继续')
        return bag, pets

    def _occupied(self, pets):
        """实际占用的仓位：快照里的宠物，加上快照之后抓到的，减去确认放生了的（放了还没查的仍算占着）。
        候选暂存、换宠后两次查询之间会连抓几只，只看快照会少算。"""
        held = {p.get('entityId') for p in (pets.values() if isinstance(pets, Mapping) else pets) if isinstance(p, Mapping)}
        for entity, rec in self.d['pets'].items():
            if rec['phase'] == 'released':
                held.discard(entity)
            else:
                held.add(entity)
        return len(held)

    def _catch_in_room(self, pet_id):
        """进一次捕捉副本，抓到或葫芦用完就按失败结算。返回抓到的实体编号，没抓到 None。"""
        client = dungeon.random.randint(0, 9999)
        created = self.op('field_step', dungeon.R_CREATE, {'mid': self.capture_mid, 'rnd': client})
        server = dungeon._find(created, ('rnd', 'random', 'serverRnd', 'server_rnd'))
        if server is None:
            raise Halt('捕捉房间随机数缺失，未继续')
        self.commit()
        caught = None
        remaining = self.bag(pet_catch.GOURD_ID)
        self.ctx.engage(True)
        # 葫芦用完就先结算：补购放在房间外，免得仓库/商店请求让房间状态不明确。
        while caught is None and remaining > 0 and not self.halted:
            reply = api.capture_pet(partial(self.op, 'capture'), pet_catch.GOURD_ID, pet_id)
            pushes = self.ctx.session.drain_pushes()
            flag = dungeon._find(reply, ('success',))
            if flag not in (0, 1):
                raise Halt('捕捉缺少明确结果，停止且不重发')
            remaining -= 1
            self._capture_remaining = remaining
            self.d['captureAttempts'] += 1
            if flag == 1:
                raw = pet_catch.entity(reply, pushes)
                if raw is None:
                    raise Halt('捕捉成功但缺少唯一实体，保留并停止')
                caught = self.add_captured(raw)
            self.commit()
        self.op('field_step', dungeon.R_FINISH, dungeon._finish_body(client, server, win=False))
        self.commit()
        self.ctx.engage(False)
        return caught

    def _can_hold(self, record, bag, pets):
        """这只候选能不能先不核验：没直接达标、不在穿插任务中，还要留够仓位。"""
        return (record and record['phase'] == 'ready' and self.d['cleanupRejected']
                and self.can_release_fresh() and not self.halted
                and max(record['initial']['passivity'].values(), default=0) < self.d['target']
                and self._occupied(pets)+1 < state.num(bag['petMaxNum']))

    def capture(self):
        if self._capture_remaining is not None and (self._capture_remaining <= 0 or not self.can_release_fresh()):
            self.refresh()
        bag, pets = self._pet_room()
        self.ensure_gourds()
        target = pet_catch.target(self.capture_mid)
        if target is None:
            raise Halt('本轮宠物捕捉配置未知')
        caught = self._catch_in_room(target[1])
        if not (caught and self.discard_fresh_capture(caught)):
            record = self.d['pets'].get(caught)
            if self._can_hold(record, bag, pets):
                # 候选先不查，留给下一只或上阵后的那次查询一起核验。
                record['captureUnverified'] = True
                self.save()
            else:
                self.refresh()
        self.ctx.log(f"益灵花{self.d['harvested']}/{self.d['goal']}；捕捉请求{self.d['captureAttempts']}次，已核验本轮新宠{len(self.d['pets'])}只")
        return caught

    def fill(self):
        slots = len(self.active_roles())
        while len(self.training()) < slots:
            if self.halted:
                return
            self.material()
            self.capture()
            self.terminal()
            if self.goal_reached:
                return

    # ---- 回收与放生 ----

    def release(self, entity, level, levels, purpose, meta=None):
        rec = self.d['pets'][entity]
        receipt = rec.get('receipt', {})
        surplus = purpose == 'surplus'
        auth = release_guard.CultivationDiscard(
            self.j.run_id, entity, rec['petId'], level, rec['nickname'], self.d['target'], purpose,
            tuple(sorted(levels.items())), receipt.get('skill', ''), receipt.get('level', 0), receipt.get('count', 0),
            capture_mid=self.capture_mid,
            completed_goal=self.d['goal'] if surplus else 0,
            confirmed_harvested=self.d['harvested'] if surplus else 0)
        body = api.free_pet_body(entity, rec['petId'], level)
        self.op('free', api.R_PET_FREE, body, meta,
                action=lambda: self.ctx.session.release_cultivation_discard(body, auth))

    def discard_fresh_capture(self, entity):
        """刚抓到就落选的，结算后立即放掉、不刷新，省下每只一次查询。返回是否放了。"""
        rec = self.d['pets'][entity]
        if (rec['phase'] != 'rejected' or not self.d['cleanupRejected']
                or self.halted or not self.can_release_fresh()):
            return False
        pet = self.ctx.session.pet_entity(entity)
        initial = rec['initial']
        if pet is None or pet.get('level') != 1 or initial.get('level') != 1:
            return False
        levels = policy.perks(pet)
        if (entity in self.d['originalIds'] or not self.ctx.session.cultivation_captured(entity)
                or not same_identity(pet, rec)
                or pet.get('capacity') != initial.get('capacity') or levels != initial.get('passivity')):
            raise Halt('新捕落选宠物的完整实体与捕获凭据不符，保留并停止')
        slots = rules.equipped_pets(self.ctx.info)
        if slots is None or any(equipped.get('entityId') == entity for _, equipped in slots):
            raise Halt('新捕落选宠物的上阵状态异常，未放生')
        if self.admissible(pet):
            rec['phase'] = 'ready'
            rec['entryDecision'] = copy.deepcopy(self._admission_note)
            rec.pop('rejectReason', None)
            self.save()
            return False
        self.release(entity, 1, levels, 'rejected', {'source': 'confirmed_fresh_capture'})
        # 成功回执先落盘，留到候选/短批边界统一查询；结果未知时仍停在 pending。
        rec['freshReleaseAck'] = True
        rec['phase'] = 'released_unverified'
        self.bump('freshReleaseCount')
        self.d['freshCaptureReleases'] = self.d.get('freshCaptureReleases', 0) + 1
        self.commit()
        self._verify_release_batch()
        return True

    def _verify_release_batch(self):
        """短批攒够了（窗口或仓位到顶）或不能再攒了，就刷新一次统一核验。"""
        bag = self.ctx.info.get('bag', {})
        limit = min(decision.FRESH_RELEASE_WINDOW,
                    max(1, (state.num(bag.get('petMaxNum')) or 1)-len(bag.get('pet', {}))-self.held()))
        if self.debts() >= limit or self.halted or not self.can_release_fresh():
            self.refresh()
            self.bump('freshVerificationCount')
            self.save()

    def discard(self, entity, purpose):
        rec = self.d['pets'][entity]
        if self.halted:
            return
        if not (self.d['autoRelease'] if purpose == 'recovered' else self.d['cleanupRejected']):
            rec['phase'] = 'kept'
            self.save()
            return
        if self._verified_info is not self.ctx.info:
            self.refresh()
        else:
            self.d['refreshReuses'] = self.d.get('refreshReuses', 0) + 1
        self.pet(entity)
        for slot, equipped in self.formation('处置前出战状态未知'):
            if equipped.get('entityId') == entity:
                self.takeoff(slot)
        pet = self.pet(entity)
        self.release(entity, pet['level'], policy.perks(pet), purpose)
        self.confirm()

    def targets(self, entity):
        return [(level, sid) for sid, level in policy.perks(self.pet(entity)).items() if level >= self.d['target']]

    def terminal(self):
        """回收达标潜能，处置回收完或落选的宠物。"""
        for entity, rec in list(self.d['pets'].items()):
            if self.halted:
                return
            if rec['phase'] in ('kept', 'released', 'released_unverified') or rec.get('captureUnverified'):
                continue
            while (eligible := self.targets(entity)) and not self.goal_reached and not self.halted:
                self.recover(entity, *min(eligible))
            if rec['phase'] in ('recovered', 'rejected') and not self._swap_later(entity):
                self.discard(entity, rec['phase'])

    def _has_material(self):
        try:
            return self.bag(rules.FORGET_FLOWER) > 0 or warehouse_count(self.ctx.info, rules.FORGET_FLOWER) > 0
        except Halt:
            return False

    def _finished_on_field(self, entity):
        return self.d['pets'].get(entity, {}).get('phase') in ('recovered', 'rejected')

    def _swap_later(self, entity):
        """还在出战位上的培养完的宠物先留着，等下一只直接上阵换下它，省一次卸下和一次查询。
        目标已完成、要停、有穿插任务、去灵花用完（马上要停）时照旧先卸下再处置。"""
        if self.goal_reached or self.halted or not self.can_release_fresh() or not self._has_material():
            return False
        slots = rules.equipped_pets(self.ctx.info)
        return slots is not None and any(pet.get('entityId') == entity for _, pet in slots)

    def recover(self, entity, level, sid):
        levels = policy.perks(self.pet(entity))
        self.material()
        expected = rules.pet_return_count(self.ctx.info, level)
        if expected is None or expected <= 0:
            raise Halt('VIP潜能回收益灵花数量未确认，未遗忘')
        meta = {'levels': levels, 'flowers': self.bag(rules.FLOWER),
                'forget': self.bag(rules.FORGET_FLOWER), 'expected': expected}
        self.op('remove', rules.R_REMOVE, {'entityId': entity, 'skillId': int(sid), 'totalNum': 0,
                                           'consume_subType': 0, 'skLev': level}, meta)
        self.ctx.session.drain_pushes()
        self.confirm()
        self.ctx.log(f"益灵花 {self.d['harvested']}/{self.d['goal']}：已核验{entity}潜能{sid}回收，其他潜能未变")


    # ---- 收尾 ----

    def finish(self):
        self.flush_formation_checks()
        if not self.goal_reached:
            raise Halt('刷取目标尚未完成，未清理剩余培养宠物')
        self.refresh_if_owed()
        for entity, rec in self.d['pets'].items():
            if self.halted:
                raise Halt('收尾已暂停，继续时接回剩余宠物清理和阵容恢复')
            if rec['phase'] == 'recovered':
                self.discard(entity, 'recovered')
            elif rec['phase'] in (*TRAINING, 'rejected', 'surplus'):
                pet = self.pet(entity)
                rec['phase'] = 'surplus' if policy.highest(pet) >= self.d['target'] else 'rejected'
                rec['rejectReason'] = '刷取目标已完成'
                rec['latest'] = copy.deepcopy(pet)
                self.save()
                self.discard(entity, rec['phase'])
        if self.halted:
            raise Halt('收尾已暂停，继续时接回阵容恢复')
        self.restore_formation()
        if self.d['loan'] > 0:
            self.transfer(R_TO, self.d['loan'])
        self.d['status'] = 'completed'
        self.tick()
        self.save()

    def restore_formation(self):
        original = self.d['originalFormation']
        current = self.formation_ids('结束时阵容未知')
        for slot, entity in current.items():
            if original.get(slot) != entity:
                if entity not in self.d['pets']:
                    raise Halt('结束时出现本轮以外的出战宠物，未覆盖')
                self.takeoff(int(slot))
        for slot, entity in original.items():
            actual = dict(self.formation('恢复原阵容时读不到出战宠物，停止')).get(int(slot))
            if actual is None:
                self.equip(int(slot), entity)
            elif actual['entityId'] != entity:
                raise Halt('原阵容恢复冲突，停止')
