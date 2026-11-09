"""自动洗练（日常页，点“选择”进去分三块）：法宝、羽毛、阵法（精魄），各洗各的，洗到达标或今天的免费次数用完为止。
阵法暂未开放（2026-10-12）：设置页只显示提示，旧配置也不执行；以下精魄协议和实现保留供后续开放。
每次执行前先重新读一次角色状态（``ctx.refresh``），不用登录时的快照；读不到就停止，什么都不发。

**只用每天的免费洗炼次数，不耗材料**（2026-10-08 起；下面各段里“耗羽枝 / 魄心 / 洗炼石”的说法是材料那一档的协议，留作对照）：
今天还剩几次 = VIP 表的每日次数 − ``everyDaySetting`` 里今天已用的（``free_left``；羽毛 ``featherResetNum``：VIP 0–5 每天 1 次、6 起 3 次；
精魄 ``formationResetNum``：VIP 7 起 1 次），读不到就不洗。**羽毛每天自动把免费次数用完**（2026-10-11 起，设置里没有次数框，开着就洗到达标或免费次数用完）；
法宝也一样（2026-10-11 起）；阵法的次数上限和协议实现保留，当前不执行。
羽毛免费走另一条路由 ``wingHandler.featherVIPRest {entityId, lock, index:0}``（``WingProxy.featherRest``）：响应带 ``needReplace`` 才接着发
``featherSureRest`` 选留新 / 留旧（VIP 表 ``isFeatherResetKeep``，只有 VIP 8）；否则**洗出来直接覆盖**，所以这时达标的属性不管“自动锁定”开没开都锁上，
锁着的洗完变了就立刻停。精魄免费还是 ``formationFlagRest``，``index`` 发 2（``ForWashView.param``），照样先试再选。
**法宝**（2026-10-11 起洗）：免费洗炼 ``magicWeaponHandler.tryResetAttrFreeVIP {entityId, attr:3}``（``MagicWeaponProxy.resetVip``，每天 ``amuletResetNum`` 次：VIP 4 起 1 次、5 起 2 次，
今天用了几次在 ``everyDaySetting.amuletResetCnt``）把**成长值和祝福一起重洗、洗出来直接覆盖**，不能只洗一半、不能保留旧的 → 只有“洗到满足条件就停”这一种控制。
设置里先选角色和一件法宝（``options["fabao"]["pick"] = {"entity": entityId, "id": 当前编号}``，以 ``entity`` 识别实体，阶数变化后的 ``id`` 仅作当前类型信息；装备着的、背包里的都行，清单由“读取”/ 任务写进 profile 的 ``fabaos``），
成长值筛选（``growth_on`` + ``growth``：成长值 ≥ 多少）和祝福筛选（``wish_on`` + ``wish_num``：目标祝福数，``wish``：候选祝福）按法宝实体分别保存在 ``per_pick`` 里；祝福候选可以多于目标数，洗到候选中命中至少 ``wish_num`` 个就停。
开着的条件都满足 = 已达标，不洗；没满足就洗（**已经满足的那一项也会一起被洗掉**）。条件到不了（成长超上限、祝福个数超这个品质的上限）不洗。
**羽毛、精魄每洗完一次都重新读一次角色状态**，下一次按读回来的属性算达没达标、锁哪几条；羽毛达标的一律锁上再洗。
法宝满足已开启的筛选条件后就标红（不要求还有免费次数）；羽毛、精魄已经都达标或属性全锁着且免费次数还有剩时也标红。结果写进存档 profile 的 ``IDLE``，提醒去换目标；
在设置里点“完成”就撤掉，下次执行再判断。

精魄只洗**身上的**（进角信息里不在背包 ``bag`` 下面的）：阵眼里装着的精魄。
羽毛一次只洗一根：设置里选中哪只翅膀上的哪一根、给它设了条件，就洗那一根（装备着的、背包里的翅膀都行，游戏里也是进哪只翅膀洗哪根的）；别的都不碰。
**只用免费次数，不用材料、不买、不碰点券**。

法宝耗洗炼石的那一档（对照 ``MagicAttributeMediator`` / ``MagicTwoSureMediator``，不用，留作对照）：
``tryResetAttr {entityId, attr, type:1, itemId}`` → ``newMagicWeapon`` → ``confirmResetAttr {confirm: 1 留新 / 0 留旧}``；``attr`` 1 = 成长、2 = 祝福。
祝福 ``fiveElements`` 支持 "1,3" 这样的字符串或编号数组（白虎/青龙/玄武/朱雀/黄麟），个数上限 ``MagicweaponEments.num``（按品质）；成长范围 ``Magicweapon.growthRate``。

羽毛、精魄（对照 ``WashFeatherView`` / ``ForWashView`` / ``BasicViewWash``）：
羽毛 ``wingHandler.featherRest {entityId, lock, index:1}`` → ``feather`` → ``featherSureRest {entityId, replace}``，耗羽枝；
精魄 ``formationHandler.formationFlagRest {entityId, lock, index:0}`` → ``formationFlag`` → ``formationFlagSureRest {entityId, replace}``，耗魄心。
一次耗 ``washNum × (1 + 锁定的条数)``；不能把属性全锁上。
精魄的筛选是每个属性一个百分数：这条属性至少洗到上限（表里该物品这一项范围的上限）的百分之几，留空 = 不要这条。
**羽毛的筛选直接填数值**（和游戏里看到的一样）：这条属性至少洗到多少，留空 = 不要这条；填的数超过这种羽毛这一项的上限 → 这种羽毛不洗（到不了）。
游戏里属性值按大小标颜色（``QualityColor`` 表，只看属性和数值，和哪种羽毛无关）：白 / 绿 / 蓝 / 紫 / 黄 / 红 / 橙，
``bands`` 给出某种羽毛某条属性的范围和范围内各颜色从哪个数起，设置界面标在输入框旁边。
**羽毛按翅膀、再按种类分开设**（雪鸟 / 蓝凫 / 紫燕 / 金鹏 / 彤鹤 / 赤乌之羽，品质不同、条数和范围都不同）：
``options["feather"]["wings"][翅膀编号]["kinds"][羽毛编号] = {"attrs": {...}}``，这只翅膀上是哪种羽毛就用哪种的条件，没设条件的那种不洗。
**一次只洗一根**：设置里选中的那一根，``options["feather"]["pick"] = {"wing": 翅膀编号, "pos": 孔位, "id": 羽毛编号}``（``feather_picked``），别的都不洗；条件仍按翅膀 + 种类存。
这个存档有哪些翅膀、每只上面镶着哪些羽毛（哪种、现在各条属性多少），由设置里的“读取”或任务运行时写进存档 profile 的 ``wings``（``feather_profile``），
界面先选翅膀，再照游戏的洗炼界面把它上面的羽毛一根根列出来，点哪根就设它那一种的条件：
先在 12 种属性里点选要的（最多这种羽毛的词条数 ``feather_slots`` 那么多），选满了才列出这几种的数值框；数值框一选上就填好默认值 = 这种羽毛自己的品质色从几起（``top_band``，赤乌 = 橙色起），有这条就行填 1。
羽毛 12 种属性都可能出，条数有限：要的属性里达标的条数 = min(要的种数, 这根羽毛的条数) 就算洗好；
精魄只算它出得了的那几种（``FormationFlag.attributes``）。达标的条数更多（一样多就看离目标更近）才留新的。
“锁定已达标的属性”开着时，达标的那几条锁上再洗（贵，但洗好的不会丢）。
羽毛还能**手动锁定**（和游戏里点属性旁边的小锁一样，按“翅膀 + 孔位”记，``feather_locks``）：锁着的每次洗都带上、永远不动；
要的属性只能出在没锁的那几条上，目标条数跟着减；一根的属性全锁着就不洗。

读不到材料数量、响应里读不到新属性 → 停下不猜（已经试出来的那次按留旧的发掉）。**按客户端脚本推出，没在真机上验证**，第一次跑请看日志。

**宁可不洗，也不洗错**（洗错一个的成本很高）：拿不准就不发。``LIVE`` 是总开关，2026-10-11 用户说关掉保护后是 True（真洗）；
改回 False 就**一个洗练请求都不发**，只读进角信息，把每件东西现在的属性、达没达标、本来要怎么洗（锁哪几条、一次耗多少）
写进结果里，用来核对识别得对不对。
羽毛 / 精魄留新的之前还要过一道：锁着的那几条必须原样还在，否则一律留旧的。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .. import protocol as p
from .. import state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task
from .rewards import with_idle_error

ID = "refine"
R_FABAO_FREE = "scene.magicWeaponHandler.tryResetAttrFreeVIP"  # 法宝的免费洗炼：成长值和祝福一起重洗，洗出来直接覆盖（MagicWeaponProxy.resetVip）
R_FEATHER, R_FEATHER_SURE = "scene.wingHandler.featherRest", "scene.wingHandler.featherSureRest"
R_FLAG, R_FLAG_SURE = "scene.formationHandler.formationFlagRest", "scene.formationHandler.formationFlagSureRest"
ATTRS = (("hpMax", "生命"), ("mpMax", "魔法"), ("atk", "攻击"), ("phyDef", "防御"), ("hit", "命中"), ("dodge", "闪避"),
         ("critical", "暴击"), ("toughness", "韧性"), ("lucky", "幸运"), ("guardian", "守护"), ("reHP", "回血"), ("reMP", "回魔"))
ATTR_NAMES = dict(ATTRS)
BEASTS = ((1, "白虎"), (2, "青龙"), (3, "玄武"), (4, "朱雀"), (5, "黄麟"))  # 祝福编号（ItemVo.fiveElementNames）
MODULES = (("fabao", "法宝"), ("feather", "羽毛"), ("formation", "阵法"))
UNAVAILABLE = ("formation",)  # 暂未开放：界面只显示提示，旧配置不执行
COLORS = ("白", "绿", "蓝", "紫", "黄", "红", "橙")  # 属性值的颜色，从低到高（PropColor.getColorByQuality：< v1 白，≥ v6 橙）
TIMES_MAX = 9999  # 每块一次最多洗几次
UNCAPPED = ("fabao", "feather")  # 设置里没有“最多洗几次”的：开着就每天把免费次数用完
LIVE = True  # False = 不真洗：所有洗练请求都在 _try 这一处拦下，只报告本来要怎么洗。用户 2026-10-11 说关掉保护，改成 True
GAP = 0.6  # 两次洗练之间隔多久：客户端点太快会提示“请勿点击过快”
R_FEATHER_FREE = "scene.wingHandler.featherVIPRest"  # 羽毛的免费洗炼是另一条路由（WingProxy.featherRest：index 0）
FEATHER_INDEX, FLAG_INDEX = 0, 2  # 免费次数的那一档（羽毛 0；精魄界面第一个页签发 2。另外两档是耗材料、点券，不用）
# 每块：(VIP 表里每天几次, everyDaySetting 里今天用了几次)。法宝两边名字不一样（VipModelManager.magicFreeResetCount）
FREE_FIELD = {"fabao": ("amuletResetNum", "amuletResetCnt"), "feather": ("featherResetNum", "featherResetNum"),
              "formation": ("formationResetNum", "formationResetNum")}
ALL_LOCKED = "属性全锁着，没法洗"  # 手动锁的 + 达标的把这件的属性占满了
IDLE = "refine_idle"  # 存档 profile：法宝已达标，或其他块达标 / 全锁且还有免费次数；界面把任务名标红，没有就是 ""


def free_left(info: Any, key: str) -> int | None:
    """这一块今天还剩几次免费洗炼 = VIP 表的每日次数 − everyDaySetting 里今天已用的；读不到 VIP 等级 / 表 / 今日计数 → None。"""
    level, daily = state.vip_level(info), state.daily(info)
    vip = state.row("VIP", level) if level is not None else None
    cap = state.num(vip.get(FREE_FIELD[key][0])) if vip else None
    used = state.num(daily.get(FREE_FIELD[key][1], 0)) if daily is not None else None
    return None if cap is None or used is None else max(cap - used, 0)


def _f(v: Any) -> float | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def owned(info: Any, table: str) -> list[Mapping[str, Any]]:
    """身上的（不在背包下面的）、编号在 table 里的物品实体，按 entityId 去重。"""
    rows, found, seen = state.table(table), [], set()

    def walk(value: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            if value.get("entityId") and str(value.get("id")) in rows and value["entityId"] not in seen:
                seen.add(value["entityId"])
                found.append(value)
            for k, child in value.items():
                if not (depth == 0 and k == "bag"):
                    walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(info, 0)
    return found


def _entity(response: Any, key: str) -> Mapping[str, Any] | None:
    """响应里的新物品（有的包在 data 里）。"""
    got = response.get(key) if isinstance(response, Mapping) else None
    if isinstance(got, Mapping) and isinstance(got.get("data"), Mapping):
        got = got["data"]
    return got if isinstance(got, Mapping) else None


def _name(table: str, item: Mapping[str, Any]) -> str:
    return str((state.row(table, item.get("id")) or {}).get("name") or item.get("id"))


class _Budget:
    """一块的次数：设置的次数或今天的免费次数用完就不再洗。"""

    def __init__(self, ctx: TaskContext, times: int, free: int) -> None:
        self.ctx, self.left, self.done = ctx, min(times, free), 0
        self.free, self.first, self.times = free, True, times
        self.last: Mapping[str, Any] = {}  # 上一次洗炼的整个响应

    def pay(self) -> None:
        self.left -= 1
        self.done += 1

    def note(self, text: str) -> None:
        """刚洗的这一次的结果：当场写一行日志（“第几次 哪一件：原来 → 新的，替换 / 保留”）。最后的任务结果里不再重复。"""
        self.ctx.log(f"第 {self.done} 次 {text}")

    def pause(self) -> bool:
        """洗下一次之前等一下；被停止返回 False。"""
        if self.first:
            self.first = False
            return not self.ctx.stopped
        return self.ctx.sleep(GAP)


class _Stop(Exception):
    """这一块到此为止（次数用完、服务器拒绝、读不到结果）。"""


class _UserStop(_Stop):
    """用户主动停止；与连接中断或洗练失败分开处理。"""


class _Out(_Stop):
    """次数用完：正常收尾，不算被拦下。"""


class _Dry(Exception):
    """LIVE 关着：这一件本来要洗，没发请求；接着看下一件。"""


def _try(ctx: TaskContext, budget: _Budget, route: str, body: dict, sure: str, keep_old: dict, key: str,
         what: str, choose: Callable[[Mapping[str, Any]], bool] = lambda r: True) -> Mapping[str, Any]:
    """用免费次数试一次，返回新物品；读不到就按留旧的发掉再停。
    choose(响应) → 这次洗出来的要不要再选留新 / 留旧（羽毛的免费洗炼 VIP 不够时直接覆盖，没有这一步）。"""
    if budget.left <= 0:
        raise _Out("免费次数用完" if budget.done >= budget.free else f"设置的次数用完（最多洗 {budget.times} 次）")
    lock = body.get("lock")
    locked = f"，锁定 {'、'.join(ATTR_NAMES[a] for a in lock)}" if lock else ""
    if not LIVE:
        raise _Dry(f"要洗，用免费次数（今天还剩 {budget.free - budget.done} 次）{locked}")
    if not budget.pause():
        if ctx.stopped:
            raise _UserStop("已被用户停止")
        raise _Stop(with_idle_error(ctx, "洗练间隔等待时连接中断"))
    try:
        response = ctx.request(route, body)
    except p.AllianceError as exc:
        raise _Stop(f"{what}洗练被拒：{exc}") from exc
    budget.pay()
    budget.last = response if isinstance(response, Mapping) else {}
    new = _entity(response, key)
    if new is None:
        if choose(budget.last):
            _sure(ctx, sure, keep_old)
            raise _Stop(f"{what}洗练的响应里读不到新属性，已保留原属性")
        raise _Stop(f"{what}洗练的响应里读不到新属性，请到游戏里核对")
    return new


def _sure(ctx: TaskContext, route: str, body: dict) -> None:
    try:
        ctx.request(route, body)
    except p.AllianceError as exc:
        raise _Stop(f"选择保留 / 替换被拒：{exc}") from exc


# ---------------------------------------------------------------- 法宝

def _beasts(item: Mapping[str, Any]) -> set[int] | None:
    raw = item.get("fiveElements")
    if isinstance(raw, str):
        values = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, (list, tuple)):
        values = raw
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        values = [raw]  # AS3 的 String 属性也会把单个数字转换成字符串
    else:
        return None
    out = set()
    for value in values:
        if isinstance(value, float) and not value.is_integer():
            return None
        beast = state.num(value)
        if beast not in dict(BEASTS):
            return None
        out.add(beast)
    return out


def fabao_items(info: Any) -> list[tuple[bool, int | None, Mapping[str, Any]]]:
    """每件法宝的 (是否装备, 角色 index：0=1P / 1=2P / None=未知, 实体)。装备按 p1/p2，背包按物品 index；同一实体优先取装备里的。"""
    rows, found = state.table("Magicweapon"), {}

    def walk(value: Any, depth: int, bagged: bool, owner: int | None = None) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            if value.get("entityId") and str(value.get("id")) in rows:
                index = owner if not bagged and owner in (0, 1) else state.num(value.get("index"))
                index = index if index in (0, 1) else None
                worn = not bagged
                old = found.get(value["entityId"])
                if old is None or (worn and not old[0]) or (worn == old[0] and old[1] is None and index is not None):
                    found[value["entityId"]] = (worn, index, value)
            for k, child in value.items():
                if depth == 0 and k == "players" and isinstance(child, list):
                    for i, role in enumerate(child):
                        walk(role, depth + 2, False, i if i in (0, 1) else None)
                else:
                    index = (0 if k == "p1" else 1) if depth == 0 and k in ("p1", "p2") else owner
                    walk(child, depth + 1, bagged or (depth == 0 and k == "bag"), index)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1, bagged, owner)

    walk(info, 0, False)
    return sorted(found.values(), key=lambda x: not x[0])


def fabao_profile(info: Any) -> dict[str, list[dict[str, Any]]]:
    """法宝清单带角色 index（0=1P / 1=2P / None=未知），按稳定 entity 区分同名法宝，id 是当前阶数。"""
    return {"fabaos": [{"entity": it["entityId"], "id": state.num(it.get("id")), "name": _name("Magicweapon", it), "worn": worn, "index": index,
                        "growth": _f(it.get("growthRate")), "wish": sorted(beasts) if (beasts := _beasts(it)) is not None else None}
                       for worn, index, it in fabao_items(info)]}


def fabao_caps(kind: Any) -> tuple[float, float, int] | None:
    """这种法宝的 (成长下限, 成长上限, 最多几个祝福)；表对不上返回 None。"""
    row = state.row("Magicweapon", kind) or {}
    lo, _, hi = str(row.get("growthRate", "")).partition(";")
    cap = state.num((state.row("MagicweaponEments", row.get("quality")) or {}).get("num"))
    return None if _f(lo) is None or _f(hi) is None or not cap else (_f(lo), _f(hi), cap)  # type: ignore[return-value]


def fabao_need(opt: Mapping[str, Any]) -> tuple[float | None, int, set[int]]:
    """设置里的筛选条件：(成长值至少多少 / None = 没开, 目标命中数 / 0 = 没开, 候选祝福编号)。"""
    growth = _f(opt.get("growth")) if opt.get("growth_on") else None
    count = max(state.num(opt.get("wish_num")) or 0, 0) if opt.get("wish_on") else 0
    return (growth if growth and growth > 0 else None), count, ({b for b in opt.get("wish") or () if b in dict(BEASTS)} if count else set())


def _fabao_pick_settings(opt: Mapping[str, Any], pick: Any) -> Mapping[str, Any] | None:
    """筛选配置绑定稳定的实体 ID；存档兼容进阶后仍保存的旧类型 ID。"""
    rows = opt.get("per_pick")
    if not isinstance(rows, list) or not isinstance(pick, Mapping):
        return None
    entity = pick.get("entity")
    if entity is None:
        return None
    # 若旧版曾按实体 + 阶数 ID 保存过多行，和设置窗口的迁移逻辑一致，取最后一条。
    return next((row for row in reversed(rows) if isinstance(row, Mapping) and
                 row.get("entity") is not None and str(row.get("entity")) == str(entity)), None)


def _wish_text(beasts: Any) -> str:
    return "、".join(n for b, n in BEASTS if b in beasts) or "无"


def _fabao_key(item: Mapping[str, Any]) -> tuple[Any, Any]:
    beasts = _beasts(item)
    return _f(item.get("growthRate")), None if beasts is None else frozenset(beasts)


def _fabao_show(item: Mapping[str, Any]) -> str:
    growth, beasts = _fabao_key(item)
    return f"成长{growth:g} 祝福{_wish_text(beasts)}" if growth is not None and beasts is not None else "读不到成长 / 祝福"


def run_fabao(ctx: TaskContext, opt: Mapping[str, Any], budget: _Budget, notes: list[str]) -> None:
    """只洗设置里选中的那一件，用免费次数：成长值和祝福**一起重洗、洗出来直接覆盖**，所以只有“洗到满足条件就停”这一种控制。
    开着的条件（成长值筛选 / 祝福筛选）都满足 → 已达标，不洗；否则洗一次、重新读一次，再看。"""
    pick = opt.get("pick")
    if isinstance(opt.get("per_pick"), list):
        settings = _fabao_pick_settings(opt, pick)
        if settings is None:
            return notes.append("选中的法宝还没有单独设置筛选条件")
        growth, count, wish = fabao_need(settings)
        hit_candidates = True
    else:
        growth, count, wish = fabao_need(opt)
        hit_candidates = False  # 兼容旧版：旧 wish 是必须出现的祝福
    if growth is None and not count:
        return notes.append("成长值筛选和祝福筛选都没开")
    if not isinstance(pick, Mapping):
        return notes.append("没有选要洗哪一件法宝")
    # 进阶会改变 Magicweapon.id，但 entityId 仍指向同一件法宝；只按实体找，随后以当前 id 读取范围。
    cur = next((it for _worn, _index, it in fabao_items(ctx.info)
                if str(it["entityId"]) == str(pick.get("entity"))), None)
    if cur is None:
        raise _Stop("选中的那件法宝没找到")
    name, eid, caps = _name("Magicweapon", cur), cur["entityId"], fabao_caps(cur.get("id"))
    if caps is None:
        return notes.append(f"{name} 表里读不到成长范围 / 祝福上限，不洗")
    if growth is not None and growth > caps[1]:
        return notes.append(f"{name} 成长上限 {caps[1]:g}，到不了 {growth:g}，不洗")
    if count > caps[2] or (hit_candidates and count > len(wish)) or (not hit_candidates and len(wish) > count):
        return notes.append(f"{name} 最多 {caps[2]} 个祝福，凑不齐设置里要的，不洗")
    while True:
        have, beasts = _fabao_key(cur)
        if have is None or beasts is None:
            raise _Stop(f"{name} 读不到成长 / 祝福，不洗")
        wish_ok = (len(beasts & wish) >= count) if hit_candidates else (len(beasts) >= count and wish <= beasts)
        if (growth is None or have >= growth) and (not count or wish_ok):
            return notes.append(f"{name} {_fabao_show(cur)} 已达标")
        try:
            new = _try(ctx, budget, R_FABAO_FREE, {"entityId": eid, "attr": 3}, "", {}, "newMagicWeapon", name, lambda r: False)
        except _Dry as dry:
            return notes.append(f"{name} {_fabao_show(cur)} {dry}")
        got, got_b = _fabao_key(new)
        if got is None or got_b is None:
            raise _Stop(f"{name} 洗练的响应里读不到新的成长 / 祝福，请到游戏里核对")
        changed = ([f"成长{have:g} → {got:g}"] if got != have else []) + ([f"祝福{_wish_text(beasts)} → {_wish_text(got_b)}"] if got_b != beasts else [])
        budget.note(f"{name}：{'，'.join(changed) or '属性没变'}，直接覆盖")
        cur = _reread(ctx, eid, name, new, _fabao_key, _fabao_show)


# ---------------------------------------------------------------- 羽毛 / 精魄

def wants(opt: Mapping[str, Any]) -> dict[str, float]:
    """设置里填了的属性 → 百分数（1–100）。"""
    out = {}
    for attr, _ in ATTRS:
        pct = _f((opt.get("attrs") or {}).get(attr))
        if pct is not None and 0 < pct <= 100:
            out[attr] = pct
    return out


def values(opt: Mapping[str, Any]) -> dict[str, float]:
    """羽毛：设置里填了的属性 → 至少要的数值。"""
    out = {}
    for attr, _ in ATTRS:
        v = _f((opt.get("attrs") or {}).get(attr))
        if v is not None and v > 0:
            out[attr] = v
    return out


def _color_steps(attr: str) -> list[int | None]:
    """QualityColor 表里这条属性各颜色的起点；读不到的是 None。"""
    row = state.row("QualityColor", attr)
    return [state.num(row.get(f"v{i}")) for i in range(1, 7)] if row else [None]


def color_of(attr: str, value: float) -> int | None:
    """这个数值在游戏里是第几种颜色（COLORS 的下标）；表里没有这条属性返回 None。"""
    steps = _color_steps(attr)
    return None if None in steps else sum(1 for s in steps if value >= s)


def top_band(kind: Any, attr: str) -> tuple[int, int] | None:
    """这种羽毛自己的品质色（它洗得到的最高一档）在这条属性上从哪个数起：(颜色下标, 数值)。"""
    got = bands(kind, attr)
    return got[2][-1] if got and got[2] else None


def bands(kind: Any, attr: str) -> tuple[int, int, list[tuple[int, int]]] | None:
    """某种羽毛的某条属性：(下限, 上限, [(颜色下标, 从哪个数起)…])，只列范围内出得了的颜色；表对不上返回 None。"""
    lo, _, hi = str((state.row("Feather", kind) or {}).get(attr, "")).partition(";")
    lo_n, hi_n, steps = state.num(lo), state.num(hi), _color_steps(attr)
    if lo_n is None or hi_n is None or hi_n <= 0 or None in steps:
        return None
    starts, out = [0, *steps], []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else None
        if start <= hi_n and (end is None or end > lo_n):
            out.append((i, max(start, lo_n)))
    return lo_n, hi_n, out


def _tops(row: Mapping[str, Any], attrs: Any) -> dict[str, float]:
    """这件物品出得了的属性 → 上限。"""
    out = {}
    for attr in attrs:
        top = _f(str(row.get(attr, "")).partition(";")[2])
        if top is not None and top > 0:
            out[attr] = top
    return out


def _score(item: Mapping[str, Any], need: Mapping[str, float]) -> tuple[int, float, list[str]]:
    """(达标条数, 离目标多近, 达标的属性)。"""
    hits, near, ok = 0, 0.0, []
    for attr, least in need.items():
        value = _f(item.get(attr)) or 0.0
        near += min(value / least, 1.0)
        if value >= least:
            hits += 1
            ok.append(attr)
    return hits, near, ok


def _show(item: Mapping[str, Any]) -> str:
    return " ".join(f"{name}{_f(item.get(a)):g}" for a, name in ATTRS if (_f(item.get(a)) or 0) > 0) or "无属性"


def _diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """只写变了的那几条：“韧性675 → 命中599”；没变的不写。"""
    changed = [a for a, _ in ATTRS if (_f(old.get(a)) or 0) != (_f(new.get(a)) or 0)]
    if not changed:
        return "属性没变"
    return f"{_show({a: old.get(a) for a in changed})} → {_show({a: new.get(a) for a in changed})}"


def run_attrs(ctx: TaskContext, opt: Mapping[str, Any], budget: _Budget, table: str, route: str, sure: str, key: str, index: int,
              possible: Callable[[Mapping[str, Any]], Any], empty: str, notes: list[str],
              need_of: Callable[[Mapping[str, Any], Mapping[str, float]], dict[str, float]],
              items: list[Mapping[str, Any]], where: Mapping[Any, str] | None = None,
              fixed: Mapping[Any, list[str]] | None = None,
              choose: Callable[[Mapping[str, Any]], bool] = lambda r: True, guard: bool = False) -> None:  # noqa: C901
    """用免费次数洗（不耗材料）。items：要看的物品。need_of(物品, 它各条属性的上限) → 这件每条属性至少要多少（羽毛按翅膀和种类填的数值，精魄按百分数折算）；空的 = 这件不洗。
    where：物品 entityId → 写在名字前面的出处（羽毛 = 哪只翅膀）。
    fixed：物品 entityId → 用户手动锁定的属性（和游戏里点小锁一样）：每次洗都带上，永远不动；它们占着的条数、够不够数都按现在的算。
    choose(响应) → 洗出来的要不要再选留新 / 留旧；不用选的是服务器已经直接换成新的了（羽毛免费洗炼、VIP 不够时）。
    guard：达标的属性不管“自动锁定”开没开都锁上（羽毛一律这样）。
    **每洗完一次都重新读一次角色状态**（``_reread``），下一次按读回来的属性判断达没达标、锁哪几条；读不到、和洗炼结果对不上就停。
    手动锁的 + 达标的会把这件的属性全锁上 → 没法洗（游戏不让全锁），写“全锁着”，不洗。"""
    seen = len(notes)
    if not items:
        return notes.append(empty)
    for item in items:
        name, row, eid = (where or {}).get(item["entityId"], "") + _name(table, item), state.row(table, item.get("id")) or {}, item["entityId"]
        if not row or row.get("isWash") == 0:
            continue  # 这种洗不了
        tops = _tops(row, possible(row))
        need = {a: v for a, v in need_of(item, tops).items() if a in tops}
        over = [f"{ATTR_NAMES[a]} {v:g}（上限 {tops[a]:g}）" for a, v in need.items() if v > tops[a]]
        if over:  # 到不了的条件：洗多少次都白洗，不洗
            if (note := f"{name} 到不了 {'、'.join(over)}，不洗") not in notes:
                notes.append(note)
            continue
        if not need or not any((_f(item.get(a)) or 0) > 0 for a, _ in ATTRS):
            continue
        cur = item
        while True:  # 每一轮都按 cur（开头是刚读的，之后是每次洗完重新读的）现算
            own = sum(1 for a, _ in ATTRS if (_f(cur.get(a)) or 0) > 0)
            pinned = [a for a in (fixed or {}).get(eid, ()) if (_f(cur.get(a)) or 0) > 0]  # 手动锁定的（只算这件现在有的）
            if len(pinned) >= own:
                notes.append(f"{name} {_show(cur)} {ALL_LOCKED}")
                break
            # 锁着的不会变：达标的就是达标，不达标的也洗不上去；其余要的属性只能出在没锁的那几条上
            goal = _score(cur, {a: v for a, v in need.items() if a in pinned})[0] + min(sum(1 for a in need if a not in pinned), own - len(pinned))
            hits, near, ok = _score(cur, need)
            if hits >= goal:
                notes.append(f"{name} {_show(cur)} 已达标")
                break
            lock = pinned + ([a for a in ok if a not in pinned] if opt.get("lock", True) or guard else [])
            if len(lock) >= own:  # 达标的都锁上就全锁了：游戏不让全锁，不洗
                notes.append(f"{name} {_show(cur)} {ALL_LOCKED}")
                break
            try:
                new = _try(ctx, budget, route, {"entityId": eid, "lock": lock, "index": index},
                           sure, {"entityId": eid, "replace": False}, key, name, choose)
            except _Dry as dry:
                notes.append(f"{name} {_show(cur)}（达标 {hits}/{goal}）{dry}")
                break
            held = all((_f(new.get(a)) or 0) >= (_f(cur.get(a)) or 0) for a in lock)  # 锁着的掉了 / 变低了就不要
            if choose(budget.last):
                keep = held and _score(new, need)[:2] > (hits, near)
                _sure(ctx, sure, {"entityId": eid, "replace": keep})
                budget.note(f"{name}：{_diff(cur, new)}，{'替换' if keep else '保留'}")
            else:  # 服务器已经直接换成新的了，没得选
                keep = True
                budget.note(f"{name}：{_diff(cur, new)}，直接覆盖")
                if not held:
                    raise _Stop(f"{name} 免费洗炼直接覆盖，锁定的 {'、'.join(ATTR_NAMES[a] for a in lock)} 没保住（现在 {_show(new)}），已停止，请到游戏里核对")
            cur = _reread(ctx, eid, name, new if keep else cur)
    if len(notes) == seen:
        notes.append("没有洗得了、又对得上所选属性的")


def _attr_key(item: Mapping[str, Any]) -> tuple[float, ...]:
    return tuple(_f(item.get(a)) or 0 for a, _ in ATTRS)


def _reread(ctx: TaskContext, eid: Any, name: str, expect: Mapping[str, Any],
            key: Callable[[Mapping[str, Any]], Any] = _attr_key, show: Callable[[Mapping[str, Any]], str] = _show) -> Mapping[str, Any]:
    """洗完一次后重新读角色状态，返回这件物品现在的样子；读不到、找不到、和刚才的洗炼结果（expect）对不上 → 停。
    key：拿哪些内容比（羽毛 / 精魄 = 12 条属性，法宝 = 成长值 + 祝福）；show：对不上时怎么写。"""
    try:
        info = ctx.refresh()
    except p.AllianceError as exc:
        raise _Stop(f"{name} 洗完后重新读取失败（{exc}），已停止") from exc
    found: list[Mapping[str, Any]] = []

    def walk(value: Any, depth: int) -> None:
        if depth > 10 or found:
            return
        if isinstance(value, Mapping):
            if value.get("entityId") == eid:
                return found.append(value)
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(info, 0)
    if not found:
        raise _Stop(f"{name} 洗完后重新读取，找不到这一件了，已停止，请到游戏里核对")
    if key(found[0]) != key(expect):
        raise _Stop(f"{name} 重新读到的属性（{show(found[0])}）和洗炼结果（{show(expect)}）对不上，已停止，请到游戏里核对")
    return found[0]


def feather_qualities() -> list[tuple[str, int]]:
    """羽毛种类 → 它的品质色（COLORS 的下标；羽毛名字和它洗得到的最高一档都是这个颜色）。"""
    return [(str(r["id"]), min(state.num(r.get("quality")) or 0, len(COLORS) - 1)) for r in state.table_rows("Feather")]


def feather_slots(kind: Any) -> int:
    """这种羽毛有几条属性（词条数）：雪鸟 1 … 赤乌 6。"""
    return state.num((state.row("Feather", kind) or {}).get("attributeNum")) or 0


def feather_kinds() -> list[tuple[str, str]]:
    """全部羽毛种类 [(编号, 名字)]，按品质从低到高。"""
    return [(str(r["id"]), str(r["name"])) for r in sorted(state.table_rows("Feather"), key=lambda r: state.num(r.get("quality")) or 0)]


def wing_name(wing: Mapping[str, Any]) -> str:
    row = state.row("Wing", f"{wing.get('id')}_{wing.get('level')}") or state.row("WingId", wing.get("id")) or {}
    return str(row.get("name") or wing.get("id"))


def wings(info: Any) -> list[dict[str, Any]]:
    """这个存档的每只翅膀和它上面镶着的羽毛：[{"id", "name", "worn": 是不是穿在身上, "feathers": [(孔位, 羽毛实体)…]}]。
    翅膀 = 带 ``inlay`` 的实体（穿着的在角色装备里，没穿的在背包的 wing 里）；没镶羽毛的不列。穿着的排前面。"""
    rows, out, seen = state.table("Feather"), [], set()

    def walk(value: Any, depth: int, bagged: bool) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            inlay = value.get("inlay")
            if isinstance(inlay, Mapping) and value.get("entityId") and value["entityId"] not in seen:
                feathers = [(str(f.get("pos", k)), f) for k, f in inlay.items()
                            if isinstance(f, Mapping) and f.get("entityId") and str(f.get("id")) in rows]
                if feathers:
                    seen.add(value["entityId"])
                    out.append({"id": state.num(value.get("id")), "name": wing_name(value), "worn": not bagged,
                                "feathers": sorted(feathers, key=lambda pf: pf[0])})
            for k, child in value.items():
                walk(child, depth + 1, bagged or (depth == 0 and k == "bag"))
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1, bagged)

    walk(info, 0, False)
    return sorted(out, key=lambda w: not w["worn"])


def feather_profile(info: Any) -> dict[str, list[dict[str, Any]]]:
    """写进存档 profile 的翅膀清单：{"wings": [{"id", "name", "worn", "feathers": [{"id": 羽毛编号, "pos": 孔位, "attrs": {属性: 现在的数值}}…]}…]}，
    设置里先选翅膀，再照它一根根列出羽毛（和游戏的洗炼界面一样）。"""
    return {"wings": [{"id": w["id"], "name": w["name"], "worn": w["worn"],
                       "feathers": [{"id": state.num(f.get("id")), "pos": pos,
                                     "attrs": {a: v for a, _ in ATTRS if (v := state.num(f.get(a))) and v > 0}} for pos, f in w["feathers"]]}
                      for w in wings(info)]}


def feather_locks(opt: Mapping[str, Any], wing: Any, pos: Any, kind: Any) -> list[str]:
    """用户手动锁定的属性：``opt["locks"][翅膀编号][孔位] = {"id": 羽毛编号, "attrs": [属性…]}``。
    孔里换了别的种类的羽毛（编号对不上）就不算，免得把锁带到另一根上。"""
    slot = ((opt.get("locks") or {}).get(str(wing)) or {}).get(str(pos))
    if not isinstance(slot, Mapping) or str(slot.get("id")) != str(kind):
        return []
    return [a for a, _ in ATTRS if a in (slot.get("attrs") or ())]


def feather_picked(opt: Mapping[str, Any], wing: Any, pos: Any, kind: Any) -> bool:
    """是不是设置里选中的那一根：``opt["pick"] = {"wing": 翅膀编号, "pos": 孔位, "id": 羽毛编号}``，一次只洗这一根。
    孔里换了别的种类的羽毛（编号对不上）就不算，免得洗到另一根上。"""
    pick = opt.get("pick")
    return isinstance(pick, Mapping) and (str(pick.get("wing")), str(pick.get("pos")), str(pick.get("id"))) == (str(wing), str(pos), str(kind))


def wing_wants(opt: Mapping[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    """设了条件的翅膀 → 羽毛种类 → 它要的属性和数值。"""
    out, saved = {}, opt.get("wings") if isinstance(opt.get("wings"), Mapping) else {}
    for wing, cfg in saved.items():
        kinds = cfg.get("kinds") if isinstance(cfg, Mapping) and isinstance(cfg.get("kinds"), Mapping) else {}
        if got := {str(k): w for k, v in kinds.items() if isinstance(v, Mapping) and (w := values(v))}:
            out[str(wing)] = got
    return out


def run_feather(ctx: TaskContext, opt: Mapping[str, Any], budget: _Budget, notes: list[str]) -> None:
    want = wing_wants(opt)
    if not want:
        return notes.append("哪只翅膀的羽毛都没设要洗的属性")
    if not isinstance(opt.get("pick"), Mapping):
        return notes.append("没有选要洗哪一根羽毛")
    items, need, where, fixed = [], {}, {}, {}
    for wing in wings(ctx.info):
        for pos, f in wing["feathers"] if str(wing["id"]) in want else ():
            if not feather_picked(opt, wing["id"], pos, f.get("id")):
                continue
            items.append(f)
            need[f["entityId"]] = want[str(wing["id"])].get(str(f.get("id")), {})
            where[f["entityId"]] = f"{wing['name']}·"
            fixed[f["entityId"]] = feather_locks(opt, wing["id"], pos, f.get("id"))
    # 免费洗炼：VIP 表 isFeatherResetKeep 才“可选择保留原属性”（响应带 needReplace，接着发 featherSureRest）；否则洗出来直接覆盖。
    # 羽毛达标的一律锁上再洗（2026-10-11 用户定，设置里不再有“自动锁定”开关）
    run_attrs(ctx, opt, budget, "Feather", R_FEATHER_FREE, R_FEATHER_SURE, "feather", FEATHER_INDEX,
              lambda row: [a for a, _ in ATTRS], "选中的那根羽毛没找到（翅膀没设条件，或孔里换了别的羽毛）", notes,
              lambda item, tops: need[item["entityId"]], items, where, fixed, lambda r: bool(r.get("needReplace")), True)


def run_formation(ctx: TaskContext, opt: Mapping[str, Any], budget: _Budget, notes: list[str]) -> None:
    want = wants(opt)
    if not want:
        return notes.append("没设要洗的属性")
    run_attrs(ctx, opt, budget, "FormationFlag", R_FLAG, R_FLAG_SURE, "formationFlag", FLAG_INDEX,  # 免费那一档也是先试、再选留新留旧
              lambda row: str(row.get("attributes", "")).split(";"), "阵眼里没有精魄", notes, lambda item, tops: {a: tops[a] * pct / 100 for a, pct in want.items() if a in tops},
              owned(ctx.info, "FormationFlag"))


RUNNERS = {"fabao": run_fabao, "feather": run_feather, "formation": run_formation}


def times_of(opt: Mapping[str, Any], key: str = "") -> int:
    """这一块最多洗几次；羽毛不设上限（每天自动把免费次数用完）。"""
    if key in UNAVAILABLE:
        return 0
    if key in UNCAPPED:
        return TIMES_MAX
    n = state.num(opt.get("times"))
    return n if n is not None and 0 < n <= TIMES_MAX else 0


def summary(options: Mapping[str, Any]) -> str:
    """任务行上的摘要：开着的几块。"""
    on = [text for key, text in MODULES if (options.get(key) or {}).get("on") and times_of(options.get(key) or {}, key)]
    return "、".join(on) if on else "未设置"


@task(ID, "自动洗练", writes=(R_FABAO_FREE, R_FEATHER_FREE, R_FEATHER_SURE, R_FLAG, R_FLAG_SURE))
def run(ctx: TaskContext) -> TaskResult:
    parts, washed, failed, user_stopped, idle, spare = [], 0, False, False, [], []
    todo = [(key, text, opt) for key, text in MODULES if (opt := ctx.options.get(key) or {}).get("on") and times_of(opt, key)]
    if todo:  # 登录快照可能过时（前面的任务、游戏里手动换过羽毛 / 洗过）：每次洗之前重新读一次，按最新的属性和材料来
        ctx.log("读取角色状态（法宝、翅膀羽毛、精魄、材料）")
        try:
            ctx.refresh()
        except p.AllianceError as exc:
            return TaskResult(ID, BLOCKED, f"{exc}；没有发出任何洗练请求")
    for key, text, opt in todo:
        if ctx.stopped:
            user_stopped = True
            break
        free = free_left(ctx.info, key)
        if free is None:
            parts.append(f"{text}：读不到 VIP 等级或今天的免费洗炼次数，不洗")
            failed = True
            continue
        budget, notes, clean = _Budget(ctx, times_of(opt, key), free), [], True
        try:
            RUNNERS[key](ctx, opt, budget, notes)
        except _UserStop as exc:
            notes.append(str(exc))
            clean = False
            user_stopped = True
        except _Stop as exc:
            notes.append(str(exc))
            clean = False
            failed = failed or not isinstance(exc, _Out)
        washed += budget.done
        rest = free - budget.done
        parts.append(f"{text}洗 {budget.done} 次（今天的免费次数还剩 {rest}）：{'；'.join(notes)}")
        if clean and notes and all(n.endswith(("已达标", ALL_LOCKED)) for n in notes):
            if key == "fabao" or rest > 0:
                idle.append(text)  # 法宝达标就标红，最后一次免费洗练达标也一样
            if rest > 0:
                spare.append(text)
        if user_stopped:
            break
    if spare:
        parts.append(f"{'、'.join(spare)}已经达标或属性全锁着，今天的免费次数还没用完：可以到“选择”里换目标或换一件接着洗")
    if "法宝" in idle and "法宝" not in spare:
        parts.append("法宝已满足筛选条件，自动洗练标红：可以到“选择”里更换目标")
    if user_stopped and not any("已被用户停止" in part for part in parts):
        parts.append("已被用户停止" if washed else "已被用户停止，未洗练")
    profile = {**feather_profile(ctx.info), **fabao_profile(ctx.info), IDLE: "、".join(idle)}  # 翅膀羽毛、法宝清单：设置里照它列；IDLE：界面把任务名标红
    parts.extend(f"{text}：暂未开放" for key, text in MODULES if key in UNAVAILABLE and (ctx.options.get(key) or {}).get("on"))
    if not parts:
        return TaskResult(ID, SKIPPED, "三块都没开（点“选择”打开要洗的，填上次数和条件）", profile=profile)
    if not LIVE:
        return TaskResult(ID, SKIPPED, "洗练还没开启，没有发出任何洗练请求，下面只是识别到的情况：\n" + "\n".join(parts), profile=profile)
    status = BLOCKED if failed else SUBMITTED if washed else SKIPPED
    return TaskResult(ID, status, "\n".join(parts), profile=profile)
