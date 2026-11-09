"""用实得经验、当前等级和全部流程成本，评估额外准入及单宠剩余价值。"""
from __future__ import annotations

import bisect
import copy
import math
from collections import defaultdict
from functools import lru_cache

from ... import data
from .policy import batches_ok, chance, default_batches, perks

WIN, FULL = 'win', 'full'
MODEL_VERSION = 3
FORMATION_QUERY_VERSION = 1  # 合并查询的成本口径变了要加一，旧模型才会重算
FRESH_RELEASE_WINDOW = 8  # 初筛放生最多积累这么多只再统一刷新核验
EXPERIENCE_MODELS = ('D+1', 'max(D,2)')  # 单次经验跨 D 级时的领悟次数，真实规则未知，两个都算取保守值


@data.cached
def table_identity():
    for function in (thresholds, stages, bounds, compile_model):
        function.cache_clear()
    return str(data.TABLE_ROOT)


@lru_cache(16384)
def draws(ranks, cap, target, count):
    if max(ranks, default=0) >= target:
        return ((WIN, 1.0),)
    if len(ranks) >= cap:
        return ((FULL, 1.0),)
    if count == 0:
        return ((ranks, 1.0),)
    children = [(tuple(sorted(ranks+(1,))), (18-len(ranks))/18)]
    for i in range(len(ranks)):
        child = list(ranks)
        child[i] += 1
        children.append((tuple(sorted(child)), 1/18))
    result = defaultdict(float)
    for child, weight in children:
        for outcome, p in draws(child, cap, target, count-1):
            result[outcome] += weight*p
    return tuple(result.items())


@lru_cache(16384)
def attempts(ranks, cap, target, p, count):
    maximum = (target-1)*(cap-1)-sum(ranks)+1
    if count <= 0:
        return ((ranks, 1.0),)
    result = defaultdict(float)
    prob = (1-p)**count
    total = 0.
    for k in range(min(count, maximum-1)+1):
        total += prob
        for child, weight in draws(ranks, cap, target, k):
            result[child] += prob*weight
        if k < count:
            prob *= (count-k)/(k+1)*p/(1-p)
    if count >= maximum:
        for child, weight in draws(ranks, cap, target, maximum):
            result[child] += max(0., 1-total)*weight
    return tuple(result.items())


@lru_cache(16384)
def kernel(ranks, cap, target, p, counts):
    active = {ranks: 1.}
    won = 0.
    survival = []
    for count in counts:
        survival.append(sum(active.values()))
        next_active = defaultdict(float)
        for outcome, weight in active.items():
            for child, probability in attempts(outcome, cap, target, p, count):
                if child == WIN:
                    won += weight*probability
                elif child != FULL:
                    next_active[child] += weight*probability
        active = next_active
    return won, tuple(active.items()), tuple(survival)


@lru_cache(128)
def thresholds(ceiling):
    result = [0]
    for lv in range(1, ceiling):
        value = (data.row('PetEXP', lv) or {}).get('upgradeEXP')
        if type(value) is not int or value <= 0:
            raise ValueError('宠物经验门槛未知')
        result.append(result[-1]+value)
    return tuple(result)


@lru_cache(4096)
def stages(total, ceiling, units, model, horizon, batch_sizes=()):
    boundaries = thresholds(ceiling)
    sizes = batch_sizes or default_batches(len(units))
    if not batches_ok(sizes, len(units)):
        raise ValueError('经验批次边界未知')
    result = []
    for _ in range(horizon):
        if total >= boundaries[-1]:
            break
        counts = []
        offset = 0
        for size in sizes:
            n = 0
            for unit in units[offset:offset+size]:
                before = min(ceiling, bisect.bisect_right(boundaries, total))
                total += unit
                d = min(ceiling, bisect.bisect_right(boundaries, total))-before
                if d:
                    n += d+1 if model == 'D+1' else max(2, d)
            counts.append(n)
            offset += size
        result.append((tuple(counts), total >= boundaries[-1]))
    return tuple(result)


@lru_cache(4096)
def bounds(ranks, cap, target, p, total, ceiling, units, batch_times, fixed, recovery, rate, horizon, model, batch_sizes=()):
    if max(ranks, default=0) >= target:
        value = max(0., 1-rate*recovery)
        return value, value
    if chance(ranks, cap, target) == 0:
        return 0., 0.
    sequence = stages(total, ceiling, units, model, horizon, batch_sizes)

    @lru_cache(None)
    def visit(current, at):
        if at == len(sequence):
            if at < horizon:
                return 0., 0.
            return 0., chance(current, cap, target)*max(0., 1-rate*recovery)
        counts, capped = sequence[at]
        win, alive, survival = kernel(current, cap, target, p, counts)
        cost = fixed+sum(s*t for s, t in zip(survival, batch_times))
        lower = upper = win*(1-rate*recovery)-rate*cost
        if not capped:
            for child, weight in alive:
                lo, hi = visit(child, at+1)
                lower += weight*lo
                upper += weight*hi
        return max(0., lower), max(0., upper)

    return visit(ranks, 0)


def birth_population(two_star):
    for cap in range(1, 6):
        weight = ((1-two_star)/4 if cap <= 4 else 0)+two_star/5
        births = (((), .5), ((1,), .5)) if cap == 1 else (((), .35), ((1,), .5), ((1, 1), .15*17/18), ((2,), .15/18))
        for ranks, probability in births:
            if len(ranks) < cap:
                yield cap, ranks, weight*probability


def forecast(pet, model, horizon=12):
    ranks = tuple(sorted(perks(pet).values()))
    total = thresholds(model['ceiling'])[pet['level']-1]+pet['petEXP']
    values = [bounds(ranks, pet['capacity'], model['target'], model['p'], total, model['ceiling'],
                     tuple(model['units']), tuple(model['batchTimes']), model['fixed'], model['recovery'],
                     model['rate'], horizon, kind, tuple(model.get('batchSizes', ())))
              for kind in EXPERIENCE_MODELS]
    return min(x[0] for x in values), max(x[1] for x in values)


@lru_cache(128)
def compile_model(target, p, ceiling, units, batch_times, fixed, recovery, capture, reject, setup, two_star, batch_sizes=()):
    rates = []
    for kind in EXPERIENCE_MODELS:
        lo, hi = 0., 1/capture
        for _ in range(18):
            rate = (lo+hi)/2
            net = -rate*(capture+reject)
            for cap, ranks, weight in birth_population(two_star):
                if not chance(ranks, cap, target):
                    continue
                value = bounds(ranks, cap, target, p, 0, ceiling, units, batch_times, fixed, recovery,
                               rate, 24, kind, batch_sizes)[0]
                net += weight*max(0., value-rate*(setup-reject))
            if net > 0:
                lo = rate
            else:
                hi = rate
        rates.append((lo+hi)/2)
    return {'version': MODEL_VERSION, 'target': target, 'p': p, 'ceiling': ceiling, 'units': list(units),
            'batchSizes': list(batch_sizes), 'batchTimes': list(batch_times), 'fixed': fixed,
            'recovery': recovery, 'capture': capture, 'reject': reject, 'setup': setup, 'twoStar': two_star,
            'rate': max(rates), 'candidateRates': rates}


def priced_model(stats, identity, two_star):
    """用本轮实测耗时给模型定价并存进 stats；样本不够返回 None。``identity`` 是决定模型是否过期的那些字段。"""
    def mean(kind, fallback=None):
        row = stats['costs'].get(kind, {})
        return row['seconds']/row['count'] if row.get('count', 0) > 0 else fallback

    query, simple, capture = mean('refresh'), mean('field_step'), mean('capture')
    timings = stats.get('batchTimings', {})
    if query is None or simple is None or capture is None or not timings:
        return None
    common = max(timings, key=lambda size: timings[size]['count'])
    if timings[common]['count'] < 3:
        return None
    roles, sizes, early = identity['roles'], identity['batchSizes'], identity['earlyRelease']
    operations = sum(row['count'] for kind, row in stats['costs'].items() if kind != 'refresh')
    overhead = max(0., stats['activeSeconds']-stats['seconds'])/max(1, operations)
    batch_times = []
    for size in sizes:
        row = timings.get(str(size), timings[common])
        batch_times.append(round((row['seconds']/row['count']+overhead)/len(roles), 6))
    free = mean('free', simple)
    query_share = 1.
    if early and stats.get('freshReleaseCount', 0):
        query_share = min(1., max(1/FRESH_RELEASE_WINDOW,
                                  stats.get('freshVerificationCount', 0)/stats['freshReleaseCount']))
    equip_count = stats['costs'].get('equip', {}).get('count', 0)
    equip_share = min(1., max(.5, stats.get('equipVerificationCount', equip_count)/max(1, equip_count)))
    tries = stats['costs']['capture']['count']/stats['captures']
    prices = [round(n, 6) for n in (
        (2*simple+query+2*overhead)/len(roles),  # fixed
        mean('remove', simple)+query+overhead,  # recovery
        2*simple+capture*tries+(2+tries)*overhead,  # capture
        free+query*query_share+overhead+(0 if early else query),  # reject
        mean('equip', simple)+mean('takeoff', simple)+free+(3+equip_share)*query+3*overhead)]  # setup
    if any(not math.isfinite(n) or n <= 0 for n in prices):
        return None
    try:
        model = copy.deepcopy(compile_model(identity['target'], identity['p'], identity['ceiling'],
                                            tuple(identity['units']), tuple(batch_times), *prices,
                                            two_star, tuple(sizes)))
    except (ValueError, data.DataError):
        return None
    model.update(identity, captureMark=stats['captures'], overheadPerOperation=overhead)
    stats['decisionModel'] = model
    stats['decisionStatus'] = 'ready'
    return model


def replacement_cost(model):
    """放掉这只、换一只新候选要付的价。"""
    return model['rate']*max(0., model['setup']-model['reject'])


def extra_shape(pet, target):
    """3 槽、只有一个 1 级潜能：基础规则不收，模型算得过才破格。"""
    return target == 3 and pet.get('capacity') == 3 and sorted(perks(pet).values()) == [1]


def extra_candidate(pet, model):
    return (extra_shape(pet, model['target'])
            and forecast(pet, model)[0] > 1.05*replacement_cost(model))
