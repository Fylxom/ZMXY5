"""准入：这只新宠值不值得培养，校准足够时用经济模型判断。"""
from __future__ import annotations

from ... import data
from .. import pet_catch
from . import decision, policy, task as rules
from .ledger import Ledger


class Admission(Ledger):
    def _experience_plan(self, stats, roles):
        """本轮实测的 (经验序列, 批次, 等级上限)；样本不够或角色间不一致返回 None，原因记进 decisionStatus。"""
        plans = stats.get('experiencePlan', {})
        stats['decisionStatus'] = 'waiting_experience_plan'
        if (stats['captures'] < 8 or stats['verifiedFields'] < 3 or not roles
                or any(not plans.get(slot) for slot in roles)):
            return None
        if any(plans[slot] != plans[roles[0]] for slot in roles):
            stats['decisionStatus'] = 'different_role_batches'
            return None
        ceilings = [rules.player_level(self.ctx.info, int(slot)) for slot in roles]
        if any(type(n) is not int or n < 2 for n in ceilings) or len(set(ceilings)) != 1:
            stats['decisionStatus'] = 'different_role_levels'
            return None
        units = tuple(plans[roles[0]])
        sizes = tuple(stats.get('experienceBatchSizes') or policy.default_batches(len(units)))
        if (not policy.batches_ok(sizes, len(units))
                or any(type(n) is not int or n < 0 for n in units) or not any(units)):
            stats['decisionStatus'] = 'invalid_experience_plan'
            return None
        return units, sizes, ceilings[0]

    def _two_star_rate(self):
        target = pet_catch.target(self.capture_mid)
        catch = data.row('PetCatch', target[1]) if target else None
        try:
            return float(str((catch or {}).get('catchUpQualityRate')).split(';')[0])
        except (TypeError, ValueError):
            return None

    def decision_model(self, pet):
        """校准足够且两个放生开关都开时的经济模型；否则 None，退回基础规则。"""
        if not (self.d.get('autoRelease') and self.d.get('cleanupRejected')):
            return None
        stats, roles = self.stats(), self.active_roles()
        plan = self._experience_plan(stats, roles)
        if plan is None:
            return None
        units, sizes, ceiling = plan
        growth = rules.growth_probability(pet)
        two_star = self._two_star_rate()
        if two_star is None or growth is None or not 0 < growth < 1 or not 0 <= two_star <= 1:
            return None
        early = self.can_release_fresh()
        identity = {'target': self.d['target'], 'p': growth, 'ceiling': ceiling, 'units': list(units),
                    'roles': list(roles), 'batchSizes': list(sizes), 'tableRoot': decision.table_identity(),
                    'earlyRelease': early, 'formationQueryVersion': decision.FORMATION_QUERY_VERSION,
                    'freshReleaseWindow': decision.FRESH_RELEASE_WINDOW if early else 1}
        old = stats.get('decisionModel')
        if (old and old.get('version') == decision.MODEL_VERSION and all(old.get(k) == v for k, v in identity.items())
                and stats['captures']-old.get('captureMark', 0) < 64):
            stats['decisionStatus'] = 'ready'
            return old
        return decision.priced_model(stats, identity, two_star)

    def admissible(self, pet):
        """这只值不值得培养。过程记进 ``_admission_note``，随宠物存档备查。"""
        self._admission_note = None
        target = self.d['target']
        base = policy.candidate(pet, target)
        if policy.highest(pet) >= target:
            return True
        if not base and not decision.extra_shape(pet, target):
            return False
        if type(pet.get('petEXP')) is not int:
            return base
        model = self.decision_model(pet)
        if model is None:
            return base
        lower, upper = decision.forecast(pet, model)
        alternative = decision.replacement_cost(model)
        self._admission_note = {'lower': lower, 'upper': upper, 'replacementCost': alternative, 'rate': model['rate']}
        return upper > alternative if base else lower > 1.05*alternative
