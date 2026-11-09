"""镇妖塔（周常页）：只做普通免费挑战 ``type=0``，一组五个小层消耗一次免费次数。

流程（客户端 DTProxy / CountDownProgress）：``switchToLayer {layer:0}`` 读当前层组（``demonTower.cur``：layer、index、towerIds，
``count`` 是剩余免费次数，``bestRecord.layer`` 是已解锁的最高层组）→ 每个小层 ``beginChallenge {type:0}`` → ``endChallenge {win:1…}``
→ 结算里的掉落按 ``dropIndex`` 逐个 ``getReward {index}`` 领取。小层之间**不能**再调 ``switchToLayer``（会重置服务器进度）。

每一步都核对服务器状态：开始后 ``count`` 必须恰好少 1、``cur.index`` 必须等于小层序号 − 2；结算后 ``cur.index`` 必须等于序号 − 1；
整组结束后重新读 ``count`` 必须恰好少 1。任何一项对不上就停止，不继续下一层。
选项：``levels`` = 层组编号列表（``DemonTowerChallenge`` 表的 id），``times`` = 每个层组打几组（留空不执行）。付费挑战（type=1）、购买次数一律不做。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .. import data, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, TaskContext, TaskResult, task

ID = "demon_tower"
R_QUERY = "scene.demonTowerHandler.switchToLayer"
R_BEGIN = "scene.demonTowerHandler.beginChallenge"
R_END = "scene.demonTowerHandler.endChallenge"
R_REWARD = "scene.demonTowerHandler.getReward"
MAX_STAGE = 5  # TowerModelManager.MaxStage
MAX_TIMES = 5  # 次数输入框的上限（每日 5 次，5 个小层算一次）
END_BODY = {"win": 1, "maxCombo": 0, "killMonsterCnt": 0, "useMagicWeaponCnt": 0}  # CountDownProgress.win()


def _range(layer: int) -> str:
    first = (layer - 1) * 10 + 1  # DTManager：currLayer = (layer-1)*10 + …
    return f"第{first}-{first + MAX_STAGE - 1}层"


def catalog() -> list[tuple[int, str]]:
    return [(layer, _range(layer)) for layer in sorted(int(k) for k in data.table("DemonTowerChallenge") if int(k) > 0)]


@dataclass(frozen=True)
class Tower:
    layer: int
    index: int | None
    count: int | None
    best: int | None
    floors: tuple[int, ...]  # 塔配置编号（DungeonTower 表）


def _find(value: Any, depth: int = 6) -> Mapping[str, Any] | None:
    if depth < 0:
        return None
    if isinstance(value, Mapping):
        if isinstance(value.get("demonTower"), Mapping):
            return value["demonTower"]
        for key in ("body", "data", "result", "response", "message", "info"):
            if (found := _find(value.get(key), depth - 1)) is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            if (found := _find(child, depth - 1)) is not None:
                return found
    return None


def parse(response: Any) -> Tower:
    """读 demonTower 状态；缺字段或对不上本地配置就抛 ValueError。"""
    tower = _find(response)
    cur = tower.get("cur") if tower else None
    if not isinstance(cur, Mapping):
        raise ValueError("响应没有 demonTower.cur")
    layer, ids = state.num(cur.get("layer")), cur.get("towerIds")
    if layer is None or layer <= 0:
        raise ValueError("当前层组无效（服务器没有给出可直接执行的层组）")
    if not isinstance(ids, list) or not 0 < len(ids) <= MAX_STAGE:
        raise ValueError("demonTower.cur.towerIds 无效")
    floors = tuple(state.num(i) or 0 for i in ids)
    if len(set(floors)) != len(floors) or any(data.row("DungeonTower", f) is None for f in floors):
        raise ValueError("小层列表重复或不在本地塔配置里")
    best = tower.get("bestRecord")
    return Tower(layer, state.num(cur.get("index")), state.num(tower.get("count")),
                 state.num(best.get("layer")) if isinstance(best, Mapping) else None, floors)


def floor_name(tower_id: int) -> str:
    row = data.row("Dungeon", (data.row("DungeonTower", tower_id) or {}).get("dungeonID")) or {}
    return str(row.get("name") or "小层")


def _drop_indexes(value: Any, out: list[int], missing: list[int], depth: int = 6) -> None:
    """结算响应里 ``give`` 下每个物品的 dropIndex（没有 dropIndex 的计入 missing）。"""
    if depth < 0:
        return
    if isinstance(value, list):
        for child in value:
            _drop_indexes(child, out, missing, depth - 1)
    elif isinstance(value, Mapping):
        give = value.get("give")
        if give is not None:
            for item in give if isinstance(give, list) else [give]:
                if isinstance(item, Mapping):
                    index = state.num(item.get("dropIndex"))
                    if index is None:
                        missing.append(1)
                    elif index not in out:
                        out.append(index)
            return
        for child in value.values():
            if isinstance(child, (list, Mapping)):
                _drop_indexes(child, out, missing, depth - 1)


def _collect(ctx: TaskContext, finished: Mapping[str, Any], gains: dungeon.Gains) -> str | None:
    """领取小层掉落。返回 None 表示成功，否则是失败原因（此时不能继续下一层）。"""
    indexes: list[int] = []
    missing: list[int] = []
    _drop_indexes(finished, indexes, missing)
    for index in indexes:
        try:
            response = ctx.request(R_REWARD, {"index": index})
        except p.AllianceError as exc:
            return f"掉落 index={index} 领取失败：{exc}"
        got = dungeon.Gains()
        got.add(response, [])
        if not got.items:
            return f"掉落 index={index} 的响应没有 give 发放记录，未计作已领取"
        gains.items.update(got.items)
    if missing:
        return f"结算里有 {len(missing)} 项掉落没有 dropIndex，已领取其余的，未猜索引"
    return None


class Halt(Exception):
    """这一组到此为止（原因是异常文字）。spent：这一组的免费次数已经被服务器扣掉（断点续跑时这一组算已打，不能再补一组）。"""

    def __init__(self, why: str, spent: bool = False) -> None:
        super().__init__(why)
        self.spent = spent


def _read(ctx: TaskContext, layer: int = 0) -> Tower:
    try:
        tower = parse(ctx.request(R_QUERY, {"layer": layer}))
    except (ValueError, p.AllianceError) as exc:
        raise Halt(f"读取服务器层组失败：{exc}") from exc
    if tower.count is None or tower.count < 0:
        raise Halt(f"第 {tower.layer} 层组没有返回有效的剩余次数")
    return tower


def _select(ctx: TaskContext, wanted: int) -> Tower:
    """切到要打的层组。必须在这一组开始之前做，且已解锁。"""
    tower = _read(ctx)
    if tower.layer == wanted:
        return tower
    if tower.best is None:
        raise Halt("服务器没有返回已解锁的最高层组，无法确认所选层组是否解锁")
    if wanted > tower.best:
        raise Halt(f"{_range(wanted)} 未解锁（当前最高可选第 {tower.best} 层组）")
    selected = _read(ctx, wanted)
    if selected.layer != wanted or selected.count != tower.count:
        raise Halt(f"切换到 {_range(wanted)} 后状态对不上（层组 {selected.layer}，次数 {tower.count}→{selected.count}），未挑战")
    return selected


def run_group(ctx: TaskContext, wanted: int, gains: dungeon.Gains, group: int = 1) -> int:
    """打一组（五个小层）。返回已完成的小层数（0 = 没有免费次数）；失败抛 Halt。"""
    tower = _select(ctx, wanted)
    left = tower.count
    if left < 1:  # count 每组都从服务器重读，已含上一组的消耗，不能再减本连接已用次数（会重复扣）
        return 0
    floors, done = tower.floors, 0
    for number in range(1, len(floors) + 1):
        if ctx.stopped:
            raise Halt(f"已停止，本组完成 {done}/{len(floors)} 个小层", spent=done > 0)
        where = f"{_range(wanted)} 第 {number}/{len(floors)} 小层 {floor_name(floors[number - 1])}"
        try:
            begun = parse(ctx.request(R_BEGIN, {"type": 0}))
        except (ValueError, p.AllianceError) as exc:
            raise Halt(f"{where} 开始失败：{exc}；未结算", spent=done > 0) from exc
        if number == 1 and len(begun.floors) == len(floors):
            floors = begun.floors  # 进组时服务器可能重新排了小层顺序
        if (begun.layer, begun.count, begun.index, begun.floors) != (wanted, left - 1, number - 2, floors):
            raise Halt(f"{where} 开始后状态异常（层组={begun.layer}，次数={begun.count}，进度={begun.index}），已停止，未结算", spent=done > 0 or begun.count != left)
        try:
            finished_raw = ctx.request(R_END, END_BODY)
            ended = parse(finished_raw)
        except (ValueError, p.AllianceError) as exc:
            raise Halt(f"{where} 结算失败：{exc}；本组完成 {done}/{len(floors)}", spent=True) from exc
        pushes, pending, missing = ctx.session.drain_pushes(), [], []
        _drop_indexes(finished_raw, pending, missing)
        if not pending and not missing:  # 结算的 give 是待领取的掉落（带 dropIndex）：只按 getReward 的回执计数，不然同一份奖励算两遍
            gains.add(finished_raw, pushes)
        if (ended.layer, ended.count, ended.index, ended.floors) != (wanted, left - 1, number - 1, floors):
            raise Halt(f"{where} 结算后状态异常（进度={ended.index}，次数={ended.count}），已停止", spent=True)
        done += 1
        if reason := _collect(ctx, finished_raw, gains):
            raise Halt(f"{where}：{reason}；本组完成 {done}/{len(floors)}，请先核对背包和进度", spent=True)
        ctx.log(f"{_range(wanted)} 第{group}组 {done}/{len(floors)}")
    after = _read(ctx)
    if after.count != left - 1:
        raise Halt(f"{_range(wanted)} 整组结束后次数为 {after.count}，预期 {left - 1}，已停止", spent=True)
    return done


def auto_times(ctx: TaskContext) -> int | None:
    """界面“自动配置”：服务器当前层组的剩余免费次数（只读 switchToLayer）。"""
    try:
        return _read(ctx).count
    except Halt:
        return None


@task(ID, "镇妖塔", reads={R_QUERY}, writes={R_BEGIN, R_END, R_REWARD}, group="周常",
      times_max=MAX_TIMES, choices=Choices(catalog, "层组", limit=1), auto=auto_times, reserve=2)  # 次数留给幻兽法宝养成周
def run(ctx: TaskContext) -> TaskResult:
    times = dungeon.read_times(ctx, ID, MAX_TIMES)
    if isinstance(times, TaskResult):
        return times
    if not ctx.options.get("levels"):
        return TaskResult(ID, SKIPPED, "没有选择层组")
    if times == 0:
        return TaskResult(ID, SKIPPED, "次数留空，不执行")
    known = {layer for layer, _ in catalog()}
    wanted = [state.num(v) for v in ctx.options["levels"]]
    if any(w not in known for w in wanted):
        return TaskResult(ID, BLOCKED, "所选层组不在镇妖塔配置里，未执行")
    gains, groups, floors, why, dry = dungeon.Gains(), 0, 0, None, False
    prior = dungeon.resume_done(ctx)  # 暂停前每个层组已打的组数
    ctx.progress.update(prior)
    if all(prior.get(str(layer), 0) >= times for layer in wanted):
        return TaskResult(ID, SKIPPED, "断点前已用完设置的次数，不再挑战")
    for layer in dict.fromkeys(wanted):
        for _ in range(times - prior.get(str(layer), 0)):
            try:
                n = run_group(ctx, layer, gains, ctx.progress.get(str(layer), 0) + 1)
            except Halt as exc:
                why = str(exc)
                if exc.spent:  # 次数已扣：这一组没打完也算一组，继续时不再补
                    ctx.progress[str(layer)] = ctx.progress.get(str(layer), 0) + 1
                break
            if not n:
                dry = True
                break  # 免费次数用完
            groups, floors = groups + 1, floors + n
            ctx.progress[str(layer)] = ctx.progress.get(str(layer), 0) + 1
        if why or dry:
            break
    note = f"完成 {groups} 组共 {floors} 个小层"
    if dry and groups:
        note += f"（设置 {times * len(set(wanted))} 组，服务器免费次数已用完）"
    note += dungeon.gain_note(gains)
    if why:
        return TaskResult(ID, BLOCKED, f"{note}；{why}" if groups else why)
    if not groups:
        return TaskResult(ID, SKIPPED, "没有可用的免费次数")
    return TaskResult(ID, SUBMITTED, note)
