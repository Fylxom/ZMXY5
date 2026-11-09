"""捕捉宠物的表格数据：副本对应哪种宠物，各星级的资质、天赋、战力范围和战力公式。"""

from __future__ import annotations

from itertools import combinations

from .. import data, state

STAR_ROWS = (("hp", "hpQualification", "生命资质"), ("rehp", "reHpQualification", "回血资质"),
             ("atk", "atkQualification", "攻击资质"), ("def", "defQualification", "防御资质"))  # 1 星 / 2 星各一行
TALENT_ROWS = (("tz", "physical", "体质"), ("gg", "bone", "根骨"), ("ql", "capacity", "潜力"), ("fp", "fightPower", "战力"))  # 同样 1 星 / 2 星各一行
ALL_ROWS = STAR_ROWS + TALENT_ROWS  # 每一项都是范围：下限 hp / 上限 hp_max
STARS = (1, 2)


@data.cached
def target(mid: int) -> tuple[str, int, tuple[int, ...]] | None:
    """副本 mid → (宠物名, petId, 葫芦编号按使用顺序)。数据对不上返回 None。"""
    row = data.row("Dungeon", mid) or {}
    for part in str(row.get("monsterID", "")).split(";"):
        monster = data.slim("Monster", "canCatch", "petId").get(part)  # 怪物表 10MB，只要这两列
        if not monster or monster.get("canCatch") != 1:
            continue
        pet_id = state.num(monster.get("petId"))
        catch = data.row("PetCatch", pet_id) if pet_id else None
        pet = data.row("Pet", (catch or {}).get("catchTargetId"))  # 怪物身上的 petId 是 PetCatch 的键；宠物名在捕到后的宠物行里
        items = tuple(int(x) for x in str((catch or {}).get("catchPropId", "")).split(";") if x.isdigit())[:1]  # 只用第一种葫芦（木灵/灵金/青篁），不动火灵、紫金
        if pet_id and items and pet:
            return str(pet.get("name")), pet_id, items
    return None


def _star_rows(mid: int) -> list[tuple[int, dict]]:
    """这个副本能捕到的 [(星级, Pet 表的行)]。1 星 = PetCatch.catchTargetId，2 星 = UpQualityTargetId（捕捉时有概率直接升到 2 星）；星级对不上的不给。"""
    t = target(mid)
    catch = data.row("PetCatch", t[1]) if t else None
    rows = []
    for star, col in zip(STARS, ("catchTargetId", "UpQualityTargetId")):
        row = data.row("Pet", (catch or {}).get(col)) or {}
        if state.num(row.get("quality")) == star:
            rows.append((star, row))
    return rows


@data.cached
def ranges(mid: int) -> dict[int, dict[str, tuple[int, int]]]:
    """这种宠物各星级的资质、体质根骨潜力、战力会在哪个范围里波动 {星级: {hp/rehp/atk/def/tz/gg/ql/fp: (最小, 最大)}}，读 Pet 表的 ``hpQualification`` 等（形如 "340;550"）。
    1 星 = PetCatch.catchTargetId，2 星 = UpQualityTargetId（捕捉时有概率直接升到 2 星）。对不上的不给。"""
    out: dict[int, dict[str, tuple[int, int]]] = {}
    for star, row in _star_rows(mid):
        spans = {}
        for key, field, _name in ALL_ROWS:
            parts = str(row.get(field, "")).split(";")
            if len(parts) == 2 and all(x.isdigit() for x in parts):
                spans[key] = (int(parts[0]), int(parts[1]))
        if star in (powers := power_range(mid)):  # 战力不在表里，按公式算刚捕到时的最低 / 最高
            spans["fp"] = powers[star]
        out[star] = spans
    return out


POWER_KEYS = ("hpMax", "atk", "phyDef", "reHP", "hit", "dodge", "critical", "toughness", "lucky", "guardian")
BY_CHARACTER = ("atk", "phyDef", "critical", "dodge")  # 性格乘在这四项上（PetCharacter 表，±10%）
QUALITIES = (("hpMax", "hpQualification"), ("atk", "atkQualification"), ("phyDef", "defQualification"), ("reHP", "reHpQualification"))
FIRST_PERKS = 2  # 刚捕到最多带几个潜能（PetSkillStudy.firstPetPerksProbability 有两项；真机样本里 1 级宠物最多见到 2 个）


def power(row, quals, character, bone: int, perks=(), level: int = 1) -> int:
    """宠物战力（2026-10-06 用三个存档 26 只没穿装备的宠物核对过，全部对上）。

    属性 = ⌊Pet 表基础值 × 资质 ÷ 资质上限⌋ + ⌊资质 × Common.xxQualificationIndex ×（等级 − 1）⌋ + ⌊潜能⌋，三段各自向下取整；
    潜能 = PetPerks 的 attributeV1 + boneValue × 根骨。战力 = Σ 属性 × PowerAttribute 第 2 行的权重（攻击/防御/暴击/闪避先乘性格），
    四舍五入后再加元素抗性潜能的 resistLevPower + resistLevPowerIndex × 根骨。技能、体质、潜力不算。
    row：Pet 表的行；quals：{hpMax/atk/phyDef/reHP: 资质}；character：PetCharacter 表的行；perks：PetPerks 表的行。"""
    weights = data.row("PowerAttribute", 2)
    index = {k: v for r in state.table_rows("Common") for k, v in r.items() if k.endswith("QualificationIndex")}
    stats = dict.fromkeys(POWER_KEYS, 0)
    for stat, col in QUALITIES:
        top = int(str(row[col]).split(";")[1])
        stats[stat] = int(row[stat] * quals[stat] // top) + int(quals[stat] * float(index[f"{col[:-13]}QualificationIndex"]) * (level - 1))
    resist = 0
    for pk in perks:
        kind, lv = pk["attributeT1"], int(pk.get("level") or 1)
        if kind in stats:
            stats[kind] += int(pk["attributeV1"] + pk["boneValue"] * bone)
        else:
            resist += int(str(weights["resistLevPower"]).split(";")[lv - 1]) + int(str(weights["resistLevPowerIndex"]).split(";")[lv - 1]) * bone
    total = sum(stats[k] * (1 + (character.get(k, 0) if k in BY_CHARACTER else 0)) * weights[k] for k in POWER_KEYS)
    return int(total + 0.5) + resist


@data.cached
def power_range(mid: int) -> dict[int, tuple[int, int]]:
    """这种宠物刚捕到时战力的 {星级: (最低, 最高)}：最低 = 资质全是最小值、最差的性格、没有潜能；
    最高 = 资质全是最大值、根骨最高、最好的性格配上最好的 FIRST_PERKS 个不同潜能（穷举）。"""
    chars = {r["id"]: r for r in state.table_rows("PetCharacter")}
    perks = [r for r in state.table_rows("PetPerks") if r.get("level") == 1]
    out: dict[int, tuple[int, int]] = {}
    for star, row in _star_rows(mid):
        span = {stat: [int(x) for x in str(row[c]).split(";")] for stat, c in QUALITIES}
        first, last = (int(x) for x in str(row["character"]).split(";"))
        mine = [chars[i] for i in range(first, last + 1) if i in chars]
        bone = int(str(row["bone"]).split(";")[1])
        low = min(power(row, {s: v[0] for s, v in span.items()}, c, 1) for c in mine)
        top = {s: v[1] for s, v in span.items()}
        high = max(power(row, top, c, bone, pair) for c in mine for pair in combinations(perks, FIRST_PERKS))
        out[star] = (low, high)
    return out
