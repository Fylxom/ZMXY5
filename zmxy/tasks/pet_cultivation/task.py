"""刷益灵花：按 VIP 分档培养宠物，以确认到账的益灵花计次。流程在 harvest.py，这里是任务声明和读状态的小工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ... import api, data, dungeon, quota, state
from ...core import Choices, Pick, TaskContext, TaskResult, task

ID = "pet_cultivation"

R_EQUIP = "scene.characterHandler.putOnEquip"
R_TAKEOFF = "scene.characterHandler.takeOffEquip"
R_REMOVE = "scene.petHandler.removePassivitySkill"

CAPTURE_PET = 10001  # 默认天马；可指定灵花，种类与断点绑定
FLOWER = 101704003  # 益灵花
FORGET_FLOWER = 101704002  # 去灵花
EXCLUDED_MAPS = ("蓬丘", "七星殿")


def _pet_walk(value: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(value, Mapping):
        if (isinstance(value.get("entityId"), str) and state.num(value.get("id")) is not None
                and "hpQualification" in value):
            out.append(dict(value))
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return
    for child in children:
        if isinstance(child, (Mapping, list)):
            _pet_walk(child, out, depth + 1)


def pets_in(value: Any) -> list[dict[str, Any]]:
    """进角信息/刷新结果里的完整宠物实体，不猜字段路径；同一 entityId 留最后出现的。"""
    raw: list[dict[str, Any]] = []
    _pet_walk(value, raw)
    return list({pet["entityId"]: pet for pet in raw}.values())


def find_entity(value: Any, entity_id: str) -> dict[str, Any] | None:
    return next((pet for pet in pets_in(value) if pet.get("entityId") == entity_id), None)


def growth_probability(pet: Mapping[str, Any]) -> float | None:
    """PetSkillStudy 的升级领悟概率。"""
    try:
        conf = data.row("Pet", state.num(pet.get("id")) or 0) or {}
        study = state.num(conf.get("petSkillStudyId"))
        row = data.row("PetSkillStudy", study) if study is not None else None
        return float(row.get("levelUpPetPerksProbability")) if row else None
    except (data.DataError, TypeError, ValueError):
        return None


def map_catalog() -> list[tuple[int, str]]:
    return [(mid, name) for mid, name in dungeon.catalog(dungeon.MAIN)
            if mid != 80 and name.split()[0] not in EXCLUDED_MAPS]


def latest_level(info):
    """最新已解锁的主线关卡；解锁状态读不准就停，不猜前置通关。"""
    for mid, _ in sorted(map_catalog(), reverse=True):
        why = quota.locked(info, mid)
        if why:
            if "未通关" in why or why.startswith("角色等级不足"):
                continue
            raise dungeon.Stop(f"无法确认培养主线解锁状态：{why}")
        lv = dungeon.level(mid)
        if lv is None or lv.kind != dungeon.MAIN or lv.name in EXCLUDED_MAPS:
            raise dungeon.Stop("自动培养主线配置异常")
        remaining = quota.of(info, mid).left
        if remaining is None:
            raise dungeon.Stop("自动培养主线次数状态未知")
        if remaining > 0:
            return lv
    raise dungeon.Stop("没有已解锁且可培养的主线关卡（已排除蓬丘、七星殿）")


def player_level(info: Mapping[str, Any], slot: int) -> int | None:
    player = info.get("p1" if slot == 0 else "p2")
    return state.num(player.get("level")) if isinstance(player, Mapping) else None


def equipped_pets(info: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]] | None:
    """1P/2P 出战宠物 [(0/1, 宠物)]；结构不完整返回 None，不能据此放生。"""
    found: list[tuple[int, Mapping[str, Any]]] = []
    for index, key in enumerate(("p1", "p2")):
        player = info.get(key)
        if index == 1 and player is None:
            continue
        equipment = player.get("equipments") if isinstance(player, Mapping) else None
        if not isinstance(equipment, Mapping):
            return None
        pet = equipment.get("pet")
        if pet is None:
            continue
        if not isinstance(pet, Mapping) or not isinstance(pet.get("entityId"), str):
            return None
        found.append((index, pet))
    return found


def pet_return_count(info: Mapping[str, Any], level: int) -> int | None:
    """本 VIP 下遗忘该等级潜能返还的益灵花数。"""
    vip = state.vip_level(info)
    try:
        row = data.row("VIP", vip) if vip is not None else None
    except data.DataError:
        row = None
    return state.by_vip(row.get("petPerksReturnCount"), level) if isinstance(row, Mapping) else None


def _choice() -> Choices:
    return Choices(
        None, button="设置",
        picks=(Pick("harvest_times", "刷取次数（每朵益灵花记1次）", lambda _a: [],
                    number=(0, 1000000), integer=True),),
        toggles=(("auto_release", "自动放生（回收后）"),
                 ("cleanup_rejected", "落选清理")),
        toggle_defaults=(("auto_release", False), ("cleanup_rejected", False)),
        note="目标潜能：VIP3 及以上 3 级，以下 4 级。\n"
             "3 级：实测约 150 只、2.8 分钟一朵（双宠，2026-11-08）；理论约 120 只、2.3 分钟。\n"
             "4 级：理论约 1500 只、35 分钟一朵（按 3 级实测推算，未实测）。\n"
             "只数 = 成功捕获数（含初筛、培养落选，不含没捕到的），均值不保底。\n"
             "自动放生（回收后）：结出益灵花回收后的放掉；落选清理：没达到目标潜能的放掉。只放本轮新捕的，关着就留在背包。",
    )


@task(ID, "刷益灵花", writes={dungeon.R_CREATE, dungeon.R_FINISH, dungeon.R_PICKUP,
       api.R_PET_CAPTURE, api.R_PET_FREE, api.R_SHOP_BUY, R_EQUIP, R_TAKEOFF, R_REMOVE,
       api.R_WAREHOUSE_TO_BAG, api.R_BAG_TO_WAREHOUSE},
      spends={api.R_SHOP_BUY}, group="周常", choices=_choice(), overnight=True)  # 主线不限次数：跨 0 点照常刷
def run(ctx: TaskContext) -> TaskResult:
    from . import harvest  # harvest 要用本模块的工具，这里才导入
    return harvest.run(ctx)
