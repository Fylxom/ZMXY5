"""培养候选的筛选规则与有限成长概率；概率只是重复抽取的条件模型。"""
from __future__ import annotations

from functools import lru_cache

BATCH = 24  # 一批经验拾取的请求上限
BASE_RATE = {3: 0.0053408894, 4: 0.0005099731}  # 按目标潜能等级的基准达标率（只/秒）


def perks(pet):
    """{潜能 id: 等级}；等级、容量或潜能读不准就抛错，调用方保留宠物并停止。"""
    skill = pet.get('skill')
    raw = skill.get('passivity') if isinstance(skill, dict) else None
    cap, level = pet.get('capacity'), pet.get('level')
    if (not isinstance(raw, dict) or type(cap) is not int or not 1 <= cap <= 18 or type(level) is not int or level < 1
            or len(raw) > cap or any(type(v) is not int or v < 1 for v in raw.values())):
        raise ValueError('宠物等级、容量或完整潜能未知，保留并停止')
    return {str(k): v for k, v in raw.items()}


def highest(pet):
    return max(perks(pet).values(), default=0)


def default_batches(count):
    return [min(BATCH, count-i) for i in range(0, count, BATCH)]


def batches_ok(sizes, total):
    return bool(sizes) and all(type(n) is int and 1 <= n <= BATCH for n in sizes) and sum(sizes) == total


@lru_cache(None)
def chance(ranks, capacity, target):
    if ranks and max(ranks) >= target:
        return 1.0
    if len(ranks) == capacity:
        return 0.0
    n = len(ranks)
    p = (18-n)/18 * chance(tuple(sorted(ranks + (1,))), capacity, target)
    for i in range(n):
        child = list(ranks)
        child[i] += 1
        p += chance(tuple(sorted(child)), capacity, target)/18
    return p


def candidate(pet, target):
    levels = perks(pet)
    best = max(levels.values(), default=0)
    if best >= target:
        return True
    return len(levels) < pet['capacity'] and (best >= 2 or pet['capacity'] >= 4)


def score(pet, target, seconds=None):
    levels = perks(pet)
    p = chance(tuple(sorted(levels.values())), pet['capacity'], target)
    # 只用于排优先级；5 槽的时间是右删失基准，不代表完整平均培养时间。
    seconds = seconds if seconds is not None and seconds > 0 else (9.464 if pet['capacity'] >= 5 else 2.95)
    return p/seconds, max(levels.values(), default=0), -len(levels), pet['capacity']


def state_budget(pet, target, opportunity_rate=None):
    """用重抓的机会成本限制无潜能进展的探索；状态变化才重新评估。"""
    p = chance(tuple(sorted(perks(pet).values())), pet['capacity'], target)
    rate = opportunity_rate if opportunity_rate is not None and opportunity_rate > 0 else BASE_RATE[target]
    return p / rate
