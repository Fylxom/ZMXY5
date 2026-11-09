"""斗神大会（定时页）：开始匹配 → 遇到人机就直接结算，遇到真人就挂机，自动打到今日保护次数用完。

开放时间读 ``PvpTime``（按星期），不在时段内不匹配。流程对照客户端 PvpMatchProxy / PvpMatchMediator / ExitBattleServerCmd：

* ``pvpHandler.matchStart {blacklist:0}``：响应里有 ``robot`` = 匹配到人机。客户端会本地打一场再
  ``pvpHandler.robotPVPFinish {win, name, entityId}``；这里不打，直接按赢结算（entityId 取响应里人机的）。
* 没有 ``robot`` = 在排队。等 ``onPlayerJoinWaitingRoom`` → ``onPrepareCreateOnlineCopy``（上报加载 100%）→ ``onCreateOnlineCopy``
  （一次性战斗服务器地址）；连上去 ``enter``，退出等待房间，然后不操作角色（挂机），但照客户端每 10 秒
  ``copy.copyHandler.ping``（BattleMapBase.doInAdvanceTime）。**不提前走**：2026-10-04 真机上挂到一半就提交/断开
  被服务器记成“逃跑”（3 次后禁赛 5 分钟）。结束只有三种：服务器先推 ``onPVPFinish``/``onCopyAbnormal``（不再提交，
  之后再发 battleExit 会被踢）；帧里出现对手 ``connected=false``（真机：对手打死我方并结算后就会这样，服务器在等我方的结算 →
  用那一帧的帧号按“被打死”报输；报胜会得到 winType 2“战斗异常”，不报会等超时记逃跑）；帧号过 5670
  （客户端“时间到”→ 按输提交）。提交后留在战斗服务器上等 ``onPVPFinish``。

**保护次数**：上限 = VIP 表 ``pvpProtectNum`` + 翅膀光环周 ``Common.pvpProtectNum``，已用 = ``everyDaySetting.pvpProtectNum``
（进角响应里），每场真人局后 +1；剩余 0 就停止。积分达到 ``PvpGrade.failNumMacthAI>=999`` 的最低段位（14000 分）后匹配不到人机，直接拒绝。

**二转技能**：斗神大会不开放二转技能（``Common.pvpDisableSkills``）；出战角色的按键设置里带着就不匹配（见 ``banned_skills``）。

任何一步对不上（匹配超时、房间被解散、没给战斗服务器、服务器拒绝）都停止，不重试；排队中停下会先取消匹配。
**待真机确认**：对手退出后报输，能否得到正常的败（winType 0）而不是逃跑/异常。
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Mapping

from .. import data, dungeon, online, protocol as p, state
from . import match_start, team_room
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from ..online import R_ENTER, R_EXIT_BATTLE, R_EXIT_ROOM, R_KEYS, R_LOAD_DONE, R_LOADING

ID = "pvp_match"
R_START = "scene.pvpHandler.matchStart"
R_CANCEL = "scene.pvpHandler.matchCancel"
R_ROBOT = "scene.pvpHandler.robotPVPFinish"
R_PING = "room.waitingRoomHandler.ping"  # 等待房间里每 5 秒一次（客户端 PvpMatchMediator.loopTime）
R_BATTLE_PING = "copy.copyHandler.ping"
R_REPORT = "copyReport.copyReportHandler.getReport"
JOIN, PREPARE, CREATE = "onPlayerJoinWaitingRoom", "onPrepareCreateOnlineCopy", "onCreateOnlineCopy"
LEAVE, MATCH_RESULT, FINISH = "onPlayerLeaveWaitingRoom", "onPVPMatchResult", "onPVPFinish"
ERROR, ABNORMAL = "onCreateOnlineCopyError", "onCopyAbnormal"
MATCH_WAIT = 300.0    # 匹配最长等待（用户指定；PvpMatch 表最大放宽时间 120 秒）
CREATE_WAIT = 90.0    # 匹配到人之后等战斗服务器地址
TIME_LIMIT_FRAMES = 5670  # BattleMapBase：斗神大会 cliCurrFrame > 5670（30 帧/秒 ≈ 189 秒）判“时间到”
BATTLE_WAIT = 240.0   # 挂机最长等待（略长于时间上限）
FINISH_WAIT = 30.0    # 提交结算后留在战斗服务器上等 onPVPFinish
BATTLE_PING_GAP = 10.0
LOAD_WAIT = 90.0      # 进战斗服务器后等双方加载完（onAllLoadComplete）
PING_GAP = 5.0
FAIL = 4  # LevelResultParam
NO_ROBOT = 999  # PvpGrade.failNumMacthAI：连败多少场后才匹配人机；999 = 永远不匹配人机
WING_WEEK = 4  # state.GROWTH_WEEKS 的第 4 周“翅膀光环”：Common.pvpProtectActivityId 指的就是它（1971）


def open_now(now: int | None) -> bool | None:
    row = data.row("PvpTime", state.week_index(now)) or {}
    return state.in_windows(row.get("time"), now)


def room_id_of(body) -> str:
    return str(state.find(online.inner(body), "roomId", depth=4) or "")


def settle_robot(ctx: TaskContext, start, season: dict) -> str:
    robot = state.find(start, "robot", depth=3)
    if not isinstance(robot, dict):
        raise dungeon.Stop("人机数据格式不对，未结算")
    entity = robot.get("entityId") or state.find(robot, "entityId", depth=3)
    if entity in (None, ""):
        raise dungeon.Stop("人机数据里没有 entityId，未结算（客户端结算要带它）")
    try:
        done = ctx.request(R_ROBOT, {"win": 1, "name": robot.get("name", ""), "entityId": entity})
    except p.AllianceError as exc:
        raise dungeon.Stop(f"人机结算失败：{exc}") from exc
    ctx.log(f"斗神大会：人机结算响应 {str(done)[:300]}")
    absorb(season, done, False)
    return "人机，已结算"


def no_robot_score() -> int | None:
    """开始没有人机的最低积分（PvpGrade 里 failNumMacthAI >= 999 的最低段位；当前表是 19 级 14000 分）。"""
    rows = [r for r in data.table("PvpGrade").values() if isinstance(r, dict) and (state.num(r.get("failNumMacthAI")) or 0) >= NO_ROBOT]
    scores = [s for r in rows if (s := state.num(r.get("integral"))) is not None]
    return min(scores) if scores else None


def protect_cap(ctx: TaskContext) -> int:
    """每日保护次数上限 = VIP 表 ``pvpProtectNum``（0–2）+ 翅膀光环周的 Common.pvpProtectNum（1）。读不到 VIP 等级 / 表就停止。

    本周是不是翅膀光环周由 ``online.growth_week`` 判断（数据表 + 线上客户端版本）；判断不了就不加，宁可少打。
    """
    level = state.vip_level(ctx.info)
    vip = state.row("VIP", level) if level is not None else None
    base = state.num(vip.get("pvpProtectNum")) if vip else None
    bonus = state.num((state.row("Common", 1) or {}).get("pvpProtectNum"))
    if base is None or bonus is None:
        raise dungeon.Stop("读不到 VIP 等级或保护次数配置，未匹配")
    week = online.growth_week(ctx.now)
    extra = bonus if week == WING_WEEK else 0
    name = state.GROWTH_WEEKS[week - 1] if week else "无法确认"
    ctx.log(f"斗神大会：VIP {level} 保护 {base} 次；养成周 = {name}"
            + (f"，加 {bonus} 次" if extra else "，不加次数") + f"；上限 {base + extra}")
    return base + extra


def my_season(ctx: TaskContext) -> dict:
    """本地副本：进角响应的 ``pvpSeason``（积分等；``getMyPVPRank`` 是排行榜，不含这些）+ 今日保护上限和已用次数
    （``everyDaySetting.pvpProtectNum`` 是今日已用，和其他每日计数一样）。"""
    season = state.find(ctx.info, "pvpSeason", depth=3)
    if not isinstance(season, dict):
        ctx.log(f"斗神大会：进角响应里没有 pvpSeason（顶层字段：{sorted(ctx.info)[:60] if hasattr(ctx.info, 'keys') else type(ctx.info).__name__}）")
        raise dungeon.Stop("读不到斗神大会个人数据（进角响应里没有 pvpSeason），未匹配")
    daily = state.daily(ctx.info)
    used = state.num(daily.get("pvpProtectNum", 0)) if daily else None
    if used is None:
        ctx.log(f"斗神大会：进角响应里没有 everyDaySetting.pvpProtectNum：{str(daily)[:200]}")
        raise dungeon.Stop("读不到今日已用保护次数（进角响应里没有 everyDaySetting），未匹配")
    return {**season, "protectCap": protect_cap(ctx), "protectUsed": used}


def absorb(season: dict, reply, human: bool) -> None:
    """用一场的结算响应更新本地副本：响应带 pvpSeason 就以它为准，否则自己推算
    （人机 +addScore；真人局我们一定是输，已用保护 +1）。"""
    fresh = state.find(reply, "pvpSeason", depth=3)
    if isinstance(fresh, dict) and state.num(fresh.get("score")) is not None:
        season.update(fresh)
    elif not human:
        add, score = state.num(state.find(reply, "addScore", depth=3)), state.num(season.get("score"))
        if add is not None and score is not None:
            season["score"] = score + add
    if human:
        season["protectUsed"] += 1


class NoRobots(dungeon.Stop):
    """积分到顶：打过场次时是正常收工，不是失败。"""


def _unused(season: dict) -> int:
    return max(season["protectCap"] - season["protectUsed"], 0)


def protect_left(ctx: TaskContext, season: dict) -> int:
    """今天还剩几次“保护”（输了不掉分）；积分够高（没有人机可打）时直接停止。

    本脚本靠人机上分；``PvpGrade.failNumMacthAI`` 达到 999 的段位（14000 分起）永远匹配不到人机，只会遇到真人，
    所以那之后不再匹配。
    """
    score, limit = state.num(season.get("score")), no_robot_score()
    if score is not None and limit is not None and score >= limit:
        raise NoRobots(f"当前积分 {score} 已达到 {limit}，这个段位起匹配不到人机，只会遇到真人，不再匹配")
    return _unused(season)


def _store(info, record: str, name: str) -> list[dict]:
    """``clientStore`` 里 ``<当前存档id>-<name>`` 的值（同账号各存档的设置都在这一个表里，不能串；值是对象或 JSON 字符串）。"""
    found = []
    for key, value in info["clientStore"].items():
        if str(key) != f"{record}-{name}":
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise dungeon.Stop(f"读不懂按键设置 {key}，无法确认有没有带二转技能，未匹配") from None
        if isinstance(value, Mapping):
            found.append(value)
    return found


def banned_skills(ctx: TaskContext) -> list[str]:
    """出战角色带着的、斗神大会禁用的技能（二转技能）名字。对照客户端 SkillModelManager.hasDisableSkill：

    禁用名单是 ``Common.pvpDisableSkills``；出战的是 1 号角色，除非 ``setNotJoin.pNum == 1``（1 号不出战）；
    有“斗神按键设置”（``setKeyboard_ds-<n>``，非空）就只看它，否则看平时的按键设置（``setKeyboard-<n>``）。
    两个都没存过 = 只带默认技能，没有二转。读不到 clientStore / 名单就停止。
    """
    banned = {s.strip() for s in str((state.row("Common", 1) or {}).get("pvpDisableSkills") or "").split(";") if s.strip()}
    record = ctx.session.record_id
    if not record or not isinstance(ctx.info.get("clientStore"), Mapping) or not banned:
        raise dungeon.Stop("读不到存档 id、按键设置或斗神大会禁用技能名单，无法确认有没有带二转技能，未匹配")
    who = 1 if any(state.num(v.get("pNum")) == 1 for v in _store(ctx.info, record, "setNotJoin")) else 0
    keys = [v for v in _store(ctx.info, record, f"setKeyboard_ds-{who}") if v] or _store(ctx.info, record, f"setKeyboard-{who}")
    taken = {str(state.num(v)) for k in keys for v in k.values() if state.num(v) is not None} & banned
    ctx.log(f"斗神大会：存档 {record}，{who + 1} 号角色出战，按键设置 {keys}")
    names = data.slim("Skill", "name")  # 整张技能表 40MB，只要名字
    return sorted((names.get(s) or {}).get("name") or s for s in taken)


def cancel(ctx: TaskContext) -> None:
    try:
        ctx.request(R_CANCEL, {})
    except (p.AllianceError, UncertainOutcome):
        pass  # 取消失败不追究


def wait_match(ctx: TaskContext) -> tuple[str, online.Endpoint, dict]:
    """排队到战斗服务器地址出现。返回（房间号，地址，create 广播体）。"""
    routes = (JOIN, PREPARE, CREATE, LEAVE, MATCH_RESULT, ERROR, ABNORMAL)
    queue_end, room, ready_end, last_ping = time.monotonic() + MATCH_WAIT, "", None, 0.0
    while True:
        if ctx.stopped:
            raise dungeon.Stop("已被用户停止")
        got = ctx.poll_push(routes, 2.0)
        now = time.monotonic()
        if room and now - last_ping >= PING_GAP:  # 保持在等待房间里（客户端也是每 5 秒 ping）
            last_ping = now
            try:
                ctx.request(R_PING, {"roomId": room})
            except p.AllianceError:
                pass
        if got is None:
            if ready_end is not None and now > ready_end:
                raise dungeon.Stop("匹配到对手后一直没有战斗服务器地址")
            if ready_end is None and now > queue_end:
                raise dungeon.Stop(f"匹配 {MATCH_WAIT:.0f} 秒没有结果")
            continue
        route, body = got
        b = online.inner(body)
        ctx.log(f"斗神大会：收到 {route} {str(b)[:160]}")
        if route == MATCH_RESULT and state.num(b.get("code")) not in (None, 200):
            raise dungeon.Stop(f"匹配失败：{b.get('error', '服务器拒绝')}")
        if route == JOIN:
            room, ready_end = room_id_of(body) or room, now + CREATE_WAIT
        elif route == PREPARE:
            room = room_id_of(body) or room
            ready_end = ready_end or now + CREATE_WAIT
            ctx.notify(R_LOADING, {"roomId": room, "percent": 100, "version": 1})
        elif route == CREATE:
            where = online.endpoint(body)
            if where is None:
                raise dungeon.Stop("onCreateOnlineCopy 没有给出完整的 ip、port、instId")
            return room, where, online.body(body) or {}
        elif route == LEAVE and room_id_of(body) != room:
            continue  # 上一局的对手离开旧等待房间（结算后才推到）：和这次匹配无关
        elif route in (LEAVE, ERROR, ABNORMAL):
            raise dungeon.Stop(f"对局没有开成（{route}：{b.get('error', '')}）")


def exit_body(info: dict, uid: str, frame_num: int, killed: bool) -> dict:
    """ExitBattleServerCmd 的 battleExit 请求体，一律报输。killed=被对手打死（我方 hp 0、dead）；否则是“时间到”，没人死。"""
    players = info.get("playerInfos") if isinstance(info.get("playerInfos"), list) else []
    rows = []
    for i, pl in enumerate(players):
        if not isinstance(pl, dict):
            continue
        pid = pl.get("playerId", pl.get("uid", pl.get("userId")))
        dead = killed and str(pid) == uid
        rows.append({"playerId": pid, "hp": 0 if dead else (state.num(pl.get("hp")) or state.num(pl.get("hpMax")) or 1),
                     "isme": str(pid) == uid, "campId": i + 1, "dead": dead, "exit": False, "from": ""})
    return {"battleResult": FAIL, "playerInfo": rows, "data": [], "frameNum": max(1, frame_num)}


class Watch:
    """边挂机边看战斗服务器的推送：帧号、谁掉线（frames[].status 里 connected=false，客户端据此判“对方已逃跑”）。"""

    def __init__(self, ctx: TaskContext, per: int) -> None:
        self.ctx, self.per, self.count, self.frame, self.gone_frame = ctx, per, 0, 0, 0
        self.gone: set[int] = set()
        self.ops: dict[int, int] = {}
        self.seen: set[str] = set()

    def _read_frames(self, b) -> None:
        for f in b.get("frames") if isinstance(b.get("frames"), list) else []:
            if not isinstance(f, dict):
                continue
            for s in f.get("status") if isinstance(f.get("status"), list) else []:
                pos = state.num(s.get("battlePos")) if isinstance(s, dict) else None
                if pos is not None and s.get("connected") is False and pos not in self.gone:
                    self.gone.add(pos)
                    self.gone_frame = self.gone_frame or state.num(f.get("currFrame")) or self.frame
                    self.ctx.log(f"斗神大会：第 {self.frame} 帧，{pos} 号位掉线/退出")
            for op in f.get("playerOper") if isinstance(f.get("playerOper"), list) else []:
                who = state.num(op.get("playerIdIndex")) if isinstance(op, dict) else None
                if who is not None and op.get("keys"):
                    self.ops[who] = self.ops.get(who, 0) + 1

    def take(self, pushes) -> tuple[str, object] | None:
        terminal = None
        for m in pushes:
            route, raw = getattr(m, "route", None), getattr(m, "body", None)
            b = online.inner(raw)
            if route != "onSendFrame":
                if route not in self.seen:  # 没见过的推送记一次，方便真机核对
                    self.seen.add(route)
                    self.ctx.log(f"斗神大会：战斗服务器推送 {route} {str(b)[:200]}")
                if route in (FINISH, ABNORMAL) and terminal is None:
                    terminal = (route, raw)
                continue
            self.count += 1
            self.frame = max(self.frame, state.num(b.get("serFrameNum")) or self.count * self.per)
            self._read_frames(b)
        return terminal


def battle_ping(team, slot: int) -> None:
    """BattleMapBase.doInAdvanceTime：真人对局里每 10 秒 ping 一次战斗服务器，再把延迟用按键通道广播出去。

    响应只有 ``{time}``、没有 code（真机：按普通请求检查 code 会被当成“服务器拒绝”）；客户端也是有 time 才广播。
    """
    sent = time.time()
    back = team.ask(R_BATTLE_PING, {"time": int(sent * 1000)})
    if not isinstance(back, dict) or "time" not in back:
        return
    team.notify(R_KEYS, {"keys": [team_room.READY_KEY],
                         "objs": [{"ping": True, "pingTime": int((time.time() - sent) * 1000), "teamPs": slot}]})


def check_replay(ctx: TaskContext, report_id: str, sent_cg: str) -> None:
    """只读：取回刚才这场的录像，看服务器存的 cg。客户端回放要求录像 cg == 客户端当前版本，对不上显示“已过期！”。

    cg 每周二（偶尔周三）随客户端更新；换版本后没跟上，服务器会把 cg 存成空（真机 2026-10-04 证实）。
    这里只告警，不改任何东西、不重试；读不到就算了。
    """
    if not report_id:
        return
    try:
        got = ctx.request(R_REPORT, {"reportId": report_id})
    except (p.AllianceError, UncertainOutcome):
        return
    cg = state.find(got, "cg", depth=3)
    if cg == sent_cg:
        ctx.log(f"斗神大会：录像 {report_id} 的 cg 正常，回放应可打开")
    else:
        ctx.log(f"斗神大会：【警告】录像 {report_id} 的 cg={cg!r}，和进场时报的 {sent_cg!r} 不一致，回放会显示“已过期”。"
                "客户端可能刚更新了版本：重启本程序会重新识别线上版本")


def _enter_battle(ctx: TaskContext, team, where, uid: str, seat, room: str) -> int:
    """进战斗服务器 → 退等待房间 → 报加载完。返回每条帧推送代表的帧数（sendKeyFrameNum）。"""
    ctx.log(f"斗神大会：进战斗服务器使用 cg={seat.cg}（表目录推出的是 {online.DEFAULT_CG}）")
    entered = team_room.enter_battle(team, where, uid, seat.cg)
    ctx.log(f"斗神大会：已进战斗服务器，响应 {str(entered)[:120]}")
    team_room.leave_room(ctx, room)  # 客户端进战斗时就退等待房间
    team.notify(R_LOAD_DONE, {})  # 和别的在线副本一样：报加载完 → 等 onAllLoadComplete → 报准备
    return team_room.frame_step(entered)


def _real_end(event):
    """客户端对 code=200 的 onCopyAbnormal 直接忽略：这种推送当作没收到。"""
    if event is not None and event[0] == ABNORMAL and state.num(online.inner(event[1]).get("code")) == 200:
        return None
    return event


def _wait_loaded(ctx: TaskContext, team):
    """等双方加载完成。这期间对局已经结束就返回那条结束推送，否则 None；对手一直不加载完抛 Stop。"""
    ready_end = time.monotonic() + LOAD_WAIT
    while True:
        event = team.poll_push(("onAllLoadComplete", FINISH, ABNORMAL), 1.0)
        if event is not None:
            if event[0] == "onAllLoadComplete":
                return _real_end(ctx.poll_push((FINISH, ABNORMAL), 0.0))
            if _real_end(event) is not None:
                return event
            continue
        if ctx.stopped or time.monotonic() > ready_end:
            raise dungeon.Stop("对手一直没有加载完成，没有进入战斗")
        finished = _real_end(ctx.poll_push((FINISH, ABNORMAL), 0.0))
        if finished is not None:
            return finished


class _Idle:
    """挂机阶段的轮询：轮流读两条连接（保持心跳）、按客户端节奏 ping、收帧。"""

    def __init__(self, ctx: TaskContext, team, slot: int, watch: Watch) -> None:
        self.ctx, self.team, self.slot, self.watch = ctx, team, slot, watch
        self.last_ping = time.monotonic()

    def poll(self, wait: float):
        """返回有效的结束推送；没有返回 None。"""
        ctx, team = self.ctx, self.team
        got = ctx.poll_push((FINISH, ABNORMAL), wait) or team.poll_push((FINISH, ABNORMAL), wait)
        drained = self.watch.take(team.drain_pushes())
        if got is None:
            got = drained  # 终局推送可能刚好到达于 poll_push 与 drain_pushes 之间
        if time.monotonic() - self.last_ping >= BATTLE_PING_GAP:
            self.last_ping = time.monotonic()
            battle_ping(team, self.slot)
        return _real_end(got)


def _hang(ctx: TaskContext, idle: _Idle, slot: int, finished):
    """挂到对局自己结束为止，不提前走（提前走 = 逃跑）。返回 (结束推送, 对手是否已结束)。

    真机 2026-10-04：对手打死挂机的我方后提交结算，帧里随即出现对手 connected=false，服务器在等我方的结算；
    这时报胜 → 双方都报胜 → winType 2“战斗异常”；不报 → 等超时记逃跑。所以对手一退出就按“被打死”报输。
    """
    watch, won = idle.watch, False
    started = time.monotonic()
    end = started + BATTLE_WAIT
    while finished is None and not ctx.stopped and time.monotonic() < end:
        finished = idle.poll(0.5)
        if watch.gone - {slot}:
            won = True  # 这里表示“对手已结束”，提交的仍是输
            break
        if watch.frame > TIME_LIMIT_FRAMES:
            break
    took = time.monotonic() - started
    ctx.log(f"斗神大会：挂机 {took:.0f} 秒，到第 {watch.frame} 帧（收到 {watch.count} 条帧推送），"
            f"各座位有操作的帧数 {watch.ops}，掉线座位 {sorted(watch.gone)}")
    return finished, won


def _submit_loss(ctx: TaskContext, idle: _Idle, info: dict, uid: str, won: bool):
    """按输提交结算，然后留在战斗服务器上等结束推送。返回等到的推送；没等到为 None。"""
    ctx.log(f"斗神大会：{'对手已结束对局，按被打死' if won else '时间到，按输'}提交结算")
    idle.team.request(R_EXIT_BATTLE, exit_body(info, uid, idle.watch.gone_frame or idle.watch.frame, won))
    # 客户端提交后留在战斗服务器上等 onPVPFinish；提交完立刻断开会被当成掉线
    finished = None
    wait_end = time.monotonic() + FINISH_WAIT
    while finished is None and not ctx.stopped and time.monotonic() < wait_end:
        finished = idle.poll(0.5)
    if finished is not None:
        ctx.log(f"斗神大会：收到 {finished[0]} {str(online.inner(finished[1]))[:400]}")
    return finished


def fight_human(ctx: TaskContext, uid: str, season: dict) -> str:
    room = ""
    try:
        try:
            room, where, info = wait_match(ctx)
        except dungeon.Stop:
            cancel(ctx)
            raise
        ctx.engage(True)  # 匹配到真人了：从这里起点“停止”要问打完再停还是立即停
        seat = online.seat(info, int(uid), 1)
        if seat is None:
            raise dungeon.Stop("onCreateOnlineCopy 没有给出本角色在 playerInfos 里唯一的位置，不进战斗服务器")
        seat = replace(seat, cg=online.client_cg(ctx.now))  # 客户端无条件发自己的 CurrCg（回放要求录像 cg == 回放者客户端的 cg）
        with ctx.open_team(where.host, where.port) as team:
            per = _enter_battle(ctx, team, where, uid, seat, room)
            finished = _wait_loaded(ctx, team)
            ctx.log("斗神大会：双方加载完成，开始挂机" if finished is None else "斗神大会：加载阶段对局已结束")
            if finished is None:
                team_room.send_ready(team, seat.slot)
            idle = _Idle(ctx, team, seat.slot, Watch(ctx, per))
            finished, won = _hang(ctx, idle, seat.slot, finished)
            if finished is not None:
                # 服务器已经给出结果（真机：这之后再发 battleExit 会被踢出会话），不再提交
                ctx.log(f"斗神大会：收到 {finished[0]}，不再提交结算：{str(online.inner(finished[1]))[:400]}")
            else:
                finished = _submit_loss(ctx, idle, info, uid, won)
    except p.AllianceError as exc:
        team_room.leave_room(ctx, room)
        raise dungeon.Stop(f"真人对战中断：{exc}") from exc
    if finished is None:
        raise UncertainOutcome(
            "真人局已提交 battleExit，但 30 秒内没有收到 onPVPFinish；结算结果未知，停止匹配，请核对游戏内积分和保护次数"
        )
    if finished[0] == ABNORMAL:
        raise dungeon.Stop(
            f"真人局收到 onCopyAbnormal（{str(online.inner(finished[1]))[:240]}），没有确认比赛结算；停止匹配并核对积分和保护次数"
        )
    done = online.inner(finished[1])
    absorb(season, done, True)  # 真人局我们一定是输；服务器结算推送带 pvpSeason 就以它为准
    check_replay(ctx, str(done.get("reportId") or ""), seat.cg)
    return "真人，对局已由服务器结算（详见日志里的 onPVPFinish）"


def one_match(ctx: TaskContext, uid: str, season: dict) -> str:
    online.client_cg(ctx.now)  # 匹配前先把当前版本读好（缓存 1 小时，过期要重新联网）：不能匹配到人了才去读
    start = match_start.start(ctx, R_START, {"blacklist": 0})  # 逃跑禁赛抛 Banned：交给执行器过几分钟再重跑
    ctx.log(f"斗神大会：已发起匹配，响应 {str(start)[:160]}")
    if state.find(start, "robot", depth=3):
        return settle_robot(ctx, start, season)
    try:
        return fight_human(ctx, uid, season)
    finally:
        ctx.engage(False)


def _matches(ctx: TaskContext, uid: str, season: dict, notes: list[str]) -> tuple[str, bool]:
    """一场接一场打，每场结果记进 notes。返回 (停止原因, 是不是中途出问题)。"""
    while True:
        if ctx.stopped or ctx.winding:
            return "已被用户停止" if ctx.stopped else "已按要求打完这一场后停止", False
        try:
            left = protect_left(ctx, season)  # 每场前按最新结算更新：输一场用掉一次，打人机不会
            ctx.log(f"斗神大会：今日剩余保护次数 {left}")
            if left <= 0:
                return "今日保护次数已用完，停止", False
            before = season["protectUsed"], state.num(season.get("score"))
            notes.append(one_match(ctx, uid, season))
        except NoRobots as stop:
            return str(stop), False
        except dungeon.Stop as stop:
            return str(stop), True
        ctx.log(f"斗神大会第 {len(notes)} 场：{notes[-1]}；今日剩余保护次数 {_unused(season)}")
        if before == (season["protectUsed"], state.num(season.get("score"))):
            return "本场结算后积分和保护次数都没有变化，不再继续匹配", True


@task(ID, "斗神大会", reads={R_REPORT},
      writes={R_START, R_CANCEL, R_ROBOT, R_LOADING, R_EXIT_ROOM, R_PING, R_ENTER, R_EXIT_BATTLE, R_BATTLE_PING, R_LOAD_DONE, R_KEYS}, group="周常",
      long_running=True,
      force_warning="如果正在打真人局，中途断开会被服务器记一次逃跑（累计 3 次禁赛 5 分钟），这一场也不会结算。")
def run(ctx: TaskContext) -> TaskResult:
    uid = state.num(ctx.info.get("uid"))
    if uid is None or uid <= 0:
        return TaskResult(ID, BLOCKED, "进角响应缺少角色 uid，未匹配")
    opened = open_now(ctx.now)
    if opened is None:
        return TaskResult(ID, BLOCKED, "读不到服务器时间或开放时间表，未匹配")
    if not opened:
        return TaskResult(ID, SKIPPED, "不在斗神大会开放时间（12:00-14:00、19:00-21:00）")
    try:
        skills = banned_skills(ctx)
        if skills:
            return TaskResult(ID, BLOCKED, f"带了二转技能（{'、'.join(skills)}），斗神大会不开放二转技能，请先换下再匹配")
        season = my_season(ctx)
    except dungeon.Stop as stop:
        return TaskResult(ID, BLOCKED, str(stop))
    notes: list[str] = []
    try:
        why, incomplete = _matches(ctx, str(uid), season, notes)
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"已完成 {len(notes)} 场；{exc}") from exc
    except match_start.Banned as ban:  # 逃跑禁赛：先做后面的任务，过几分钟再来（重跑时重读保护次数）
        return ban.result(ID, f"已完成 {len(notes)} 场" if notes else "")
    # 每一场的结果当场已经写过日志，这里只写场数
    status = SUBMITTED if notes and not incomplete else BLOCKED
    return TaskResult(ID, status, f"完成 {len(notes)} 场；{why}" if notes else why)
