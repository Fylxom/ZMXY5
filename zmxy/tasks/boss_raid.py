"""联盟 Boss / 世界 Boss（周常页“Boss”模块，单独执行）。都是“进场 → 定时上报伤害 → 等结算推送 → 退出”，没有本地战斗。

* 联盟 Boss：``challengeBoss`` 进场，每秒 ``attackBossCopy {hurt}``（每分钟约 0.2% 最大血量），结算 ``onFinishLeagueBossCopy`` 后 ``exitLeagueCopy``。
* 世界 Boss（祖巫）。排名奖励：伤害占比 ≥ 5% 且在自己的榜（战力 ≥ ``highPower`` 进高战榜，否则普通榜）前三；前三奖励一样，只求稳进前三。
  1. 选房：没满员、血量过半的房间里人最少的；一个祖巫试 ``ROUND`` 个都争不到就换下一个祖巫，都没有再等重开的新房间（最多 ``WAIT_ROOMS`` 秒）。
  2. 对手 = 同榜占比到 ``QUIT_SHARE``% 的人，占比估到 Boss 死的时候。要保持在“第 3 高的 + ``LEAD``”以上；战力比我高的对手有 3 个就退出换房间。
  3. 伤害照真人的节奏报，从进房打到 Boss 死：一条命被普通招式打死（``LIFE``）、挨全场秒杀（``KILL_*``）或蓝空（``DRAINED``），倒下等复活（``REST``），偶尔短停（``PAUSE``）。
     每秒伤害 = 战力 × ``PACE`` × ``scale`` × 装备倍数 × ``HARD`` × ``flow``。三档：低 = 只正常输出；中 = 不够稳才追赶（``CHASE``，倒下两秒就起来）；高 = 全程追赶。
     追赶的速度也够不到就退出换房间（``Room.hopeless``）。
  4. 结算 ``onFinishWorldBossCopy`` 带名次和奖励。

代码分四处：本文件是注册入口和换房间的流程，``boss_fight`` 是进房后的出手节奏，``boss_room`` 是局面判断，``boss_rules`` 是参数和小算法。

进场后任何路径结束都会尝试退出副本；上报失败 → ``UncertainOutcome``，不重发。买次数的路由不在声明里。各参数怎么量的见 docs/CHANGELOG.md、docs/LESSONS.md。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable, Mapping

from .. import bosslog, data, api, online, protocol as p, servertime, state, timeplan
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, TaskContext, TaskResult, task
from . import boss_rules as r
from .boss_fight import recount, world_fight
from .boss_room import Room

UNION_ID, WORLD_ID = "union_boss", "world_boss"
R_UNION_ENTER = "league.leagueHandler.challengeBoss"
R_UNION_HIT = "scene.leagueHandler.attackBossCopy"
R_UNION_EXIT = "scene.leagueHandler.exitLeagueCopy"
PERCENT_PER_MINUTE = 0.2     # 联盟 Boss：每分钟伤害占最大血量的百分比
PROFILE_KEY = "role_level"   # 账号 profile 里的角色等级：选择框据此过滤祖巫
HEALTHY = 0.5                # 选房：血量比例 ≥ 这个值才进
ROUND = 3                    # 一个祖巫先在现有房间里试这么多个
FRESH = 0.9                  # 血量比例 ≥ 这个值 = 刚重开的新房间
WAIT_ROOMS = 1800            # 等新房间最多多久（秒）
POLL = 10                    # 等新房间时隔几秒查一次房间列表
SKIP_GOLD = "skip_gold"      # 一次性开关：只在本轮结算出传说阵眼后跳过对应祖巫，历史记录不参与
BETWEEN = (6.0, 12.0)        # 打完一场到进下一场隔几秒
GOLD = 4                     # FormationFlag.quality：4 = 传说
RESERVE_WEEK = 4  # 次数留给翅膀光环养成周（state.GROWTH_WEEKS 的序号）；定时开着“按养成周”时选项里带 plan="growth"
MAX_FIGHTS = 9               # 行上“次数”的上限，也是没填过时的初始值（= 有几次打几次）
P_UNION_HIT, P_UNION_HP, P_UNION_FINISH = "onBossCopySelfHit", "onLeagueBossHpChange", "onFinishLeagueBossCopy"


def world_bosses() -> list[tuple[int, str]]:
    """WorldBoss 表里的祖巫，按 order 排。"""
    rows = sorted(data.table("WorldBoss").values(), key=lambda row: state.num(row.get("order")) or 0)
    return [(int(row["id"]), str(row["bossName"])) for row in rows if state.num(row.get("id"))]


def role_level(info: Mapping[str, Any]) -> int | None:
    p1 = info.get("p1")
    return state.num(p1.get("level")) if isinstance(p1, Mapping) else state.num(info.get("level"))


def boss_level(boss_id: int) -> int | None:
    """客户端 WorldBossModule：角色等级 ≥ WorldBoss.bossLevel 才能挑战。"""
    row = data.row("WorldBoss", boss_id)
    return state.num(row.get("bossLevel")) if row else None


def available(boss_id: int, profile: Mapping[str, Any]) -> bool:
    """选择对话框里是否显示该祖巫：读到过角色等级就按等级过滤；没读到放行（由进场时再判）。"""
    level, need = state.num(profile.get(PROFILE_KEY)), boss_level(boss_id)
    return level is None or need is None or level >= need


def union_hurt(hp_max: float) -> float:
    rate = hp_max * PERCENT_PER_MINUTE / 100 / 60
    return random.uniform(rate / 4, rate * 7 / 4)


def _my_card(request: Callable[[str, dict], Any], uid: int | None) -> dict:
    """“点开自己看资料”：``{"userdata": 资料}``；查不到 → 空的。"""
    try:
        mine = api.offline_user_info(request, uid) if uid is not None else None
    except p.AllianceError:
        return {}
    return {"userdata": mine["user"]} if isinstance(mine, Mapping) and isinstance(mine.get("user"), Mapping) else {}


def forecast(power: float, gear: float | None, dry: bool, bosses: Iterable[int] | None = None) -> list[tuple[int, float, float]]:
    """界面“估伤”：照低档 / 高档打，平均每分钟打掉各祖巫百分之几 → [(祖巫 id, 低档, 高档)]；``bosses`` 不给 = 全部祖巫。和 ``boss_fight`` 用同一套数：
    低档 = PACE 中间值 × LIVE（深渊饰品再 × ABYSS_STAY），高档 = CHASE 中间值 × DUTY；算上了等复活和短停，是长时间平均的中位水平。"""
    out = []
    for boss in (b for b, _ in world_bosses()) if bosses is None else bosses:
        hp = r.real((data.row("Monster", boss) or {}).get("hpMax"))
        if hp:
            each = power * r.scale(power) * (gear or 1.0) * r.HARD.get(boss, 1.0) / hp * 100 * 60
            out.append((boss, sum(r.PACE) / 2 * each * r.LIVE * (r.ABYSS_STAY if dry else 1.0), sum(r.CHASE) / 2 * each * r.DUTY))
    return out


def estimate(session: Any) -> tuple[float, float | None, list[tuple[int, float, float]], int | None]:
    """登录以后只读地估一下：自己的战力、装备倍数（读不到 = None，按主线饰品、没有红莲算）、全部祖巫的 ``forecast``、角色等级（选择框据此过滤祖巫）。
    装备从“点开自己看资料”的应答里读，只留在内存里，不存。"""
    info, uid = session.info, state.num(session.info.get("uid"))
    me = _my_card(session.request, uid)
    power = r.real(info.get("power")) or r.real(info.get("fightPower")) or r.real(me.get("userdata", {}).get("fightPower"))
    if not power:
        raise p.AllianceError("读不到自己的战力")
    return power, r.gear_of(me), forecast(power, r.gear_of(me), r.abyss(me)), role_level(info)


def pick_room(rooms: Any, tried: Any, cap: float | None, floor: float, fresh: bool = False) -> Mapping[str, Any] | None:
    """血量高、人少：没满员且血量 ≥ HEALTHY 的房间里人最少的（人数相同取血量高的）；没有这样的就退到"血量还够打出两倍目标"的。
    ``fresh`` = 只要刚重开的新房间（血量 ≥ FRESH）。``tried`` 里的房间不进。"""
    def ratio(room: Mapping[str, Any]) -> float:
        return (r.real(room["boss"].get("hp")) or 0) / r.real(room["boss"]["hpMax"])

    alive = [room for room in rooms if isinstance(room, Mapping) and isinstance(room.get("boss"), Mapping) and room.get("roomId") is not None
             and room["roomId"] not in tried and (r.real(room["boss"].get("hpMax")) or 0) > 0 and ratio(room) > 0
             and (not cap or (state.num(room.get("userCnt")) or 0) < cap)] if isinstance(rooms, list) else []
    if fresh:
        ok = [room for room in alive if ratio(room) >= FRESH]
    else:
        ok = [room for room in alive if ratio(room) >= HEALTHY] or [room for room in alive if ratio(room) >= floor * 2 / 100]
    return min(ok, key=lambda room: (state.num(room.get("userCnt")) or 0, -ratio(room))) if ok else None


def reborn(rooms: Any, tried: dict, now: float) -> None:
    """进过又退出来的房间（``tried``：房间 → (离开时的 Boss 血量, 那一场到点的时刻 / None)）里，哪些已经重开了一场、可以再进：
    Boss 死过了（血量 0）、血量比我离开时还高，或者那一场的结束时间已经过了（我在别的房间里打着，没看见它死）。"""
    for room in rooms if isinstance(rooms, list) else ():
        if isinstance(room, Mapping) and isinstance(room.get("boss"), Mapping) and room.get("roomId") in tried:
            hp, (seen, ends) = r.real(room["boss"].get("hp")) or 0.0, tried[room["roomId"]]
            if hp <= 0 or hp > seen or (ends is not None and now >= ends):
                del tried[room["roomId"]]


def nap(rooms: Any, now: float | None) -> float:
    """等新房间时隔几秒再查房间列表：有房间报了 Boss 的复活时刻（``rebirthTime``，毫秒）就卡在最早的那个之后 1 秒，最长 POLL 秒；
    没报 / 读不到服务器时间 / 时间对不上都按 POLL。"""
    waits = []
    for room in rooms if isinstance(rooms, list) and now else ():
        at = r.real(room.get("rebirthTime")) if isinstance(room, Mapping) else None
        if at and at / 1000 > now:
            waits.append(at / 1000 - now)
    return min(float(POLL), min(waits) + 1) if waits else float(POLL)


def golden(finished: Any) -> list[str]:
    """结算推送里的传说阵眼的名字。奖励是 ``{type, subType: "id,id", count: "n;n"}``（客户端 HandWorldBossInfoCmd），参与、排名、击败三份都看。"""
    names = []
    for key in ("joinReward", "rankReward", "defeatReward"):
        prize = finished.get(key) if isinstance(finished, Mapping) else None
        for item in str(prize.get("subType") or "").replace(";", ",").split(",") if isinstance(prize, Mapping) else ():
            row = data.row("FormationFlag", item.strip()) if item.strip().isdigit() else None
            if row and (state.num(row.get("quality")) or 0) >= GOLD:
                names.append(str(row.get("name") or item))
    return names


def _obj(response: Any) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise p.AllianceError("服务器返回的不是对象")
    return response


def _union_fight(ctx: TaskContext, hp_max: float, start_ms: float, end_ms: float) -> tuple[bool, str]:
    """联盟 Boss 进场后的上报循环；返回 (是否收到结算推送, 说明)。退出副本由调用方负责。"""
    now = servertime.now()  # end_ms / start_ms 是服务器时间
    left = end_ms / 1000 - now if end_ms else 0
    deadline = time.monotonic() + (left + r.GRACE_SECONDS if left > 0 else r.FALLBACK_SECONDS)
    wait_start = start_ms / 1000 - now if start_ms else 0
    if wait_start > 0:
        ctx.log(f"联盟Boss：等待开战约 {wait_start:.0f} 秒")
        if not ctx.sleep(min(wait_start, deadline - time.monotonic()), background=False):
            return False, "已被用户停止"
    hit, hp, sent, finished = 0.0, None, 0, None

    def absorb() -> None:
        nonlocal hit, hp, finished
        for route, body in ctx.session.drain_routed():
            if not isinstance(body, Mapping):
                continue
            if route == P_UNION_HIT and body.get("hit") is not None:
                hit = state.num(body["hit"]) or hit
            elif route == P_UNION_HP and body.get("hp") is not None:
                hp = state.num(body["hp"])
            elif route == P_UNION_FINISH:
                finished = body

    while finished is None and time.monotonic() < deadline:
        if ctx.stopped:
            return False, "已被用户停止"
        if hp is not None and hp <= 0:  # Boss 已倒：不再上报，等结算推送
            try:
                finished = ctx.wait_push(P_UNION_FINISH, max(1.0, deadline - time.monotonic()))
            except p.AllianceError:
                break
            continue
        try:
            ctx.request(R_UNION_HIT, {"hurt": union_hurt(hp_max)})
        except p.AllianceError as exc:
            absorb()
            if finished is None:
                return False, f"伤害上报停止：{exc}"
            break
        sent += 1
        absorb()
        if sent % 10 == 0:
            ctx.log(f"联盟Boss：已上报 {sent} 次，服务器计伤 {hit:,.0f}（{hit / hp_max:.2%}）")
        if finished is None and not ctx.sleep(1, background=False):
            return False, "已被用户停止"
    if finished is None:  # 时间到了还没收到结算：再等一小会儿
        try:
            finished = ctx.wait_push(P_UNION_FINISH, 10)
        except p.AllianceError:
            pass
    note = f"上报 {sent} 次，服务器计伤 {hit:,.0f}（{hit / hp_max:.2%}）"
    if finished is None:
        return False, f"{note}；等待结算推送超时"
    return True, f"{note}；已结算" + ("（击败）" if isinstance(finished, Mapping) and finished.get("success") else "")


def _exit(ctx: TaskContext, route: str) -> None:
    try:
        ctx.request(route, {})
    except p.AllianceError:  # 服务器明确拒绝退出，不覆盖本场已经确定的结果
        pass


@task(UNION_ID, "联盟Boss", writes={R_UNION_ENTER, R_UNION_HIT, R_UNION_EXIT}, group="周常")
def union_boss(ctx: TaskContext) -> TaskResult:
    league = ctx.info.get("league")
    if not isinstance(league, Mapping) or not league.get("leagueId"):
        return TaskResult(UNION_ID, SKIPPED, "当前存档未加入联盟")
    try:
        entered = _obj(ctx.request(R_UNION_ENTER, {"pNum": 0}))
    except p.AllianceError as exc:
        return TaskResult(UNION_ID, BLOCKED, f"进入联盟 Boss 失败：{exc}")
    try:
        boss = entered.get("Boss") or entered.get("boss")
        hp_max = state.num(boss.get("hpMax")) if isinstance(boss, Mapping) else None
        if not hp_max or hp_max <= 0:
            return TaskResult(UNION_ID, BLOCKED, "服务器没有返回 Boss 最大血量，未上报伤害")
        ctx.engage(True)
        done, note = _union_fight(ctx, hp_max, state.num(entered.get("startTime")) or 0, state.num(entered.get("endTime")) or 0)
    finally:
        _exit(ctx, R_UNION_EXIT)
    return TaskResult(UNION_ID, SUBMITTED if done else BLOCKED, note)


def _chances(info: Mapping[str, Any]) -> int | None:
    box = info.get("worldBoss")
    return state.num(box.get("count")) if isinstance(box, Mapping) else None


def auto_chances(ctx: TaskContext) -> int | None:
    """周常页自动配置：只读当前账号共享的世界 Boss 剩余奖励次数。"""
    return _chances(ctx.info)


class WorldRun:
    """一次执行：按队列轮着打选中的祖巫，次数用完为止。"""

    def __init__(self, ctx: TaskContext, info: Mapping[str, Any], names: dict, queue: list[int], gear: str, chances: int, level: int | None, profile: dict | None) -> None:
        self.ctx, self.names, self.queue, self.gear, self.chances = ctx, names, queue, gear, chances
        self.level, self.profile = level, profile
        self.uid = state.num(info.get("uid"))
        self.skip_gold = bool(ctx.options.get(SKIP_GOLD))
        self.helper = data.row("WorldBossHelper", 1) or {}
        self.threshold = (r.real(self.helper.get("damage")) or r.THRESHOLD / 100) * 100
        self.floor = max(random.uniform(*r.DEFAULT_PERCENT), self.threshold)
        self.power = r.real(info.get("power")) or r.real(info.get("fightPower"))
        self.read_me()
        self.tried: dict = {boss: {} for boss in queue}   # 各祖巫进过又退出来的房间 → (离开时的 Boss 血量, 那一场到点的时刻)；重开了的由 reborn 去掉
        self.entered = dict.fromkeys(queue, 0)            # 各祖巫上一场打完以后又进过几个房间（都退出来了）
        self.last = dict.fromkeys(queue, "")              # 各祖巫上一个房间为什么退出
        self.fought: list[str] = []                       # 打完了的每一场的结果
        self.wait_end = None                              # 不为 None = 已经在等新房间了（等到这个时刻为止）
        self.turn = 0
        self.barren = 0                                   # 连着几个祖巫没有能进的房间
        self.rooms = None

    def read_me(self) -> None:
        """进房前先照“估伤”那样看一眼自己的资料（用户 2026-10-11 定）：装备倍数、满蓝能撑多久从这里读，不等进房后的推送。只留在内存里，不存。"""
        me = _my_card(self.ctx.request, self.uid)
        if not self.power and isinstance(me.get("userdata"), Mapping):
            self.power = r.real(me["userdata"].get("fightPower")) or r.real(me["userdata"].get("power"))
        self.panel = me.get("userdata", {}).get("powerAttr")
        self.worn, self.dry = r.gear_of(me), r.abyss(me)
        self.mana = r.mana_seconds(me, self.panel) or r.MANA_USUAL
        self.ctx.log("世界Boss：装备倍数 " + (f"{self.worn:.2f}" if self.worn else "读不到，先按主线饰品、没有红莲算") + f"；满蓝大约能撑 {self.mana:.0f} 秒")

    def over(self, status: str, note: str, completed: bool = False) -> TaskResult:
        # 已完成的场次可以正常返回 submitted；后续查询/进场/结算失败必须保留 blocked，不能被前面的成功场次掩盖。
        final = SUBMITTED if self.fought and (status == SKIPPED or completed) else status
        return TaskResult(WORLD_ID, final, "；".join(self.fought + ([note] if note else [])), self.profile)

    def fight_in(self, boss_id: int, found: Mapping[str, Any], joined: Mapping[str, Any], high_power: float | None) -> TaskResult | tuple:
        """进了房间以后：打一场，不管怎么结束都退房、记录。返回 TaskResult（到此为止）或 (结果, 说明, 房间, 那一场到点的时刻)。"""
        ctx, room_id, boss = self.ctx, found["roomId"], found["boss"]
        journal = bosslog.Journal(time.monotonic(), boss=boss_id, bossName=self.names[boss_id], room=room_id, hpMax=r.real(boss["hpMax"]),
                                  listed=found, rooms=self.rooms, joined={k: v for k, v in joined.items() if k != "message"},
                                  me={"uid": self.uid, "power": self.power, "level": self.level, "highPower": high_power,
                                      "floor": round(self.floor, 2)})
        outcome, note, room = None, "", None
        try:
            ctx.engage(True)  # 进了房间：从这里起点“停止”要问打完再停还是立即停
            if joined.get("roomId") != room_id:
                return self.over(BLOCKED, "加入房间的应答与所选房间不符，已退出")
            room = Room(r.real(boss["hpMax"]), self.uid, self.power, high_power, self.floor, r.LEAD[0][1], self.threshold, journal=journal, boss=boss_id, gear_mode=self.gear,
                        panel=self.panel if isinstance(self.panel, Mapping) else None, gear=self.worn, dry=self.dry, mana=self.mana, room_id=str(room_id),
                        hp=r.real(joined["Boss"].get("hp")) if isinstance(joined.get("Boss"), Mapping) else None)
            room.absorb(r.P_JOIN, joined, time.monotonic())
            end_ms, server_ms = r.real(joined.get("endTime")) or 0, r.real(joined.get("serverTime")) or servertime.now() * 1000
            ends = time.monotonic() + (end_ms - server_ms) / 1000 if end_ms else None  # 这一场到点的时刻
            outcome, note = world_fight(ctx, room, lambda h: {"roomId": room_id, "hurt": h, "bossId": boss_id}, (end_ms - server_ms) / 1000 if end_ms else 0)
            return outcome, note, room, ends
        finally:
            self.leave(journal, room, outcome, note)

    def leave(self, journal: bosslog.Journal, room: Room | None, outcome: str | None, note: str) -> None:
        ctx = self.ctx
        exit_error = None
        try:
            _exit(ctx, r.R_WORLD_EXIT)
        except Exception as exc:  # noqa: BLE001 保留 UncertainOutcome，先完成本地收尾和日志
            exit_error = exc
        finally:
            ctx.engage(False)
            if outcome:  # 退房以后再数一次同场的人的还魂丹
                try:
                    recount(ctx.request, journal, lambda seconds: ctx.sleep(seconds, background=False))
                except Exception:  # noqa: BLE001 数不到不影响结果
                    pass
            if outcome or journal.players:  # 进房应答就对不上的：什么都没看到，不记
                journal.finish = room.finished if outcome else None
                journal.save(outcome, note if outcome else "没打成", self.uid)
        if exit_error:
            raise exit_error

    def try_rooms(self, boss_id: int) -> TaskResult | tuple:
        """这个祖巫：现有的房间一个个试，打成一场或者没有能进的为止。返回 TaskResult（到此为止）或 (结果, 说明, 房间)。"""
        ctx, name = self.ctx, self.names[boss_id]
        high_power = r.real((data.row("WorldBoss", boss_id) or {}).get("highPower"))
        outcome, note, room = None, "", None
        while True:
            if ctx.winding:  # 用户选了“打完这场再停”：不再进下一个房间
                return self.over(BLOCKED, "已按要求打完这一场后停止", completed=True)
            try:
                listed = _obj(ctx.request(r.R_WORLD_LIST, {"bossId": boss_id, "type": 0}))
            except p.AllianceError as exc:
                return self.over(BLOCKED, f"查询{name}的房间失败：{exc}")
            self.rooms = listed.get("rooms")
            reborn(self.rooms, self.tried[boss_id], time.monotonic())
            found = pick_room(self.rooms, self.tried[boss_id], r.real(self.helper.get("roomPeople")), self.floor, fresh=self.wait_end is not None or self.entered[boss_id] >= ROUND)
            if found is None:
                return outcome, note, room
            room_id, boss = found["roomId"], found["boss"]
            if self.power and high_power:
                ctx.log(f"世界Boss：战力 {self.power:,.0f}，{name}高战线 {high_power:,.0f} → 预计进{r.BOARDS[1 if self.power >= high_power else 2]}")
            ctx.log(f"世界Boss：进{name}的房间 {room_id}（血量 {r.real(boss.get('hp')) / r.real(boss['hpMax']):.0%}，{state.num(found.get('userCnt')) or 0} 人）")
            ctx.session.drain_routed()  # 进房前把攒着的推送扔掉：都是上一个房间的，留着会被当成这个房间的占比、榜单
            try:
                joined = _obj(ctx.request(r.R_WORLD_JOIN, {"bossId": boss_id, "roomId": room_id, "type": 0}))
            except p.AllianceError as exc:
                return self.over(BLOCKED, f"加入{name}的房间失败：{exc}")
            played = self.fight_in(boss_id, found, joined, high_power)
            if isinstance(played, TaskResult):
                return played
            outcome, note, room, ends = played
            if outcome != r.QUIT:
                return outcome, note, room
            self.entered[boss_id], self.last[boss_id] = self.entered[boss_id] + 1, note
            self.tried[boss_id][room_id] = (min(h for h in (room.hp, r.real(boss.get("hp")), room.hp_max) if h is not None), ends)
            ctx.log(f"世界Boss：{note}，已退出{name}的房间 {room_id}")
            if not ctx.sleep(3, background=False):
                return self.over(BLOCKED, f"已被用户停止（{name}进过 {self.entered[boss_id]} 个房间，都退出了）", completed=bool(self.fought))

    def won(self, boss_id: int, note: str, room: Room) -> TaskResult | None:
        """打完一场：用掉一次，轮到下一个祖巫。"""
        ctx, name = self.ctx, self.names[boss_id]
        self.chances, self.wait_end, self.barren, self.entered[boss_id] = self.chances - 1, None, 0, 0
        gold = golden(room.finished)
        self.fought.append(f"{name}：{note}" + (f"，出了传说阵眼（{'、'.join(gold)}）" if gold else ""))
        if gold:
            bosslog.gold(self.uid, boss_id)
            if self.skip_gold:  # 本轮出金：只从本轮队列移除；下一轮仍按所选祖巫重新开始
                self.queue.remove(boss_id)
                ctx.log(f"世界Boss：{name}本轮出了传说阵眼，本轮不再挑战")
                self.turn -= 1
        self.turn += 1
        if self.chances > 0 and self.queue and not ctx.sleep(random.uniform(*BETWEEN), background=False):  # 回大厅、点下一个祖巫
            return self.over(BLOCKED, "已被用户停止", completed=True)
        return None

    def no_room(self, boss_id: int) -> TaskResult | None:
        """这个祖巫现有的房间都争不到前三 / 没有能进的：先打下一个；选的都这样就等别人把 Boss 打死、房间重开。"""
        ctx, names, entered = self.ctx, self.names, self.entered
        self.barren, self.turn = self.barren + 1, self.turn + 1
        if self.barren < len(self.queue):
            ctx.log(f"世界Boss：{names[boss_id]}" + (f"进了 {entered[boss_id]} 个房间都争不到前 {r.TOP}" if entered[boss_id] else "没有合适的房间") + f"，先看{names[self.queue[self.turn % len(self.queue)]]}")
            return None
        now = time.monotonic()
        minutes = WAIT_ROOMS // 60
        before = "；".join(f"{names[b]}进了 {entered[b]} 个房间都争不到前 {r.TOP}（最后一个：{self.last[b]}）" if entered[b] else f"{names[b]}没有合适的房间（要没满员、血量够）" for b in self.queue)
        if self.wait_end is None:
            self.wait_end = now + WAIT_ROOMS
            ctx.log(f"世界Boss：{before}，等 Boss 被打死后重开的新房间（最多等 {minutes} 分钟）")
        if now >= self.wait_end:
            return self.over(SKIPPED, f"{before}，等了 {minutes} 分钟也没等到能进前 {r.TOP} 的新房间" + ("" if self.fought else "，没有打"))
        if not self.ctx.sleep(min(nap(self.rooms, ctx.now) if len(self.queue) == 1 else POLL, self.wait_end - now), background=False):
            return self.over(BLOCKED, f"已被用户停止（{before}，正在等新房间）", completed=bool(self.fought))
        return None

    def run(self) -> TaskResult:
        while self.chances > 0 and self.queue:
            boss_id = self.queue[self.turn % len(self.queue)]
            tried = self.try_rooms(boss_id)
            if isinstance(tried, TaskResult):
                return tried
            outcome, note, room = tried
            if outcome == r.FAILED:
                return self.over(BLOCKED, f"{self.names[boss_id]}：{note}")
            if outcome == r.NO_REWARD:
                return self.over(BLOCKED, f"{self.names[boss_id]}：{note}；结算未成功，未按成功场次扣次数，未自动重打")
            stopped = self.won(boss_id, note, room) if outcome == r.DONE else self.no_room(boss_id)
            if stopped:
                return stopped
        return self.over(SKIPPED, "" if self.fought else "本轮没有完成世界 Boss 挑战")


@task(WORLD_ID, "世界Boss", reads={r.R_WORLD_LIST, r.R_LOOK}, writes={r.R_WORLD_JOIN, r.R_WORLD_HIT, r.R_WORLD_EXIT}, group="周常", modes=r.GEARS, mode_default=r.MID,
      times_max=MAX_FIGHTS, times_default=MAX_FIGHTS,  # 行上的“次数”：这次最多打几场（不超过剩下的挑战次数）
      auto=auto_chances, reserve=RESERVE_WEEK,  # 次数留给翅膀光环养成周：SpeedDevelop 的该周加成包含世界Boss传说精魄概率提升
      force_warning="会马上退出房间，这一场的伤害作废。",
      account_wide=True,  # 挑战次数全存档通用（用户 2026-10-11 说明）：一个登录账号只在选定的那个存档打，祖巫、档位、开关按登录账号存
      choices=Choices(world_bosses, "祖巫", available=available, toggles=((SKIP_GOLD, "本轮出传说阵眼后不再挑战该祖巫"),),
                      one_shot_toggles=(SKIP_GOLD,),
                      mode_inside=True, extra=("估伤", "boss_guess"), actions=(("选存档", "choose_world_boss_save"),),
                      note="多个祖巫：从三才往金轮着打，争不到前三换下一个，次数用完为止。\n"
                           "档位：低 = 正常输出；中 = 高低档自动切换保前三；高 = 按无限复活输出。\n"
                           "直接上报伤害，不耗道具；伤害值按 100–350 万战力、五行–三才祖巫实测，超出这个范围会有偏差。"))
def world_boss(ctx: TaskContext) -> TaskResult:
    gear = ctx.options.get("mode") if ctx.options.get("mode") in dict(r.GEARS) else r.MID
    names = dict(world_bosses())
    chosen = {state.num(v) for v in ctx.options.get("levels") or []}
    queue = [boss for boss, _ in reversed(world_bosses()) if boss in chosen]  # 从三才往金排（用户 2026-10-11 定）
    if not queue:
        return TaskResult(WORLD_ID, SKIPPED, "没有选择祖巫")
    if why := timeplan.closed(WORLD_ID, ctx.now):
        return TaskResult(WORLD_ID, SKIPPED, f"{why}，未挑战")
    try:
        info = ctx.refresh()  # 次数是账号共享状态，不能使用可能已经过期的登录快照
    except p.AllianceError as exc:
        return TaskResult(WORLD_ID, BLOCKED, f"刷新世界 Boss 状态失败：{exc}")
    level = role_level(info)
    profile = {PROFILE_KEY: level} if level is not None else None
    low = [boss for boss in queue if level is not None and boss_level(boss) is not None and level < boss_level(boss)]
    queue = [boss for boss in queue if boss not in low]
    if not queue:
        return TaskResult(WORLD_ID, SKIPPED, f"{names[low[0]]}需要角色 {boss_level(low[0])} 级，当前 {level} 级", profile)
    chances = _chances(info)
    if chances is None:
        return TaskResult(WORLD_ID, BLOCKED, "读不到世界 Boss 挑战次数（worldBoss.count），未进场")
    if chances <= 0:
        return TaskResult(WORLD_ID, SKIPPED, "没有世界 Boss 挑战次数")
    if ctx.options.get("plan") == "growth":  # 定时开着“按养成周”：不看次数框，按现有次数现算
        keep = state.growth_keep(ctx.now, online.growth_week(ctx.now), RESERVE_WEEK)
        if keep is None:
            return TaskResult(WORLD_ID, SKIPPED, f"翅膀光环养成周今天开始，游戏还没更新，{chances} 次全部留着")
        wanted = chances - keep
        if wanted <= 0:
            return TaskResult(WORLD_ID, SKIPPED, f"现有 {chances} 次，今天要留 {keep} 次给翅膀光环养成周，不打")
        if keep:
            ctx.log(f"现有 {chances} 次，留 {keep} 次给翅膀光环养成周，打 {wanted} 场")
    else:
        wanted = state.num(ctx.options.get("times", MAX_FIGHTS)) or 0
    if wanted <= 0:
        return TaskResult(WORLD_ID, SKIPPED, "次数填的 0，不打")
    return WorldRun(ctx, info, names, queue, gear, min(chances, wanted), level, profile).run()  # 行上填的次数：这次最多打这么多场
