"""万兽秘境（灵宠乱斗的人机关，v4811 起开放）：先把还没通关的关首通，再用每日免费次数刷已通关的最高一关。

奖励（``PetBattleDungeonPve``）：首通 兽印×30；已通关再打赢 兽印×15 + 乱斗币×10 + 灵宠库经验。
**打输什么都不给、也不扣次数**；每日免费 ``PetBattleHelperPve.dayNumMax``（现为 2）次，只算已通关的关。
次数用完后客户端会弹窗花 ``challengeTicket``（100 点券）买次数再进——所以进已通关的关之前，必须刚读过剩余次数且大于 0。

流程对照客户端 v4811 ``PetBrawlManager``（enterPveWait / sendStartPVE / fightEnd / gotoPveNext）：

* ``getPetPVPInfo {type:2}`` → ``petPve``：``count`` 今日已用次数、``copy`` {关 id: …} 有就是已通关；
* ``enterPveWait {copyId}`` → 摆阵（``self`` / ``robot`` / ``session`` / ``time`` 倒计时）；
* ``startPveBattle {}`` → 开打；本地演算后 ``endPveBattle {btlResult, hps}``（5 赢 / 4 输 / 2 平）；
  ``hps`` 是剩余血量比例，顺序同客户端 players：``session`` 单数 [自己, 人机]，双数 [人机, 自己]；
* 响应里 ``finish`` 为真 = 整关结算（``winType`` 1 胜），否则就是下一局的摆阵数据，接着 ``startPveBattle``。

**这里不演算，每局等一会儿直接报赢**（用户 2026-11-17 定，先在一个存档上试）。对面是人机，没有对手的上报跟我们对不上；
服务器会不会查血量、时长还不知道。所以默认不勾，结算不是胜、或者刷新后通关 / 次数没对上就停，不重试。
客户端 PvE 发请求时不带有效的 roomId（``roomId`` 只在 PvP 进房时赋值），这里也不带。
"""

from __future__ import annotations

import random

from .. import data, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task
from .pet_brawl import R_INFO  # getPetPVPInfo，PvE 带 type 2

ID = "pet_brawl_pve"
R_ENTER = "scene.petPvpHandler.enterPveWait"
R_START = "scene.petPvpHandler.startPveBattle"
R_END = "scene.petPvpHandler.endPveBattle"
WIN = 5
WAIT_DELAY = (4.0, 8.0)     # 摆阵后多久点“开始”（客户端倒计时 15 秒，到点自动开）
FIGHT_DELAY = (18.0, 32.0)  # 一局从开打到上报（客户端打完还要等 2.6 秒才报）
HP_LEFT = (0.25, 0.75)      # 报赢时自己剩余血量比例
ROUND_MAX = 5               # 一关最多 5 局（先赢 3 局）
PLAY_MAX = 10               # 一次执行最多打几关（6 关首通 + 2 次刷关，留点余量防死循环）


def helper() -> dict:
    try:
        return dict(data.row("PetBattleHelperPve", 1) or {})
    except data.DataError:
        return {}


def levels() -> list[dict]:
    """第 1 层的关（客户端 PetWarPveModule 只显示 layerId 1），按 id 从小到大。"""
    try:
        table = data.table("PetBattleDungeonPve") or {}
    except data.DataError:
        return []
    rows = [dict(r) for r in table.values()
            if isinstance(r, dict) and state.num(r.get("layerId")) == 1 and state.num(r.get("id"))]
    return sorted(rows, key=lambda r: state.num(r["id"]))


def passed(pve: dict, cid) -> bool:
    copy = pve.get("copy") if isinstance(pve.get("copy"), dict) else {}
    return bool(copy.get(str(cid)) or copy.get(state.num(cid)))


def unlocked(pve: dict, row: dict) -> bool:
    """客户端 isCanFight：第一关（prevId 1）、自己通过、或者上一关通过了。"""
    return state.num(row.get("prevId")) == 1 or passed(pve, row["id"]) or passed(pve, row.get("prevId"))


def left_of(pve: dict) -> int | None:
    cap, used = state.num(helper().get("dayNumMax")), state.num(pve.get("count", 0))
    return None if cap is None or used is None else cap - used


def pick(pve: dict) -> tuple[str, dict] | None:
    """下一关打哪：先首通（不吃次数）；都通了且有免费次数就刷已通关的最高一关。没得打返回 None。"""
    rows = levels()
    fresh = next((r for r in rows if unlocked(pve, r) and not passed(pve, r["id"])), None)
    if fresh:
        return "首通", fresh
    done = [r for r in rows if passed(pve, r["id"])]
    left = left_of(pve)
    if done and left is not None and left > 0:
        return "刷关", done[-1]
    return None


def hps(session: int, mine: float) -> list[float]:
    return [mine, 0.0] if session % 2 == 1 else [0.0, mine]


class _Run:
    def __init__(self, ctx: TaskContext) -> None:
        self.ctx = ctx
        self.gains = dungeon.Gains()
        self.notes: list[str] = []

    def read(self) -> dict:
        reply = self.ctx.request(R_INFO, {"type": 2})
        pve = state.find(reply, "petPve", depth=3)
        if not isinstance(pve, dict):
            raise dungeon.Stop("服务器没返回万兽秘境数据（petPve）")
        return pve

    def wait(self, span: tuple[float, float]) -> None:
        if not self.ctx.sleep(random.uniform(*span), background=False) or self.ctx.stopped:
            raise dungeon.Stop("已被用户停止")

    def play(self, row: dict) -> dict:
        """打一关，返回结算响应（finish 为真）。"""
        ctx, cid = self.ctx, state.num(row["id"])
        reply = ctx.request(R_ENTER, {"copyId": cid})
        self.gains.add(reply, [])
        ctx.engage(True)
        try:
            for n in range(1, ROUND_MAX + 1):
                self.wait(WAIT_DELAY)
                start = ctx.request(R_START, {})
                session = state.num(state.find(start, "session", depth=2))
                if session is None or session <= 0:
                    raise dungeon.Stop(f"第 {n} 局开打响应里没有 session，认不出座位，未上报：{str(start)[:200]}")
                self.wait(FIGHT_DELAY)
                body = {"btlResult": WIN, "hps": hps(session, round(random.uniform(*HP_LEFT), 3))}
                ctx.log(f"万兽秘境：{row.get('name')} 第 {n} 局（session {session}）报赢 {body}")
                reply = ctx.request(R_END, body)
                if state.find(reply, "finish", depth=2):
                    ctx.log(f"万兽秘境：{row.get('name')} 结算 {reply}")
                    return reply
                ctx.log(f"万兽秘境：第 {n} 局已响应，已赢 {state.find(reply, 'btlWin', depth=2)}")
            raise dungeon.Stop(f"{row.get('name')} 打满 {ROUND_MAX} 局还没结算")
        finally:
            ctx.engage(False)

    def one(self, pve: dict) -> dict | None:
        """打下一关并核对。返回刷新后的 petPve；没得打返回 None。出问题抛 Stop。"""
        choice = pick(pve)
        if choice is None:
            return None
        kind, row = choice
        cid, name = state.num(row["id"]), row.get("name") or row["id"]
        before = state.num(pve.get("count", 0))
        if kind == "刷关" and not (left_of(pve) or 0) > 0:  # pick 已判过，这里再兜一次：没免费次数绝不进（否则要花点券）
            return None
        self.ctx.log(f"万兽秘境：{kind}「{name}」（今日免费次数剩 {left_of(pve)}）")
        finish = self.play(row)
        self.gains.add(finish, [])
        win = state.num(state.find(finish, "winType", depth=2))
        if win not in (1, 3):
            raise dungeon.Stop(f"「{name}」报赢后结算不是胜（winType={win}）：{str(finish)[:300]}")
        after = self.read()
        if kind == "首通" and not passed(after, cid):
            raise dungeon.Stop(f"「{name}」结算是胜，但刷新后没记为通关，不再继续")
        if kind == "刷关" and state.num(after.get("count", 0)) != (before or 0) + 1:
            raise dungeon.Stop(f"「{name}」结算是胜，但今日次数 {before}→{after.get('count')} 没对上，不再继续")
        self.notes.append(f"{kind}「{name}」")
        return after


@task(ID, "万兽秘境", reads={R_INFO}, writes={R_ENTER, R_START, R_END}, group="日常", long_running=True,
      force_warning="正在打的这一关不会结算（打输、没打完都不扣次数）。")
def run(ctx: TaskContext) -> TaskResult:
    if not levels() or state.num(helper().get("dayNumMax")) is None:
        return TaskResult(ID, BLOCKED, "本地没有万兽秘境的关卡表（PetBattleDungeonPve / PetBattleHelperPve），未进入")
    r = _Run(ctx)
    why, status, pve = None, SUBMITTED, None
    try:
        pve = r.read()
        for _ in range(PLAY_MAX):
            if ctx.stopped or ctx.winding:
                why = "已被用户停止" if ctx.stopped else None
                break
            nxt = r.one(pve)
            if nxt is None:
                break
            pve = nxt
        else:
            why = f"一次执行已打满 {PLAY_MAX} 关，停止"
    except dungeon.Stop as stop:
        why, status = str(stop), (SUBMITTED if r.notes and str(stop) == "已被用户停止" else BLOCKED)
    except p.AllianceError as exc:
        why, status = f"服务器拒绝：{exc}", BLOCKED
    left = left_of(pve) if pve is not None else None
    text = (f"打了 {len(r.notes)} 关：{'、'.join(r.notes)}" if r.notes else "今日免费次数已用完" if left == 0 else "没有可打的关") + (
        f"；今日免费次数剩 {left}" if left is not None else "")
    if r.gains.items:
        text += f"；获得 {r.gains.text()}"
    if why:
        return TaskResult(ID, status, f"{text}；{why}")
    return TaskResult(ID, SUBMITTED if r.notes else SKIPPED, text, day_done=left is not None and left <= 0)
