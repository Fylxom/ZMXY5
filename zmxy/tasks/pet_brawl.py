"""灵宠乱斗（定时页）：开始匹配后挂机，自动打到今日乱斗币拿满（``petBattleMoneyDayNum``，现为 20）就停。

开放时间读 ``OpenTime``（``PetBattleHelper.openTimeType`` + 星期），不在时段内不匹配。流程对照客户端 PetWarProxy / PetBrawlManager：

* ``petPvpHandler.matchStart {}`` → 服务器推 ``onPrepareCreateOnlineCopy``（房间号）→ 客户端加载完发一次
  ``waitingRoomHandler.changePlayerStatus {roomId, status:4}``（准备好）；
* 之后服务器按回合推 ``onPetPvpBtlChangeState``：``btlStage`` 0 = 摆阵（选择最低战力宠物），
  1 = 加载（每 2 秒发 ``petPvpRoomHandler.loadingEnd {roomId}`` 直到进战斗），2 = 战斗。
  战斗是双方客户端各自本地演算再报 ``setBtlResult {roomId, btlResult, hps}``；**这里不演算，每一局按输上报**
  （自己 0 血、对手满血；不能没打就报赢）。一方输 3 局或打满 5 局才结算。
  **这是试验做法**：若对手赢了而我们报输这类结果被服务器当异常，就得改成真的演算战斗；
* ``onPetPVPFinish`` = 一场结束，里面有 ``addPetBtlM``（本场乱斗币）和刷新后的 ``everyDaySetting``，据此判断是否到上限。

出战宠物、宝符可以事先在游戏里配好；没配时服务器随机。匹配超时最多重试 ``MATCH_RETRY_MAX`` 次（不算场次）；其他服务器拒绝、整场超时仍停止。排队中停下会先取消匹配。
例外（真机 2026-10-09）：对手中途退出时，上报会被拒绝“战斗已完成”、没有结算推送、也不给乱斗币——这一场不算次数，重新开一局（一次执行最多补 ``REPLAY_MAX`` 局）。
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from dataclasses import replace

from .. import data, dungeon, online, protocol as p, state
from . import match_start
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from ..taskdef import is_kicked
from ..online import R_STATUS as R_READY

ID = "pet_brawl"
R_START = "scene.petPvpHandler.matchStart"
R_CANCEL = "scene.petPvpHandler.matchCancel"
R_LOADED = "room.petPvpRoomHandler.loadingEnd"
R_RESULT = "room.petPvpRoomHandler.setBtlResult"
R_SET_PET = "room.petPvpRoomHandler.setPetPvpWaitPet"  # 摆阵阶段换上阵宠物 {roomId, index}
R_INFO = "scene.petPvpHandler.getPetPVPInfo"  # 客户端打开灵宠乱斗界面时先发，服务器据此初始化
PREPARE, STAGE, FINISH = "onPrepareCreateOnlineCopy", "onPetPvpBtlChangeState", "onPetPVPFinish"
MATCH_RESULT, EXIT, ABNORMAL = "onPVPMatchResult", "onPetPvpBtlExit", "onCopyAbnormal"
ROUTES = (PREPARE, STAGE, FINISH, MATCH_RESULT, EXIT, ABNORMAL)
PET_BRAWL_DUNGEONS = {2000101, 2000201}  # v4760 LevelConfigVO.isPetBrawl()
KEY = "addPetPvpM"      # everyDaySetting：今日已得乱斗币
MATCH_WAIT = 300.0      # 匹配最长等待（用户指定）
MATCH_RETRY_MAX = 2     # 每场匹配超时后最多重试次数（加上首次，最多匹配 3 次）
MATCH_TOTAL = 1200.0    # 一场（含 5 回合）最长时间
LOAD_DELAY = 4.0        # 模拟加载耗时：收到推送后这么久才上报
FIGHT_DELAY = 6.0       # 进入每一局后这么久上报结果（客户端打完还要等 2.6 秒）
LOSE = 4                # setBtlResult.btlResult：5 赢 / 4 输 / 2 平
ACK_GAP = 2.0           # loadingEnd 间隔（客户端 setInterval 2000）
REPLAY_MAX = 3          # 对手中途退出、没给币的场不算次数，重新开一局；一次执行最多补这么多局
KICK_RETRY_MAX = 2      # 对局中被服务器踢下线：重新登录接着打，一次执行最多这么多次
KICK_WAIT = 30.0        # 被踢后等这么久再登录（立刻登录容易撞上服务器还在清理这一场）


class MatchTimeout(dungeon.Stop):
    """仅未匹配到对手时的超时可重试；本地等满时先确认取消排队成功。"""

    def __init__(self, message: str, *, server_timeout: bool = False) -> None:
        super().__init__(message)
        self.server_timeout = server_timeout


class Fled(Exception):
    """上报时服务器说“战斗已完成”：这一场已经结束（真机：对手中途退出），没有结算推送。参数 = 打到第几回合。"""


def helper() -> dict:
    return data.row("PetBattleHelper", 1) or {}


def cap_of() -> int | None:
    n = state.num(helper().get("petBattleMoneyDayNum"))
    return n if n and n > 0 else None


def open_now(now: int | None) -> bool | None:
    kind = state.num(helper().get("openTimeType"))
    if kind is None or now is None:
        return None
    row = data.row("OpenTime", kind + state.week_index(now)) or {}
    return state.in_windows(row.get("time"), now)


def cancel(ctx: TaskContext) -> None:
    try:
        ctx.request(R_CANCEL, {})
    except (p.AllianceError, UncertainOutcome):
        pass  # 取消失败不追究


def watchdog(ctx: TaskContext, ident: int, beat: list[float], done: threading.Event) -> None:
    """调试用：循环 20 秒没走动（卡在某个调用里）就把工作线程的调用栈写进日志，看清卡在哪。"""
    while not done.wait(5.0):
        if time.monotonic() - beat[0] > 20.0:
            frame = sys._current_frames().get(ident)
            ctx.log("灵宠乱斗：疑似卡住，调用栈：" + " | ".join(
                f"{x.name}:{x.lineno}" for x in traceback.extract_stack(frame)[-6:]) if frame else "灵宠乱斗：疑似卡住（取不到调用栈）")
            beat[0] = time.monotonic()


def _is_me(player, uid: str) -> bool:
    return isinstance(player, dict) and str(player.get("uid", player.get("playerId"))) == uid


def seats(players, uid: str) -> str:
    """排查用：各座位是谁、上阵/手牌下标、各宠物剩余血量（推送里有的话）。"""
    out = []
    for pl in players if isinstance(players, list) else []:
        if not isinstance(pl, dict):
            continue
        pets = pl.get("btlPets") if isinstance(pl.get("btlPets"), list) else []
        hp = [x.get("hp") for x in pets if isinstance(x, dict) and "hp" in x]
        who = "我" if _is_me(pl, uid) else f"对手{pl.get('playerId', pl.get('uid'))}"
        out.append(f"{who} 上阵{pl.get('onBtl')} 手牌{pl.get('btlCards')} 加载{pl.get('loading')}" + (f" 血{hp}" if hp else ""))
    return "；".join(out) or "推送里没有座位"


def choose_pet(players, uid: str) -> tuple[int | None, str]:
    """摆阵阶段选战力最低的上阵。返回（要换上的 btlPets 下标；不用换/不选为 None，原因）。
    可选的宠物 = 手牌 btlCards + 已上阵 onBtl（客户端 PetBrawlUI.showPet）；战力都相同就不选，交给系统随机。"""
    rows = players if isinstance(players, list) else []
    me = next((pl for pl in rows if _is_me(pl, uid)), None)
    if me is None:
        return None, "摆阵推送里找不到自己"
    pets = me.get("btlPets") if isinstance(me.get("btlPets"), list) else []
    on = [i for i in (me.get("onBtl") or []) if isinstance(i, int) and i >= 0]
    cards = [i for i in (me.get("btlCards") or []) if isinstance(i, int) and i >= 0 and i not in on]
    power = {}
    for i in on + cards:
        value = state.num(pets[i].get("fightPower")) if i < len(pets) and isinstance(pets[i], dict) else None
        if value is None:
            return None, f"宠物 {i} 读不到战力，交给系统"
        power[i] = value
    if len(set(power.values())) <= 1:
        return None, f"可选宠物战力相同（{sorted(power.values())}），交给系统随机"
    low = min(power.values())
    if on and power[on[0]] == low:
        return None, f"已上阵的宠物战力最低（{low}），不换"
    pick = next(i for i in cards if power[i] == low)
    return pick, f"换上战力最低的宠物 {pick}（战力 {low}，可选 {sorted(power.values())}）"


def loss_hps(players, uid: str) -> list[float]:
    """剩余血量比例，顺序同推送里的 players（客户端 fightEnd 的 hps 就是这个顺序）：自己 0，对手 1。认不出返回空。"""
    rows = players if isinstance(players, list) else []
    hps = [0.0 if _is_me(pl, uid) else 1.0 for pl in rows]
    return hps if len(hps) == 2 and hps.count(0.0) == 1 else []


class _Match:
    """一场对局进行中的状态：房间、打到第几局、到点要做的上报。one_match 的循环在“定时上报”和“处理推送”之间共用它。"""

    def __init__(self, ctx: TaskContext, uid: str, start: float) -> None:
        self.ctx, self.uid, self.start = ctx, uid, start
        self.connection_lost = False
        self.room, self.matched, self.rounds, self.last_push = "", False, 0, start
        self.ready_at = self.ack_at = self.report_at = None  # 到点要做的事：报准备好 / 报加载完成 / 报本局结果
        self.players, self.session = None, None
        self.stage, self.last_step = None, "匹配请求已响应，等待进房推送"

    def report(self, now: float) -> None:
        """到点的上报，一次最多做一件。服务器拒绝时抛 AllianceError，由 one_match 处理。"""
        ctx, room = self.ctx, self.room
        if self.ready_at is not None and now >= self.ready_at:  # 客户端加载完发一次 status 4
            self.ready_at = None
            ctx.log("灵宠乱斗：发送“准备好”，等服务器响应…")
            self.last_step = "准备好请求待响应"
            reply = ctx.request(R_READY, {'roomId': room, 'status': 4})
            self.last_step = f"准备好已响应（code={reply.get('code')}），等待推送"
            ctx.log(f"灵宠乱斗：上报准备好，响应 {str(reply)[:120]}")
        elif self.ack_at is not None and now >= self.ack_at:  # 加载阶段每 2 秒报一次，直到进入战斗
            self.ack_at = now + ACK_GAP
            self.last_step = "加载完成请求待响应"
            reply = ctx.request(R_LOADED, {'roomId': room})
            self.last_step = f"加载完成已响应（code={reply.get('code')}），等待推送"
            ctx.log(f"灵宠乱斗：上报加载完成，响应 {str(reply)[:120]}")
        elif self.report_at is not None and now >= self.report_at:
            self.report_at = None
            hps = loss_hps(self.players, self.uid)
            if not hps:
                raise dungeon.Stop("战斗推送里认不出自己和对手，未上报本局结果")
            body = {"roomId": room, "btlResult": LOSE, "hps": hps}
            ctx.log(f"灵宠乱斗：第 {self.session or self.rounds} 局，按输上报 {body}")
            self.last_step = f"第{self.session or self.rounds}局结果请求待响应"
            reply = ctx.request(R_RESULT, body)
            self.last_step = f"第{self.session or self.rounds}局结果已响应（code={reply.get('code')}），等待推送"
            ctx.log(f"灵宠乱斗：第 {self.session or self.rounds} 局上报响应 {str(reply)[:120]}")

    def on_push(self, route: str, b: dict, now: float) -> tuple[str, int | None] | None:
        """处理一条推送。对局结算了返回 one_match 的结果，否则 None。"""
        if route == MATCH_RESULT and state.num(b.get("code")) not in (None, 200):
            if not self.matched and "匹配超时" in str(b.get("error", "")):
                raise MatchTimeout(f"匹配失败：{b['error']}", server_timeout=True)
            raise dungeon.Stop(f"匹配失败：{b.get('error', '服务器拒绝')}")
        if route == PREPARE:
            dungeon_id = state.num(state.find(b, "dungeonId", depth=3))
            if dungeon_id not in PET_BRAWL_DUNGEONS:
                self.ctx.log(f"灵宠乱斗：忽略非灵宠乱斗的进房推送（dungeonId={dungeon_id}）")
                return None
            self.room = str(state.find(b, "roomId", depth=3) or self.room)
            if not self.room:
                raise dungeon.Stop("onPrepareCreateOnlineCopy 没有 roomId，无法继续")
            self.matched, self.ready_at = True, now + LOAD_DELAY
        elif route == STAGE:
            if not self.matched or not self.room:
                self.ctx.log("灵宠乱斗：忽略未对应到本场房间的阶段推送")
                return None
            self._on_stage(b, now)
        elif route in (ABNORMAL, EXIT):
            raise dungeon.Stop(f"对局中止（{route}：{b.get('error', '')}）")
        elif route == FINISH:
            return self._on_finish(b)
        return None

    def _on_finish(self, b: dict) -> tuple[str, int | None]:
        if not self.matched or not self.room:
            raise dungeon.Stop("尚未进入本场房间就收到结算，可能是上一场延迟推送；未计为本场完成，不自动重试")
        if state.num(b.get("code")) not in (None, 200):
            raise dungeon.Stop(f"结算带错误：{b.get('error', '')}")
        add = state.num(b.get("addPetBtlM"))
        notice = b.get("error")
        if notice:
            # 客户端先结算/更新次数，再显示 error 提示；实测对方逃跑也可奖励10币。
            # 仅接受这一已知提示且有完整胜场回执；其他异常仍停止。
            if (notice != "对方已逃跑" or state.num(b.get("winType")) != 1
                    or not isinstance(b.get("reportId"), str) or not b["reportId"].strip()
                    or add is None or add < 0):
                raise dungeon.Stop(f"结算带错误：{notice}")
            self.ctx.log(f"灵宠乱斗：结算提示“{notice}”，回执乱斗币+{add}；将刷新今日币数核验")
            # 不用 got+add 推算，也不把带提示的旧计数当成刷新结果。
            # 返回 None 让调用方重新进角，只有确认增长后才开始下一场。
            return f"打了 {self.rounds} 回合，对方已逃跑，回执乱斗币+{add}", None
        total = state.used(b, KEY)
        return f"打了 {self.rounds} 回合，乱斗币+{add if add is not None else '?'}", total

    def _on_stage(self, b: dict, now: float) -> None:
        """阶段推送：0 摆阵（选战力最低的宠物）/ 1 加载（开始定时报加载完成）/ 2 进入战斗（定时按输上报）。"""
        ctx = self.ctx
        self.matched = True
        stage = state.num(b.get("btlStage"))
        self.stage = stage
        self.players = b.get("players") or self.players
        self.session = state.num(b.get("session")) or self.session
        self.ack_at = now + LOAD_DELAY if stage == 1 else None
        ctx.log(f"灵宠乱斗：阶段 {stage}，第 {self.session} 局，{seats(self.players, self.uid)}，倒计时 {b.get('time')}，已赢 {b.get('btlWin')}")
        if stage == 0:  # 送分玩法：每局摆阵都选战力最低的宠物
            pick, why_pick = choose_pet(self.players, self.uid)
            ctx.log(f"灵宠乱斗：第 {self.session or self.rounds + 1} 局摆阵：{why_pick}")
            if pick is not None:
                try:
                    self.last_step = f"第{self.session or self.rounds + 1}局换宠请求待响应（index={pick}）"
                    reply = ctx.request(R_SET_PET, {"roomId": self.room, "index": pick})
                    self.last_step = f"第{self.session or self.rounds + 1}局换宠已响应（index={pick}，code={reply.get('code')}），等待推送"
                    ctx.log(f"灵宠乱斗：{self.last_step}")
                except p.AllianceError as exc:
                    self.last_step = f"换宠被拒绝（{exc}），等待推送"
                    ctx.log(f"灵宠乱斗：换宠被拒绝（{exc}），交给系统")
        if stage == 2:
            self.rounds, self.report_at = self.rounds + 1, now + FIGHT_DELAY
            ctx.log(f"灵宠乱斗：进入第 {self.session or self.rounds} 局战斗")

    def wait(self, beat: list[float]) -> tuple[str, int | None]:
        """定时上报、处理推送，直到这一场结算。"""
        ctx, start = self.ctx, self.start
        while True:
            if ctx.stopped:
                raise dungeon.Stop("已被用户停止")
            ctx.engage(self.matched)  # 匹配到了才算正打着：还在匹配时点“停止”直接停
            now = beat[0] = time.monotonic()
            if not self.matched and now - start > MATCH_WAIT:
                raise MatchTimeout(f"匹配 {MATCH_WAIT:.0f} 秒没有结果")
            if now - start > MATCH_TOTAL:
                raise dungeon.Stop(f"一场超过 {MATCH_TOTAL / 60:.0f} 分钟还没结算，停止")
            try:
                self.report(now)
            except p.AllianceError as exc:
                self.ready_at = self.ack_at = self.report_at = None  # 服务器不再需要（客户端也是收到 500 就停）
                ctx.log(f"灵宠乱斗：上报被拒绝（{exc}）")
                if "已完成" in str(exc):  # 这场已经结束：不用再等结算推送，调用方重读今日乱斗币
                    raise Fled(self.rounds) from exc
            try:
                got = ctx.poll_push(ROUTES, 1.0)
            except p.AllianceError as exc:
                self.connection_lost = True
                phase = "对局中断" if self.matched else "匹配中断"
                raise dungeon.Stop(f"{phase}：{exc}；等待推送，房间{self.room or '未进房'}，第{self.session or 0}局，阶段{self.stage}；最后步骤：{self.last_step}") from exc
            if got is None:
                if now - self.last_push >= 30:
                    self.last_push = now
                    ctx.log(f"灵宠乱斗：{'对局中' if self.matched else '匹配中'}，等待服务器推送（开始后 {now - start:.0f} 秒）")
                continue
            route, body = got
            b = online.inner(body)
            self.last_push = now
            ctx.log(f"灵宠乱斗：收到 {route}（开始后 {now - start:.0f} 秒）{str(b)[:160]}")
            result = self.on_push(route, b, now)
            if result is not None:
                return result


def one_match(ctx: TaskContext, uid: str) -> tuple[str, int | None]:
    """打一场（挂机：每一局都按输上报，输满 3 局服务器就结算）。返回（说明，今日已得乱斗币；响应里没有为 None）。"""
    reply = match_start.start(ctx, R_START, {})  # 逃跑禁赛抛 Banned：交给执行器过几分钟再重跑
    ctx.log(f"灵宠乱斗：已发起匹配，响应 {str(reply)[:160]}，等待服务器推送…")
    start = time.monotonic()
    beat, done = [start], threading.Event()
    threading.Thread(target=watchdog, args=(ctx, threading.get_ident(), beat, done), daemon=True).start()
    m = _Match(ctx, uid, start)
    try:
        return m.wait(beat)
    except MatchTimeout as exc:
        if not exc.server_timeout:  # 服务器明确超时已结束排队；本地超时要先确认取消，避免重复排队
            try:
                ctx.request(R_CANCEL, {})
            except p.AllianceError as cancel_error:
                raise dungeon.Stop(f"{exc}；取消匹配失败：{cancel_error}，不再重试") from cancel_error
        raise
    except dungeon.Stop:
        if not m.matched and not m.connection_lost:
            cancel(ctx)
        raise
    except p.AllianceError as exc:
        raise dungeon.Stop(f"对局中断：{exc}") from exc
    except UncertainOutcome as exc:
        if is_kicked(exc):  # 踢出包和请求撞在一起：这一场反正作废，按被踢处理（可以重新登录接着打）
            raise dungeon.Stop(f"对局中断：{exc}") from exc
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:  # 非协议类错误也写进日志，别悄悄死掉
        ctx.log(f"灵宠乱斗：意外错误 {type(exc).__name__}: {exc} @ {traceback.extract_tb(exc.__traceback__)[-1].lineno}")
        raise dungeon.Stop(f"意外错误：{exc}") from exc
    finally:
        ctx.engage(False)
        done.set()


class _Day:
    """今天这轮乱斗：一场一场打到币数拿满，攒着每场说明和停止原因。"""

    def __init__(self, ctx: TaskContext, uid: str, got: int, cap: int, coins: list) -> None:
        self.ctx, self.uid, self.got, self.cap, self.coins = ctx, uid, got, cap, coins
        self.notes: list[str] = []
        self.why, self.incomplete = None, False
        self.replays = 0  # 对手中途退出、没给币而重开的局数
        self.match_retries = 0  # 当前这一场的匹配超时重试次数，匹配成功后重置
        self.kicks = 0  # 被服务器踢下线后重新登录的次数

    def rejoin(self, why: str) -> bool:
        """对局中被踢下线（真机 10-30、11-08 都在后面一场第 2 局摆阵后，原因见 LESSONS.md）：这一场作废，
        等 KICK_WAIT 秒重新登录，按新的进角信息重读乱斗币，接着匹配。服务器多半记一次逃跑，攒到 3 次的禁赛由 match_start 处理。"""
        ctx = self.ctx
        self.kicks += 1
        ctx.log(f"灵宠乱斗：{why}；这一场作废，{KICK_WAIT:.0f} 秒后重新登录接着打（第 {self.kicks}/{KICK_RETRY_MAX} 次）")
        if not ctx._rest(KICK_WAIT) or ctx.stopped:  # 连接已断，不能用 ctx.sleep（它看到断线就立刻返回）：干等
            return self.stop("已被用户停止", incomplete=False)
        if ctx.stopped:
            return self.stop("已被用户停止", incomplete=False)
        try:
            info = ctx.session.relogin()
        except Exception as exc:  # 登录失败：停下，不再试
            return self.stop(f"{why}；重新登录失败：{exc}")
        total = state.used(info, KEY)
        if total is None:
            return self.stop(f"{why}；重新登录后读不到今日乱斗币，不再继续")
        self.got = total
        self.coins[0] = total
        ctx.log(f"灵宠乱斗：已重新登录，今日乱斗币 {total}/{self.cap}")
        return True

    def stop(self, why: str, incomplete: bool = True) -> bool:
        self.why, self.incomplete = why, incomplete
        return False

    def play(self) -> bool:
        """打一场，返回要不要接着打。"""
        ctx = self.ctx
        if ctx.stopped or ctx.winding:
            return self.stop("已被用户停止" if ctx.stopped else "已按要求打完这一场后停止", incomplete=False)
        fled = None
        try:
            note, total = one_match(ctx, self.uid)
        except MatchTimeout as exc:
            if ctx.stopped or ctx.winding:
                return True  # 回到开头处理停止，不再发起匹配
            if self.match_retries >= MATCH_RETRY_MAX:
                return self.stop(f"{exc}；已重试匹配 {self.match_retries} 次，不再重试")
            self.match_retries += 1
            ctx.log(f"灵宠乱斗：{exc}，重新匹配（第 {self.match_retries}/{MATCH_RETRY_MAX} 次重试）")
            return True
        except Fled as exc:
            fled, note, total = exc, f"对手中途退出（{exc.args[0]} 回合）", None
        except dungeon.Stop as stop:
            if is_kicked(str(stop)) and self.kicks < KICK_RETRY_MAX and not (ctx.stopped or ctx.winding):
                return self.rejoin(str(stop))
            return self.stop(str(stop), incomplete=str(stop) != "已被用户停止")
        self.match_retries = 0
        if total is None:  # 结算里没带刷新后的次数：重读角色信息
            try:
                total = state.used(ctx.refresh(), KEY)
            except p.AllianceError:
                total = None
        if fled and total is not None and total <= self.got:  # 对手跑了、这一场没给币：不算次数，重新开一局
            if self.replays >= REPLAY_MAX:
                return self.stop(f"对手中途退出、没给币的已经重开 {self.replays} 局，不再重开")
            self.replays += 1
            ctx.log(f"灵宠乱斗：{note}，这一场没给乱斗币，不算次数，重新开一局（第 {self.replays} 次重开）")
            return True
        self.notes.append(note)
        self.coins[0] = total if total is not None else "?"
        ctx.log(f"灵宠乱斗第 {len(self.notes)} 场：{note}")
        if total is None:
            return self.stop("读不到刷新后的今日乱斗币，不再继续")
        gained, self.got = total > self.got, total
        if self.got >= self.cap:
            return False
        if not gained:
            return self.stop("这一场没有增加乱斗币（可能被判弃权），不再继续；请对照游戏里的结算")
        return True


def _gate(ctx: TaskContext) -> tuple[int, int, int] | TaskResult:
    """开打前必须读准的状态：(上限, 今日已得, uid)；读不到或不用打返回 TaskResult。"""
    cap = cap_of()
    got = state.used(ctx.info, KEY)
    uid = state.num(ctx.info.get("uid"))
    if uid is None or uid <= 0:
        return TaskResult(ID, BLOCKED, "进角响应缺少角色 uid，未匹配")
    if cap is None:
        return TaskResult(ID, BLOCKED, "读不到每日乱斗币上限（PetBattleHelper.petBattleMoneyDayNum），未匹配")
    if got is None:
        return TaskResult(ID, BLOCKED, f"读不到今日已得乱斗币（everyDaySetting.{KEY}），未匹配")
    if got >= cap:
        return TaskResult(ID, SKIPPED, f"今日乱斗币已达上限 {got}/{cap}")
    opened = open_now(ctx.now)
    if opened is None:
        return TaskResult(ID, BLOCKED, "读不到服务器时间或开放时间表，未匹配")
    if not opened:
        return TaskResult(ID, SKIPPED, "不在灵宠乱斗开放时间（12:00-14:00、20:00-22:00）")
    return cap, got, uid


@task(ID, "灵宠乱斗", reads={R_INFO}, writes={R_START, R_CANCEL, R_READY, R_LOADED, R_RESULT, R_SET_PET}, group="周常", long_running=True,
      force_warning="正在进行的这一场不会结算，服务器可能把它当成中途退出。")
def run(ctx: TaskContext) -> TaskResult:
    gate = _gate(ctx)
    if isinstance(gate, TaskResult):
        return gate
    cap, got, uid = gate
    coins = [got]  # 每条日志前面都带上今日乱斗币，打完一场就更新
    ctx = replace(ctx, log=lambda message, log=ctx.log: log(f"乱斗币 {coins[0]}/{cap}｜{message}"))
    try:  # 先像客户端打开界面那样取一次信息（上阵宠物没选时服务器随机，不拦）
        pet = state.find(ctx.request(R_INFO, {}), "pet", depth=3) or {}
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"读取灵宠乱斗信息失败：{exc}，未匹配")
    team = pet.get("btlPet") if isinstance(pet, dict) else None
    ctx.log(f"灵宠乱斗：上阵宠物 {len(team) if isinstance(team, list) else '?'} 只")
    day = _Day(ctx, str(uid), got, cap, coins)
    try:
        while day.got < cap and day.play():
            pass
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"已打 {len(day.notes)} 场；{exc}") from exc
    except match_start.Banned as ban:  # 逃跑禁赛：先做后面的任务，过几分钟再来（重跑时重读乱斗币）
        return ban.result(ID, f"已打 {len(day.notes)} 场，今日乱斗币 {day.got}/{cap}" if day.notes else "")
    notes, why = day.notes, day.why
    note = f"打了 {len(notes)} 场，今日乱斗币 {day.got}/{cap}" + (f"（另有 {day.replays} 场对手中途退出没给币，已重开）" if day.replays else "")
    if why:
        return TaskResult(ID, SUBMITTED if notes and not day.incomplete else BLOCKED, f"{note}；{why}" if notes else why)
    return TaskResult(ID, SUBMITTED, note)
