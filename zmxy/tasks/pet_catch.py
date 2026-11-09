"""宠物捕捉（周常页，时光之舟下面）：进“御马监/御花园/阆风幽境/翠竹径”，用葫芦捕捉天马/灵花/白虎/熊猫。

同一卡片内另有独立诸星封印操作行（star_seal），关卡选择、次数与执行独立于宠物捕捉。

对照客户端 ``CatchPetCmd`` / ``E9003``：进副本后对可捕捉的宠物怪（Monster.canCatch=1）用葫芦，发一次
``scene.petHandler.capture {itemId, petId}``，响应 ``success==1`` 才算捕到（怪物消失）；没捕到怪物还在，可以再扔。
捕到（或葫芦用完）就结算副本，**按失败结算**（``win=0, star=0``，和主线的“不要通关”一样，通关次数 passNum 不增加）；捕捉结果未知或响应里的 ``success`` 无效时保留副本现场，不再发送后续请求。
副本本身不限次数、无消耗，只消耗葫芦。

道具读 ``PetCatch`` 表 ``catchPropId`` 的第一项（天马/灵花=木灵葫芦，白虎=灵金葫芦，熊猫=青篁葫芦），
不用火灵、紫金葫芦；一次捕捉只发一次，不自动重发。**按客户端脚本推出，没在真机上验证**，第一次跑请看日志。

筛选（只对天马、灵花）：``options["filters"][宠物编号]`` = ``{"stars": [1, 2], "s1": {...}, "s2": {...}, "release": .., "stop": ..}``（天马、灵花各一份），
捕到后按宠物实体（PetItemVo）的字段判断，留空 = 那一头不限：
``stars`` 开了哪一星就只收那一星、只用那一星的范围（都不开 = 不筛选）；``s1`` / ``s2`` 是 1 星 / 2 星各自的一组**范围**，宠物是几星就看哪一组：
生命 ``hp`` / 回血 ``rehp`` / 攻击 ``atk`` / 防御 ``def`` 资质、体质 ``tz``（``physical``）、根骨 ``gg``（``bone``）、潜力 ``ql``（``capacity``）、战力 ``fp``（``fightPower``）；
``hp`` = 下限、``hp_max`` = 上限，两头都含。各项会在哪个范围里波动见 ``ranges``（资质等读 Pet 表，战力按公式算）。
次数 = 捕到的只数（合不合格都算），背包满暂停时按宠物编号保存已捕只数、继续时扣除；不合格的日志里写原因。``stop`` 为 True（默认关）时捕到第一只合格的就停。``release`` 为 True（默认关）时，不合格的在副本结算后放生
（``scene.petHandler.free {entityId, petId, petLevel: 1}``）；能不能放由 ``release_guard`` 决定，放不掉（被保护拦下 / 服务器拒绝）就停，不再捕。
有筛选条件但响应里读不到所需字段 → 停下，不猜，也不放生。

购买木灵葫芦是另一个任务 ``pet_gourd_buy``（宠物捕捉那一行上有自己的数量框和“执行”，和捕捉分开跑）：``options["pet_gourd_buy"]["buy"]`` = 0–``BUY_MAX``，留空 = 不买；
普通商店 ``Shop`` 表第 1 行，2000 灵魂一个，``scene.shopHandler.buy {shopId, count}``；单次最多 ``BATCH`` 个（真机上 10001 起被拒），超过就分批买，批与批之间隔 ``GAP`` 秒，灵魂不够买满就只买得起的数量。
"""

from __future__ import annotations

from .. import data, dungeon, api, state
from .. import protocol as p
from ..core import BLOCKED, PAUSED, SKIPPED, SUBMITTED, Choices, TaskContext, TaskResult, UncertainOutcome, task
from . import star_seal
from .pet_ranges import ALL_ROWS, STAR_ROWS, STARS, TALENT_ROWS, power, power_range, ranges, target  # noqa: F401  （界面和培养经这里取用）

ID = "pet_catch"
R_CAPTURE = api.R_PET_CAPTURE
R_FREE = api.R_PET_FREE  # 放生；发得出去的只有过得了 release_guard 的（1 级、没改名、没装备、1/2 星天马灵花、没培养、技能 1 级）
R_BUY = api.R_SHOP_BUY
MAX_TIMES = 1000000
BUY_MAX = 9999999  # 一次最多买几个木灵葫芦
BATCH = 9999  # 单次购买数量：真机试过，服务器上限是 10000 以内（10001、99999 都被拒：“商品不存在”），取 9999；超了拆成几批
SHOP_ID, GOURD_ID = 1, 101017001  # 普通商店里的木灵葫芦（Shop 表 id 1）、它的物品编号
GAP, COOLDOWN_RETRIES = 1.0, 3
PETS = (10001, 10002, 10003, 10004)  # 副本编号：御马监（天马）、御花园（灵花）、阆风幽境（白虎）、翠竹径（熊猫）
FILTERABLE = (10001, 10002)  # 只有天马、灵花能筛选


class ConfirmedCaptureUnsettled(Exception):
    """捕捉已确认，但副本结算被服务器拒绝；保存捕捉数后暂停续跑。"""


def gourd_price() -> int | None:
    """商店里木灵葫芦的灵魂单价；表里对不上（不是灵魂标价 / 卖的不是木灵葫芦）返回 None。"""
    row = state.row("Shop", SHOP_ID)
    if (not row or row.get("consume_type") != "coin" or row.get("consume_subType") != "spirit"
            or row.get("sell_type") != "item" or state.num(row.get("sell_subType")) != GOURD_ID or state.num(row.get("sell_count")) != 1):
        return None
    price = state.num(row.get("consume_count"))
    return price if price and price > 0 else None


def _cooldown(exc: Exception) -> bool:
    text = str(exc).casefold().replace(" ", "")
    return "购买cd中" in text and "请稍后再试" in text


def buy_gourds(ctx: TaskContext, want: int, prior: int = 0) -> tuple[int, str]:
    """分批买木灵葫芦，返回 (确认买到几个, 日志说明)。灵魂不够就只买得起的；失败 / 被停止就带着已买到的数量结束，不重试（冷却中的等 1 秒再试）。
    每批确认后把累计数量（含 prior = 暂停前已买的）记进 ctx.progress["n"]，被顶号暂停时存进断点，继续时扣掉。"""
    price = gourd_price()
    if price is None:
        return 0, "商店里的木灵葫芦对不上，未购买"
    balance = state.spirit(ctx.info)
    if balance is None:
        return 0, "读不到灵魂余额，未购买"
    afford = int(balance // price)
    total = min(want, afford)
    if total <= 0:
        return 0, f"灵魂不足（当前 {int(balance)}，一个 {price}），未购买"
    bought, first = 0, True
    note = "" if total == want else f"（灵魂只够买 {total} 个）"
    while bought < total:
        count = min(BATCH, total - bought)
        for attempt in range(COOLDOWN_RETRIES + 1):
            if ctx.stopped or ctx.winding:
                return bought, f"已停止，买到木灵葫芦 {bought} 个"
            if not first and not ctx.sleep(GAP):
                return bought, f"购买间隔中断，买到木灵葫芦 {bought} 个"
            first = False
            if ctx.stopped or ctx.winding:
                return bought, f"已停止，买到木灵葫芦 {bought} 个"
            try:
                api.buy_shop(ctx.request, SHOP_ID, count)
            except p.AllianceError as exc:
                if _cooldown(exc) and attempt < COOLDOWN_RETRIES:
                    continue
                return bought, f"购买 {count} 个失败：{exc}；已买到木灵葫芦 {bought} 个{note}"
            bought += count
            ctx.progress["n"] = prior + bought
            ctx.log(f"购买木灵葫芦 {bought}/{total}")
            break
    return bought, f"买到木灵葫芦 {bought} 个（灵魂 {bought * price}）{note}"


SEX = {1: "雄", 2: "雌"}  # PetItemVo.sex：1 = 雄（Pet.sexProbability 第一项）、2 = 雌（用 resource_female）
ID_KEYS = ("itemId", "petId", "id", "configId", "classType")


def _pets(value, out: list, depth: int = 0) -> None:
    """响应/推送里所有带 sex 的字典（捕到的宠物实体）。"""
    if depth > 6:
        return
    if isinstance(value, dict):
        if "sex" in value:
            out.append(value)
        for v in value.values():
            _pets(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _pets(v, out, depth + 1)


def entity(response, pushes) -> dict | None:
    """捕到的宠物实体（响应里找，找不到再找推送）。"""
    found: list = []
    _pets(response, found)
    if not found:
        _pets(pushes, found)
    return found[0] if found else None


def star_of(pet: dict) -> int | None:
    """星数（Pet.quality，实体里有 quality/star 就以实体为准）。"""
    stars = state.num(pet.get("quality", pet.get("star")))
    if stars is None:
        for key in ID_KEYS:
            row = data.row("Pet", pet.get(key)) if state.num(pet.get(key)) else None
            if row:
                return state.num(row.get("quality"))
    return stars


def describe(response, pushes) -> str:
    """捕到的宠物：雌雄 + 星数。认不出就把响应原样写进日志，方便核对字段。"""
    pet = entity(response, pushes)
    if pet is None:
        return "响应里找不到宠物信息：" + str(response)[:300]
    sex = SEX.get(state.num(pet.get("sex")), f"性别{pet.get('sex')}")
    stars = star_of(pet)
    return f"{sex} {stars}星" if stars is not None else f"{sex} 星数未知"


def detail(pet) -> str:
    """合格的那只写进日志的明细：战力、四项资质、体质根骨潜力、潜能（名字 + 等级）。读不到的写 ?。"""
    def val(field):
        n = state.num(pet.get(field))
        return "?" if n is None else str(n)

    quals = "/".join(f"{name.replace('资质', '')} {val(field)}" for _k, field, name in STAR_ROWS)
    gifts = "/".join(f"{name} {val(field)}" for _k, field, name in TALENT_ROWS if field != "fightPower")
    skill = pet.get("skill")
    raw = skill.get("passivity") if isinstance(skill, dict) else None
    perks = []
    for sid, level in (raw.items() if isinstance(raw, dict) else ()):
        row = data.row("PetSkill", state.num(sid)) or {}
        perks.append(f"{row.get('name') or sid} {level}级")
    return f"战力 {val('fightPower')}，资质 {quals}，{gifts}，潜能 {'、'.join(perks) or ('无' if isinstance(raw, dict) else '?')}"


def _limit(value) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def limits(flt, star: int | None) -> list[tuple[str, str, int | None, int | None]]:
    """这只宠物要满足的范围 [(名称, 实体字段, 下限, 上限)]（None = 那一头不限）：只看它那一星的那一组
    （四项资质、体质、根骨、潜力、战力，``hp`` 下限 / ``hp_max`` 上限），而且那一星的开关要开着（``stars`` 里有）；
    没开的星级填过什么都不算，不是 1 星 / 2 星的没有条件。"""
    flt = flt if isinstance(flt, dict) else {}
    out = []
    row = flt.get(f"s{star}") if star in stars_wanted(flt) else None
    row = row if isinstance(row, dict) else {}
    for key, field, name in ALL_ROWS:
        lo, hi = _limit(row.get(key)), _limit(row.get(f"{key}_max"))
        if lo is not None or hi is not None:
            out.append((f"{star}星{name}", field, lo, hi))
    return out


def stars_wanted(flt) -> tuple[int, ...]:
    raw = flt.get("stars") if isinstance(flt, dict) else None
    return tuple(sorted({n for x in raw or () if (n := state.num(x)) in STARS}))


def active(flt) -> bool:
    """有没有在筛选：开了哪一星的开关就只收那一星（再按那一星的范围挑），两个都没开 = 不筛选。"""
    return bool(stars_wanted(flt))


def check(pet: dict, flt) -> tuple[bool | None, str]:
    """捕到的宠物合不合格：(True, "") / (False, 原因) / (None, 读不到什么，没法判断)。"""
    star = star_of(pet)
    wanted = stars_wanted(flt)
    if wanted:
        if star is None:
            return None, "读不到星数"
        if star not in wanted:
            return False, f"{star}星不在所选星级里"
    for name, field, least, most in limits(flt, star):
        value = state.num(pet.get(field))
        if value is None:
            return None, f"读不到{name}"
        if least is not None and value < least:
            return False, f"{name} {value}<{least}"
        if most is not None and value > most:
            return False, f"{name} {value}>{most}"
    return True, ""


def catalog() -> list[tuple[int, str]]:
    return [(mid, t[0]) for mid in PETS if (t := target(mid))]


def _settle_room(req, client: int, server: int, caught: bool, name: str, count: int) -> None:
    """按失败结算，不增加 passNum。已确认捕到时，结算的异常要带上这一点：继续运行不能重抓一只。"""
    try:
        dungeon._settle(req, client, server, win=False)
    except UncertainOutcome as exc:
        if caught:
            raise UncertainOutcome(f"已确认捕到 {name}（累计 {count} 只），但副本结算结果未知：{exc}") from exc
        raise
    except dungeon.Stop as exc:
        if caught:
            raise ConfirmedCaptureUnsettled(f"已确认捕到 {name}（累计 {count} 只），但副本结算未确认：{exc}") from exc
        raise


def catch_once(ctx: TaskContext, mid: int, name: str, pet_id: int, left: dict[int, int], gains: dungeon.Gains, notes: list[str],
               captured_before: int, flt, verdicts: list) -> tuple[bool, str | None]:
    """进一次副本捕捉。返回 (是否捕到, 停止原因)；停止原因为 None 表示可以继续下一次。
    flt：筛选条件（None = 不筛）；捕到时把 (check 的结果…, 宠物实体) 追加进 verdicts。"""
    session = ctx.session
    if ctx.stopped:
        return False, "已被用户停止"

    def req(route: str, body=None):
        response = ctx.request(route, body)
        pushes = session.drain_pushes()
        gains.add(response, pushes)
        if route == R_CAPTURE and dungeon._find(response, ("success",)) == 1:
            notes.append(describe(response, pushes))
            if flt is not None:
                pet = entity(response, pushes)
                verdicts.append((*check(pet, flt), pet) if pet is not None else (None, "响应里找不到宠物信息", None))
        return response

    client = dungeon.random.randint(0, 9999)
    try:
        created = req(dungeon.R_CREATE, {"mid": mid, "rnd": client})
    except p.AllianceError as exc:
        return False, f"创建失败：{exc}"
    server = dungeon._find(created, ("rnd", "random", "serverRnd", "server_rnd"))
    if server is None:
        return False, "创建响应缺少 rnd，未结算"
    caught, why = False, None
    settle_room = True
    try:
        while not caught:
            item = next((i for i, n in left.items() if n > 0), None)
            if item is None:
                why = "葫芦用完了"
                break
            if ctx.stopped:
                why = "已被用户停止"
                break
            left[item] -= 1  # 先占用：请求后无论结果都当作用掉，不自动退回
            try:
                response = api.capture_pet(req, item, pet_id)
            except UncertainOutcome:
                settle_room = False  # 捕捉可能已成功；保留副本供用户核对，不再发后续请求
                raise
            except p.AllianceError as exc:
                why = f"捕捉被拒绝：{exc}"
                break
            flag = dungeon._find(response, ("success",))
            if flag not in (0, 1):
                settle_room = False  # 响应不能证明成功或失败；保留现场，也不继续消耗葫芦
                why = "捕捉响应里的 success 缺失或无效，无法确认结果"
                break
            caught = flag == 1
            if caught:
                # 捕获成功已由服务器确认；即使随后失败结算中断，继续运行也不能重抓一只。
                ctx.progress[str(mid)] = captured_before + 1
    finally:
        if settle_room:
            _settle_room(req, client, server, caught, name, captured_before + 1)
    return caught, why


def release(ctx: TaskContext, pet: dict) -> str | None:
    """放生一只筛选不合格的宠物（副本结算之后发）。返回没放成的原因，放掉了 = None。放生保护拒绝、服务器拒绝都只是没放成。"""
    if ctx.stopped:
        return "已被用户停止，未放生"
    try:
        api.free_pet(ctx.request, pet.get("entityId"), pet.get("id"), 1)
    except p.AllianceError as exc:
        return str(exc)
    return None


class _Tally:
    """一种宠物这次执行的战果。done 含暂停前已捕到的只数，got 是合格的。"""

    def __init__(self, done: int) -> None:
        self.done, self.got, self.rejected, self.freed = done, 0, 0, 0
        self.details: list[str] = []

    def summary(self, name: str, filtered: bool) -> str:
        kept = self.rejected - self.freed
        fate = "没放生" if not self.freed else "已放生" if not kept else f"放生 {self.freed} 只，{kept} 只没放生"
        head = f"{name}×{self.done}" + (f"，合格 {self.got} 只" if filtered else "") + (f"，不合格 {self.rejected} 只（{fate}）" if self.rejected else "")
        return head + (f"（{'、'.join(self.details)}）" if self.details else "")


def _reject(ctx: TaskContext, tally: _Tally, ok: bool | None, reason: str, pet, notes: list[str], free: bool) -> tuple[str, str | None]:
    """处理没过筛选的一只，返回 (日志说明, 停止原因)。"""
    note = f"{notes[0] if notes else ''} 不合格：{reason}".strip()
    if ok is None:  # 读不到字段：不猜，停下来看日志核对（也不放生）
        return note, f"捕到了但无法筛选（{reason}），已停止"
    tally.rejected += 1
    if not free:
        return note, None
    failed = release(ctx, pet)  # 副本已结算；放不放得掉由放生保护说了算
    if failed is None:
        tally.freed += 1
        return note + "，已放生", None
    return note + f"，没放生（{failed}）", "不合格的宠物没放生成功，已停止"  # 放不掉就别再捕了，免得不合格的越攒越多


def _catch_kind(ctx: TaskContext, mid: int, name: str, pet_id: int, times: int, left: dict[int, int], done: int, use,
                gains: dungeon.Gains, parts: list[str]) -> str | None:
    """捕一种宠物，直到次数够了或该停。战果写进 parts，返回停止原因。"""
    free = use is not None and use.get("release") is True  # 自动放生不合格的：默认关，筛选里的开关
    first = use is not None and use.get("stop") is True  # 捕到合格停止：默认关，筛选里的开关
    tally, stop = _Tally(done), None
    while tally.done < times:
        if ctx.stopped:
            stop = f"{name}：已被用户停止"
            break
        notes: list[str] = []
        verdicts: list[tuple[bool | None, str]] = []
        caught, why = catch_once(ctx, mid, name, pet_id, left, gains, notes, tally.done, use, verdicts)
        ok, reason, pet = verdicts[0] if verdicts else (True, "", None)
        tally.done += caught
        if caught:
            ctx.progress[str(mid)] = tally.done  # 只记服务器明确确认捕到的宠物
        if caught and use is not None and ok is not True:
            caught = False
            note, halt = _reject(ctx, tally, ok, reason, pet, notes, free)
            why = why or halt
            notes = [note]
        elif caught and use is not None and isinstance(pet, dict):
            notes = [f"{notes[0] if notes else ''} 合格：{detail(pet)}".strip()]
        tally.got += caught
        tally.details.extend(notes)
        ctx.log(f"{name} 捕到 {tally.done}/{times}" + (f"，合格 {tally.got}" if use is not None else "") + (f"（{notes[0]}）" if notes else ""))
        if why:
            stop = f"{name}：{why}"
            break
        if first and tally.got:
            ctx.log(f"{name}：捕到合格的了，停止")
            break
    if tally.done or tally.details:
        parts.append(tally.summary(name, use is not None))
    return stop


def _targets(picked) -> list | TaskResult:
    """选中的宠物去重成 [(副本, 宠物名, petId, 葫芦)]；有认不出的返回 TaskResult。"""
    targets = []
    for raw in picked:
        mid = state.num(raw)
        t = target(mid) if mid in PETS else None
        if t is None:
            return TaskResult(ID, BLOCKED, f"{raw} 不是可捕捉的宠物，未执行")
        if all(x[0] != mid for x in targets):
            targets.append((mid, *t))
    return targets


def _with_summary(parts: list[str], text: str) -> str:
    return "捕到 " + "、".join(parts) + "；" + text if parts else text


@task(ID, "宠物捕捉", writes={dungeon.R_CREATE, dungeon.R_FINISH, R_CAPTURE, R_FREE}, group="周常", times_max=MAX_TIMES, times_default=1,
      choices=Choices(catalog, "宠物", cols=4, limit=1,
                      caption="选要捕捉的宠物；行上的次数 = 最多捕到几只（合不合格都算）"))
def run(ctx: TaskContext) -> TaskResult:
    times = dungeon.read_times(ctx, ID, MAX_TIMES, 1)
    if isinstance(times, TaskResult):
        return times
    picked = ctx.options.get("levels") or []
    if not picked:
        return TaskResult(ID, SKIPPED, "没有选择宠物")
    targets = _targets(picked)
    if isinstance(targets, TaskResult):
        return targets
    if times == 0:
        return TaskResult(ID, SKIPPED, "次数留空，不执行")
    prior = dungeon.resume_done(ctx)
    ctx.progress.update(prior)
    if all(prior.get(str(mid), 0) >= times for mid, *_rest in targets):
        return TaskResult(ID, SUBMITTED, "暂停前已经捕到设置的次数")
    filters = ctx.options.get("filters")  # 筛选按宠物各记各的：{"10001": {...}, "10002": {...}}
    filters = filters if isinstance(filters, dict) else {}
    gains, parts, stop = dungeon.Gains(), [], None
    try:
        for mid, name, pet_id, items in targets:
            if ctx.stopped:
                stop = "已被用户停止"
                break
            flt = filters.get(str(mid))
            use = flt if mid in FILTERABLE and active(flt) else None  # 白虎、熊猫不筛
            left = {}
            for item in items:
                n = state.bag_count(ctx.info, item)
                if n is None:
                    return TaskResult(ID, BLOCKED, _with_summary(parts, f"读不到背包里的葫芦数量，{name}未执行"))
                left[item] = n
            done = prior.get(str(mid), 0)
            if done >= times:
                parts.append(f"{name}×{done}（暂停前已完成）")
                continue
            if not any(left.values()):
                ctx.log(f"{name}：没有葫芦，跳过")
                continue
            stop = _catch_kind(ctx, mid, name, pet_id, times, left, done, use, gains, parts)
            if stop and not stop.endswith("葫芦用完了"):
                break
            stop = None  # 葫芦用完只影响这种宠物，下一种用的是别的葫芦
    except ConfirmedCaptureUnsettled as exc:
        return TaskResult(ID, PAUSED, _with_summary(parts, f"{exc}；已保存捕捉计数，请先核对并处理当前副本，再点「继续」"),
                          progress=dict(ctx.progress))
    except dungeon.Stop as exc:
        stop = str(exc)
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"{'，'.join(parts) or '尚未捕到'}；{exc}") from exc
    if ctx.stopped:
        return TaskResult(ID, PAUSED, _with_summary(parts, "已被用户停止；捕捉进度已保存"), progress=dict(ctx.progress))
    if stop:
        return TaskResult(ID, BLOCKED, _with_summary(parts, f"停止于 {stop}"))
    return TaskResult(ID, SUBMITTED if parts else SKIPPED, "捕到 " + "、".join(parts) if parts else "没有捕到（葫芦不足）")


BUY_ID = "pet_gourd_buy"


@task(BUY_ID, "购买木灵葫芦", writes={R_BUY}, spends={R_BUY}, group="周常")
def run_buy(ctx: TaskContext) -> TaskResult:
    raw = ctx.options.get("buy")
    want = 0 if raw in (None, "") else state.num(raw)
    if want is None or not 0 <= want <= BUY_MAX:
        return TaskResult(BUY_ID, BLOCKED, f"数量必须在 0–{BUY_MAX} 之间（留空不买），未执行")
    if want == 0:
        return TaskResult(BUY_ID, SKIPPED, "数量留空，不购买")
    prior = dungeon.resume_done(ctx).get("n", 0)  # 被顶号暂停前已买到的：从设置的数量里扣掉
    ctx.progress["n"] = prior
    if want <= prior:
        return TaskResult(BUY_ID, SUBMITTED, f"暂停前已经买够（{prior} 个）")
    bought, note = buy_gourds(ctx, want - prior, prior)
    if ctx.stopped or ctx.winding or note.startswith("购买间隔中断"):
        return TaskResult(BUY_ID, PAUSED, f"{note}；已保存购买进度", progress=dict(ctx.progress))
    return TaskResult(BUY_ID, SUBMITTED if bought else BLOCKED if "失败" in note else SKIPPED, note)
