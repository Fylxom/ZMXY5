"""培养计时与保守收益校准；统计随断点保存，不产生游戏请求。"""
from __future__ import annotations

import hashlib
import json
import math
import statistics

from .policy import BASE_RATE, batches_ok, default_batches

PRIOR_TARGETS = 10.0  # 十次模型产出的先验权重，避免一次稀有出货使淘汰阈值骤变。
EPOCH_COUNTERS = ('seconds', 'captures', 'fields', 'verifiedFields')
EPOCH_DERIVED = ('experiencePlan', 'experienceBatchSizes', 'decisionModel', 'decisionStatus', 'batchTimings')


def state_key(pet):
    return repr(sorted((str(k), v) for k, v in pet['skill']['passivity'].items()))


def bucket(data, info, roles):
    fingerprint = [data['map'], data['target'], roles,
                   [info['p1' if r == '0' else 'p2']['level'] for r in roles], info.get('buffs')]
    key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    root = data.setdefault('performance', {'version': 1, 'contexts': {}})
    return root['contexts'].setdefault(key, {'key': key, 'seconds': 0.0, 'costs': {}, 'captures': 0,
        'fields': 0, 'verifiedFields': 0, 'targets': [], 'growth': {}, 'lastExperience': {}})


def cost(stats, kind, seconds):
    if not math.isfinite(seconds) or seconds < 0:
        return
    stats['seconds'] += seconds
    row = stats['costs'].setdefault(kind, {'count': 0, 'seconds': 0.0})
    row['count'] += 1
    row['seconds'] += seconds


def rate(stats, target):
    baseline = BASE_RATE[target]
    # 数据不足时不动机会率；校准用实际达标宠数，一次到账多朵也只算一只。
    if stats['captures'] < 8 or stats['verifiedFields'] < 3:
        return baseline
    return (PRIOR_TARGETS + len(stats['targets'])) / (PRIOR_TARGETS / baseline + stats.get('activeSeconds', stats['seconds']))


def reward_changed(stats, signature):
    """同一角色还有共同经验单位时，只是正常的掉落组合变化，不重置校准。"""
    old = stats.get('rewardSignature')
    if old is None or set(old) != set(signature):
        return False
    return any(set(old[slot]).isdisjoint(signature[slot]) for slot in old if old[slot] and signature[slot])


def calibrate(stats, signature):
    """奖励规格真变了才重置，旧区间汇总留着审计。"""
    if not reward_changed(stats, signature):
        old = stats.get('rewardSignature') or {}
        stats['rewardSignature'] = {slot: sorted(set(old.get(slot, [])) | set(values))
                                    for slot, values in signature.items()}
        return
    past = stats.setdefault('previousEpochs', {'seconds': 0.0, 'captures': 0, 'fields': 0,
        'verifiedFields': 0, 'targets': 0})
    for key in EPOCH_COUNTERS:
        past[key] += stats[key]
    past['activeSeconds'] = past.get('activeSeconds', 0.0) + stats.get('activeSeconds', stats['seconds'])
    past['targets'] += len(stats['targets'])
    stats.update(seconds=0.0, costs={}, captures=0, fields=0, verifiedFields=0,
                 targets=[], growth={}, lastExperience={}, activeSeconds=0.0,
                 freshReleaseCount=0, freshVerificationCount=0, equipVerificationCount=0)
    for key in EPOCH_DERIVED:
        stats.pop(key, None)
    stats['rewardEpoch'] = stats.get('rewardEpoch', 0) + 1
    stats['rewardSignature'] = signature


def growth(stats, key, seconds, terminal):
    row = stats['growth'].setdefault(key, {'complete': [], 'censored': 0})
    if terminal:
        row['complete'] = (row['complete'] + [seconds])[-20:]
    else:
        row['censored'] += 1


def estimate(stats, key):
    row = stats['growth'].get(key, {})
    values = row.get('complete', [])
    # 有超时/等级截断时，完成样本偏向快宠，不能据此估完整培养时间。
    if len(values) < 3 or row.get('censored', 0):
        return None
    return statistics.median(values) + 1.35  # 冷启动装卸参考，仅用于排序。


def response_exp(replies):
    """{'0': 1P 宠物经验, '1': 2P 宠物经验}；只认 index:0/1 的 petExp 条目。"""
    totals = {}

    def visit(value):
        if isinstance(value, dict):
            if str(value.get('subType', '')).lower() == 'petexp':
                entity, count = value.get('entityId'), value.get('count')
                if entity in ('index:0', 'index:1') and type(count) is int and count >= 0:
                    totals[entity[-1]] = totals.get(entity[-1], 0) + count
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(replies)
    return totals


def complete_experience_plan(field, slots):
    """保留真实网络批次，零填充另一角色的掉落，不要求一条应答给两只宠物。"""
    count = field.get('pickupCount')
    if type(count) is not int or count <= 0 or count != field.get('availablePetDrops'):
        return None
    sizes = field.get('experienceBatchSizes')
    if sizes is None:
        sizes = default_batches(count)
    if not isinstance(sizes, list) or not batches_ok(sizes, count):
        return None
    plans = {}
    for slot in slots:
        raw = field.get('experiencePlan', {}).get(slot)
        if (not isinstance(raw, list) or len(raw) != count
                or any(type(n) is not int or n < 0 for n in raw)
                or sum(raw) <= 0 or sum(raw) != field.get('experience', {}).get(slot)):
            return None
        # 零经验请求不触发升级；只在同一批内移动零，保留正经验的顺序和批次边界。
        plans[slot] = []
        start = 0
        for size in sizes:
            positive = [n for n in raw[start:start+size] if n > 0]
            plans[slot].extend(positive+[0]*(size-len(positive)))
            start += size
    return plans, list(sizes)


def experience_delta(before, after, threshold):
    try:
        a, b = before['level'], after['level']
        if type(a) is not int or type(b) is not int or not 1 <= a <= b:
            return None
        values = [threshold(level) for level in range(a, b)]
        if any(type(v) is not int or v <= 0 for v in values):
            return None
        if any(type(p.get('petEXP')) is not int or p['petEXP'] < 0 for p in (before, after)):
            return None
        delta = sum(values) + after['petEXP'] - before['petEXP']
        return delta if delta >= 0 else None
    except (LookupError, TypeError, ValueError):
        return None
