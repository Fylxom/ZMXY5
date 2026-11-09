"""刷益灵花入口：捕捉 → 培养 → 回收 → 放生 → 还原阵容，仓库借还，可核验续跑。

流程在 ``engine.py``，断点和核验在 ``ledger.py``，刷图在 ``field.py``。
"""
from __future__ import annotations

from collections.abc import Mapping

from ... import dungeon, protocol as p, release_guard, state
from ...core import BLOCKED, PAUSED, SKIPPED, SUBMITTED, TaskResult, UncertainOutcome
from . import task as rules
from .engine import Engine
from .journal import RunJournal
from .ledger import Halt, warehouse_count

MAX_HARVEST = 1000000


def _preflight(info):
    """开工前必须读准的状态：(VIP, 写入断点的字段)。"""
    vip = state.vip_level(info)
    if vip is None:
        raise Halt('VIP状态未知')
    target = 3 if vip >= 3 else 4
    refund = rules.pet_return_count(info, target)
    if refund is None or refund <= 0:
        raise Halt('VIP目标潜能回收益灵花数量未确认，未捕捉或刷图')
    roles = ['0'] if info.get('p2') is None else ['0', '1']
    if any((rules.player_level(info, int(i)) or 0) <= 1 for i in roles):
        raise Halt('实际角色槽的等级未知或没有宠物升级余量')
    formation = rules.equipped_pets(info)
    if formation is None:
        raise Halt('起始宠物阵容未知')
    if len({pet['entityId'] for _, pet in formation}) != len(formation):
        raise Halt('原阵容存在重复实体，未执行')
    pets = info.get('bag', {}).get('pet')
    pets = list(pets.values()) if isinstance(pets, Mapping) else pets
    if (not isinstance(pets, list)
            or any(not isinstance(pet, Mapping) or not isinstance(pet.get('entityId'), str) or not pet['entityId'] for pet in pets)):
        raise Halt('原宠物清单身份不完整，未开始捕捉')
    return vip, {'target': target, 'roles': roles,
                 'originalIds': sorted({pet['entityId'] for pet in pets} | {pet['entityId'] for _, pet in formation}),
                 'originalFormation': {str(i): pet['entityId'] for i, pet in formation}}


def _start(ctx, journal, info, lv, goal, capture_mid, plan):
    own = state.bag_count(info, rules.FORGET_FLOWER)
    if own is None:
        raise Halt('去灵花背包数量未知')
    if own <= 0 and warehouse_count(info, rules.FORGET_FLOWER) <= 0:
        raise Halt('背包和仓库都没有去灵花，未捕捉或刷图')
    journal.initialize(map=lv.mid, captureMap=capture_mid, goal=goal, **plan,
                       autoRelease=ctx.options.get('auto_release') is True,
                       cleanupRejected=ctx.options.get('cleanup_rejected') is True,
                       pets={}, selected={}, harvested=0, loan=0, ownFlowers=own, bought=0, fields=0,
                       captureAttempts=0, pending=None, field=None, status='running')
    ctx.session.begin_cultivation_capture_tracking(run_id=journal.run_id, capture_mid=capture_mid)


def _check_resume(ctx, journal, goal, capture_mid, plan):
    d = journal.data
    if (d['goal'], d['target'], d['roles']) != (goal, plan['target'], plan['roles']):
        raise Halt('刷取目标、VIP档位或角色槽与断点不符，未重开捕捉')
    if d.get('captureMap', rules.CAPTURE_PET) != capture_mid:
        raise Halt('捕捉种类与断点不符，未混用宠物或重开捕捉')
    # 续跑只能关掉放生开关，不能打开。
    d['autoRelease'] = d['autoRelease'] and ctx.options.get('auto_release') is True
    d['cleanupRejected'] = d['cleanupRejected'] and ctx.options.get('cleanup_rejected') is True


def _pause(ctx, journal, engine, detail):
    if engine is not None:
        engine.tick()
    journal.data['status'] = 'paused'
    journal.save()
    return TaskResult(rules.ID, PAUSED, detail, progress=dict(ctx.progress))


def _harvest(ctx, engine, goal):
    """主循环；返回 TaskResult。"""
    d = engine.d
    while d['harvested'] < goal:
        if engine.halted:
            return _pause(ctx, engine.j, engine, f"已暂停；益灵花{d['harvested']}/{goal}，继续时接回同一批宠物")
        engine.terminal()
        if d['harvested'] >= goal:
            break
        engine.material()
        engine.fill()
        if d['harvested'] >= goal:
            break
        if engine.halted:
            continue
        engine.arrange()
        engine.field()
    engine.finish()
    count = d['harvested']-d['timingStartHarvest']
    timing = f"；计时以来运行{d['activeSeconds']/60:.1f}分钟"
    if count > 0:
        timing += f"，新增收益平均{d['activeSeconds']/60/count:.2f}分钟/朵"
    return TaskResult(rules.ID, SUBMITTED, f"益灵花{d['harvested']}/{goal}，已确认到账；刷图{d['fields']}场，补购葫芦{d['bought']}个；剩余借用去灵花已归还、原阵容已核验{timing}")


def run(ctx):
    raw = ctx.options.get('harvest_times')
    goal = 0 if raw in (None, '') else state.num(raw)
    if goal is None or not 0 <= goal <= MAX_HARVEST:
        return TaskResult(rules.ID, BLOCKED, '刷取次数必须是0–1000000的整数')
    if goal == 0:
        return TaskResult(rules.ID, SKIPPED, '刷取次数留空，不执行')
    capture_mid = state.num(ctx.options.get('capture_pet', rules.CAPTURE_PET))
    if capture_mid not in release_guard.CULTIVATION_FAMILIES:
        return TaskResult(rules.ID, BLOCKED, '培养捕捉种类无效，未执行')
    journal = engine = None
    try:
        info = ctx.refresh()
        lv = rules.latest_level(info)
        vip, plan = _preflight(info)
        journal = RunJournal(ctx)
        if journal.resumed:
            _check_resume(ctx, journal, goal, capture_mid, plan)
        else:
            _start(ctx, journal, info, lv, goal, capture_mid, plan)
        engine = Engine(ctx, journal)
        engine.verify_formation_checks()
        # 先按权威状态核验借还/购买/回收/放生，再恢复放生名单；旧写操作不重发。
        engine.resolve_pending()
        engine.verify_fresh_releases()
        ctx.session.restore_cultivation_capture_tracking(journal.run_id, engine.d['pets'], engine.d['originalIds'], capture_mid=capture_mid)
        engine.settle_field_state()
        # 旧场先按断点结清，再切到当前自动选的关；不复用旧场随机数和掉落。
        engine.d['map'] = lv.mid
        engine.d['status'] = 'running'
        engine.save()
        ctx.log(f'自动培养主线：{lv.name}（{lv.mid}），已排除蓬丘、七星殿')
        mode = '双宠培养' if len(engine.d['roles']) == 2 else '1P单宠培养'
        ctx.log(f"VIP{vip}目标{plan['target']}级潜能，{mode}；益灵花{engine.d['harvested']}/{goal}")
        return _harvest(ctx, engine, goal)
    except UncertainOutcome:
        raise
    except (Halt, ValueError, OSError, p.AllianceError, dungeon.Stop) as exc:
        if journal is not None and journal.data:
            return _pause(ctx, journal, engine, str(exc))
        return TaskResult(rules.ID, BLOCKED, str(exc))
    except Exception as exc:
        reason = f'{type(exc).__name__}: {exc}'
        if journal is not None and journal.data:
            journal.data['lastError'] = reason
            return _pause(ctx, journal, engine, f'运行中断，已保留实体断点：{reason}')
        return TaskResult(rules.ID, BLOCKED, f'培养前置状态无法核验：{reason}')
