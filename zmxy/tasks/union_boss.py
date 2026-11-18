"""联盟 Boss（周常页）：进场 → 照真人的节奏上报伤害 → 等结算推送 → 退出。没有本地战斗。

**打多少由用户填**（选择框“目标伤害（%）”，Boss 总血量的百分之几，存 ``percent``；没填不进场）。伤害跟着 Boss 掉的血走：
Boss 掉了几成，我的累计伤害就到目标的几成，Boss 掉到 ``AHEAD`` 时到齐，到了就不再报（等它被打死、收结算）。
速度按最近 ``DROP_WINDOW`` 秒的掉血速度估，落后 / 超前的差额在 ``CATCH`` 秒左右里补平；单次大小不超过正常速度的 ``SWAY`` 范围。

节奏照世界 Boss（``boss_rules``）：一阵高一阵低用真人的节拍（``event_beat`` / ``beat_damage``），活着时偶尔短停（``PAUSE``）。
联盟 Boss 打的时间短、实测几乎没人倒地等复活（``联盟Boss/分析_2026-10-10.md``），所以不学倒地。
上报间隔照客户端 ``UnionBossSendDamageCmd``：伤害攒着，离上次超过 5 秒、下一下打中时才发；Boss 血量 1%–10% 时 1 秒，≤ 1% 时每下都发。

协议：``challengeBoss {pNum:0}``（应答有 ``startTime``/``endTime``/``serverTime``，实测**没有 Boss 对象**）→
``attackBossCopy {hurt}`` → 结算 ``onFinishLeagueBossCopy`` → ``exitLeagueCopy``。
Boss 血量来自 ``onLeagueBossHpChange``（客户端把它的 message 并进 Boss 对象），最大血量依次取：进场应答的 Boss、血量推送、
开战前看到的血量（那时还是满血）、看到过的最高血量（开战后才进场时，偏小一点）。自己的累计伤害来自 ``onBossCopySelfHit {hit}``。
进场后任何路径结束都会退出副本；上报失败不重发。**未真机验证**。
"""

from __future__ import annotations

import random
import time
from typing import Any, Mapping

from .. import protocol as p, servertime, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, task
from . import boss_rules as r

ID = "union_boss"
R_ENTER = "league.leagueHandler.challengeBoss"
R_HIT = "scene.leagueHandler.attackBossCopy"
R_EXIT = "scene.leagueHandler.exitLeagueCopy"
P_SELF, P_HP, P_FINISH = "onBossCopySelfHit", "onLeagueBossHpChange", "onFinishLeagueBossCopy"
KEY = "percent"              # 选项里的目标伤害：Boss 总血量的百分之几
RANGE = (0.01, 90.0)         # 输入框范围（%）
UNIT = 10_000
# 开战后过多久报第一笔：(累计比例, 秒)。实测 2026-10-10：多数人第 10 秒左右，最晚的 30 秒
FIRST = ((0.0, 6.0), (0.5, 10.0), (0.9, 18.0), (1.0, 30.0))
SEND_GAP = ((0.01, 0.0), (0.1, 1.0), (1.0, 5.0))  # (Boss 血量比例不超过, 离上次至少几秒)：客户端 UnionBossSendDamageCmd
HIT_LAG = (0.1, 0.8)         # 过了间隔以后等下一下打中才发：再晚这么多秒
EXPECTED = 230.0             # 掉血速度估不出来时按 Boss 这么多秒死（实测 2026-10-10：229 秒）
DROP_WINDOW = 30.0           # 按最近这么多秒的掉血速度估
DROP_MIN = 8.0               # 不满这么多秒的掉血记录不估
AHEAD = 0.95                 # Boss 掉到这个比例时伤害到齐（留一点余地：Boss 死得比估的快也能到）
CATCH = 20.0                 # 落后 / 超前的差额大约用多少秒补平
LATE = 0.8                   # Boss 掉到这个比例以后补得快一倍（剩下的时间不多了）
SWAY = (0.25, 3.0)           # 单次伤害只在正常速度的这个倍数范围里
MANA = r.MANA_RANGE[1]       # 节拍里的蓝：联盟 Boss 不学蓝空掉速

STOPPED = "已被用户停止（中途退出副本）"


def wan(value: float) -> str:
    """伤害写成“x 亿 / x 万”。"""
    return f"{value / 1e8:.2f} 亿" if value >= 1e8 else f"{value / UNIT:,.0f} 万"


def summary(opt: Mapping[str, Any]) -> str:
    pct = r.real(opt.get(KEY))
    return f"目标 {pct:g}%" if pct else "未设置目标伤害"


def _body(body: Any) -> Any:
    return body.get("message", body) if isinstance(body, Mapping) and isinstance(body.get("message"), Mapping) else body


def _gap(share: float | None) -> float:
    """这次上报完，离下一次至少多久（客户端按 Boss 血量比例定），再加上等下一下打中的工夫。"""
    least = next(gap for top, gap in SEND_GAP if share is not None and share <= top) if share is not None else SEND_GAP[-1][1]
    return least + random.uniform(*HIT_LAG)


class Fight:
    """一场联盟 Boss：按目标伤害跟着 Boss 掉的血报。"""

    def __init__(self, ctx: TaskContext, pct: float, hp_max: float | None, start_ms: float, end_ms: float) -> None:
        self.ctx, self.pct, self.hp_max = ctx, pct, hp_max
        now, server = time.monotonic(), servertime.now()  # start_ms / end_ms 是服务器时间
        self.opens = now + max(0.0, start_ms / 1000 - server) if start_ms else now
        left = end_ms / 1000 - server if end_ms else 0
        self.deadline = now + (left + r.GRACE_SECONDS if left > 0 else r.FALLBACK_SECONDS)
        self.hp: float | None = None
        self.peak = 0.0                            # 看到过的最高血量
        self.hps: list[tuple[float, float]] = []   # 开战后的血量 [(时刻, 血量)]
        self.hit = 0.0                             # 服务器回的累计伤害
        self.total = 0.0                           # 我报上去的合计
        self.sent = 0
        self.finished: Any = None
        self.send_at = self.opens + r.draw(FIRST)  # 下一次上报不早于这个时刻
        self.active_at = self.send_at - _gap(None)  # 从这时起开始打（第一笔是攒了一轮的）
        self.accounted = self.active_at            # 伤害算到哪里了
        self.tune_at, self.tempo = self.active_at, r.event_beat(0)
        self.calm = self.active_at + r.CALM        # 这之前不短停
        self.reached = False

    # ---- 推送
    def absorb(self) -> None:
        for route, raw in self.ctx.session.drain_routed():
            body = _body(raw)
            if not isinstance(body, Mapping):
                continue
            if route == P_HP and r.real(body.get("hp")) is not None:
                self.hp = r.real(body["hp"])
                self.peak = max(self.peak, self.hp)
                if not self.hp_max:
                    self.hp_max = r.real(body.get("hpMax")) or (self.hp if time.monotonic() < self.opens else None)
                if time.monotonic() >= self.opens:
                    self.hps.append((time.monotonic(), self.hp))
            elif route == P_SELF and r.real(body.get("hit")) is not None:
                self.hit = max(self.hit, r.real(body["hit"]))
            elif route == P_FINISH:
                self.finished = body
                self.hp_max = self.hp_max or r.real(body.get("hpMax"))

    # ---- 算伤害
    @property
    def goal(self) -> float:
        """目标伤害 = 总血量 × 百分比；总血量还不知道 → 0。"""
        return self.hp_max * self.pct / 100 if self.hp_max else 0.0

    def mine(self) -> float:
        return max(self.total, self.hit)

    def share(self) -> float | None:
        return self.hp / self.hp_max if self.hp is not None and self.hp_max else None

    def lost(self, now: float) -> float:
        """Boss 掉了几成；血量不知道就按开战以来的时间估。"""
        share = self.share()
        return 1.0 - share if share is not None else min(1.0, max(0.0, now - self.opens) / EXPECTED)

    def drop(self, now: float) -> float:
        """Boss 每秒掉几成（最近 DROP_WINDOW 秒）；估不出来按 EXPECTED 秒死算。"""
        recent = [(t, hp) for t, hp in self.hps if t >= now - DROP_WINDOW]
        if self.hp_max and len(recent) >= 2 and recent[-1][0] - recent[0][0] >= DROP_MIN and recent[0][1] > recent[-1][1]:
            return (recent[0][1] - recent[-1][1]) / self.hp_max / (recent[-1][0] - recent[0][0])
        return 1.0 / EXPECTED

    def hurt(self, now: float) -> int:
        """这次报多少：正常速度 × 这段的节拍，再把离“该到多少”的差额补一部分。"""
        start = max(self.accounted, self.active_at)
        self.accounted = now
        if now <= start:
            return 0
        area, self.tempo, self.tune_at = r.beat_damage(0, self.tempo, self.tune_at, self.active_at, MANA, start, now)
        usual = self.goal * self.drop(now) * area
        if usual <= 0:
            return 0
        lost = self.lost(now)
        owed = self.goal * min(1.0, lost / AHEAD) - self.mine()
        hurt = usual + owed * min(1.0, (now - start) / (CATCH * (0.5 if lost >= LATE else 1.0)))
        hurt = min(max(hurt, usual * SWAY[0]), usual * SWAY[1], self.goal - self.mine())
        return max(0, round(hurt))

    def pause(self, now: float, elapsed: float) -> float | None:
        """偶尔短停（照世界 Boss 活着时的短停）：停了返回停多久。落后于“该到多少”时不停（Boss 死得快时停一下就补不回来）。"""
        if self.mine() < self.goal * min(1.0, self.lost(now) / AHEAD):
            return None
        if now < self.calm or elapsed <= 0 or random.random() >= min(1.0, elapsed / (r.PAUSE_EVERY - r.CALM)):
            return None
        rest = r.draw(r.PAUSE)
        self.calm = now + rest + r.CALM
        return rest

    # ---- 主循环
    def run(self) -> tuple[bool, str]:
        """返回 (是否收到结算推送, 说明)。退出副本由调用方负责。"""
        ctx = self.ctx
        wait = self.opens - time.monotonic()
        if wait > 0:
            ctx.log(f"联盟Boss：等待开战约 {wait:.0f} 秒")
        while time.monotonic() < self.deadline:
            if ctx.stopped:
                return False, STOPPED
            self.absorb()
            if self.finished is not None:
                break
            if self.hp is not None and self.hp <= 0:  # Boss 已倒：不再上报，等结算推送
                try:
                    self.finished = ctx.wait_push(P_FINISH, max(1.0, self.deadline - time.monotonic()))
                except p.AllianceError:
                    pass
                break
            now = time.monotonic()
            if now < self.send_at or self.reached:
                if not ctx.sleep(max(0.2, min(1.0, self.send_at - now)), background=False):
                    return False, STOPPED
                continue
            if not self.hp_max:
                if not self.peak:  # 还没收到过血量：先不打
                    if not ctx.sleep(1.0, background=False):
                        return False, STOPPED
                    continue
                self.hp_max = self.peak
                ctx.log(f"联盟Boss：没读到 Boss 总血量，按看到过的最高血量 {wan(self.peak)} 算")
            started = max(self.accounted, self.active_at)
            hurt = self.hurt(now)
            if hurt > 0:
                try:
                    ctx.request(R_HIT, {"hurt": hurt})
                except p.AllianceError as exc:
                    self.absorb()
                    if self.finished is None:
                        return False, f"伤害上报停止：{exc}；{self.note()}"
                    break
                self.sent, self.total = self.sent + 1, self.total + hurt
                if self.sent % 10 == 0:
                    ctx.log(f"联盟Boss：已上报 {self.sent} 次，{self.note()}")
            if self.mine() >= self.goal:
                self.reached = True
                ctx.log(f"联盟Boss：已到目标 {self.pct:g}%（{wan(self.goal)}），不再上报，等 Boss 被打死")
                continue
            gap = _gap(self.share())
            rest = self.pause(now, now - started)
            if rest is not None:
                self.active_at = now + rest   # 停手的这段不算伤害
                gap += rest
            self.send_at = now + gap
        if self.finished is None:  # 时间到了还没收到结算：再等一小会儿
            try:
                self.finished = ctx.wait_push(P_FINISH, 10)
            except p.AllianceError:
                return False, f"{self.note()}；等待结算推送超时"
        return True, self.verdict()

    def note(self) -> str:
        hit = self.mine()
        part = f"（Boss 血量的 {hit / self.hp_max:.2%}）" if self.hp_max else ""
        return f"目标 {self.pct:g}%" + (f"（{wan(self.goal)}）" if self.hp_max else "") + f"，已打 {wan(hit)}{part}"

    def verdict(self) -> str:
        done = self.finished if isinstance(self.finished, Mapping) else {}
        hit = r.real(done.get("hit"))
        if hit is not None:
            self.hit = max(self.hit, hit)
        rank = state.num(done.get("rank"))
        tail = (f"，第 {rank} 名" if rank else "") + ("；Boss 已被击败" if r.finish_success(done) else "；Boss 没被打死")
        return f"上报 {self.sent} 次，{self.note()}{tail}"


def _exit(ctx: TaskContext) -> None:
    try:
        ctx.request(R_EXIT, {})
    except p.AllianceError:  # 服务器明确拒绝退出，不覆盖本场已经确定的结果
        pass


@task(ID, "联盟Boss", writes={R_ENTER, R_HIT, R_EXIT}, group="周常", needs=((KEY,),),
      force_warning="会马上退出副本，这一场后面的伤害就不打了。",
      choices=Choices(None, button="设置",
                      picks=(Pick(KEY, "目标伤害（%）", lambda _a: [], number=RANGE),),
                      note="填这一场要打掉 Boss 总血量的百分之几（例：10 = 一成血）。\n"
                           "伤害跟着 Boss 掉的血走：Boss 掉了几成，伤害就到目标的几成，掉到 95% 时打满，之后不再上报。\n"
                           "节奏照世界Boss的真人节拍；直接上报伤害，不耗道具。开战时间由盟主定，到时间前进场会等着开战。"))
def union_boss(ctx: TaskContext) -> TaskResult:
    pct = r.real(ctx.options.get(KEY))
    if not pct or not RANGE[0] <= pct <= RANGE[1]:
        return TaskResult(ID, SKIPPED, "没有设置目标伤害" if not pct else f"目标伤害 {pct:g}% 不在 {RANGE[0]:g}–{RANGE[1]:g}% 里")
    league = ctx.info.get("league")
    if not isinstance(league, Mapping) or not league.get("leagueId"):
        return TaskResult(ID, SKIPPED, "当前存档未加入联盟")
    try:
        entered = ctx.request(R_ENTER, {"pNum": 0})
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"进入联盟 Boss 失败：{exc}")
    try:
        if not isinstance(entered, Mapping):
            return TaskResult(ID, BLOCKED, "进入联盟 Boss 的应答不是对象，未上报伤害")
        boss = entered.get("Boss") or entered.get("boss")
        hp_max = r.real(boss.get("hpMax")) if isinstance(boss, Mapping) else None
        ctx.engage(True)  # 进场了：从这里起点“停止”要问打完再停还是立即停
        fight = Fight(ctx, pct, hp_max, r.real(entered.get("startTime")) or 0, r.real(entered.get("endTime")) or 0)
        done, note = fight.run()
    finally:
        try:
            _exit(ctx)
        finally:
            ctx.engage(False)
    return TaskResult(ID, SUBMITTED if done else BLOCKED, note)
