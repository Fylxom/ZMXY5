"""一场世界 Boss 里从推送攒出来的局面（``Room``）和结算说明。占比一律是百分数（5 = 5%）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import bosslog, state
from . import boss_rules as r


def _rows(rows: Any) -> list[Mapping[str, Any]]:
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


@dataclass
class Room:
    """一场世界 Boss 里从推送攒出来的局面：自己的占比、所在的榜、同房间的人和他们的战力。占比一律是百分数（5 = 5%）。"""

    hp_max: float
    uid: int | None
    power: float | None          # 我的战力
    high_power: float | None     # 进高战榜的战力线
    floor: float                 # 目标的下限（门槛出头）
    lead: float
    threshold: float = r.THRESHOLD
    hp: float | None = None
    pct: float = 0.0
    kind: int | None = None      # 我所在的榜：1 高战 / 2 普通（以榜单推送的 type 为准，没推送前按战力猜）
    rows: list = field(default_factory=list)       # 我那个榜的行 {uid, rank, percent, hit}（服务器只推前 20 名；走掉的人不在里面）
    powers: dict = field(default_factory=dict)     # 房间里的人：uid → 战力（None = 读不到）
    gears: dict = field(default_factory=dict)      # 房间里的人：uid → 装备倍数（gear_of；None = 读不到）
    hps: list = field(default_factory=list)        # 最近的血量推送 [(时刻, 血量)]，估 Boss 还能活多久
    seen: dict = field(default_factory=dict)       # 同榜的人最近 TREND 秒的占比：uid → [(时刻, 占比)]
    moved: dict = field(default_factory=dict)      # 两个榜上的人：uid → (占比, [最近几回占比涨的时刻])；刚看到、还没见它涨过 → 没有时刻
    others: list = field(default_factory=list)     # 另一个榜的行：只给日志看，不参与判断
    finished: Any = None
    boss: int = 0                # 哪个祖巫（WorldBoss 表的 id）：全场秒杀只有一部分祖巫有（``KILL_BOSSES``）
    gear: float | None = None    # 我的装备倍数（gear_of）：进房前查的；读不到 → None，按 1 算
    dry: bool = False            # 我带的是深渊饰品（abyss）：蓝少，短停更勤
    panel: Any = None            # 我的最终面板（powerAttr）：算满蓝能撑多久用；没查到 → None
    gear_mode: str = r.MID         # 档位（LOW / MID / HIGH）
    mana: float = r.MANA_USUAL     # 我满蓝能撑几秒（mana_seconds）：进房前查的；算不出来按常人的
    room_id: str = ""            # 我在哪个房间：带 roomId 的推送不是这个房间的就不要（上一个房间迟到的）
    journal: bosslog.Journal | None = None  # 同场玩家记录（bosslog）：推送原样抄一份进去，不参与任何判断

    def __post_init__(self) -> None:
        if self.kind is None and self.power is not None and self.high_power:
            self.kind = 1 if self.power >= self.high_power else 2

    def _pct(self, row: Mapping[str, Any]) -> float:
        hit = r.real(row.get("hit"))
        return hit / self.hp_max * 100 if hit else r.real(row.get("percent")) or 0.0

    def absorb(self, route: str | None, body: Any, now: float) -> None:
        log = self.journal
        if self.room_id and isinstance(body, Mapping):
            pushed_room = body.get("roomId")
            if pushed_room is not None and str(pushed_room) != str(self.room_id):
                return  # 上一个房间迟到的推送，onJoin 也不能污染当前房间
        if route == r.P_JOIN:
            for member in r.members(body):
                self._joined(member, now)
        elif not isinstance(body, Mapping):
            return
        elif route == r.P_LEAVE:
            self.powers.pop(state.num(body.get("uid")), None)
            log and log.leave(state.num(body.get("uid")), now)
        elif route == r.P_SELF:
            self.pct = max(self.pct, self._pct(body))
            log and log.own(body, now)
        elif route == r.P_HP and r.real(body.get("hp")) is not None:
            self.hp = r.real(body["hp"])
            self.hps = [(t, hp) for t, hp in self.hps if t >= now - r.ETA_WINDOW * 2] + [(now, self.hp)]
            log and log.hp(self.hp, now)
        elif route == r.P_RANK:
            log and log.rank(body, now)
            self._ranked(body, now)
        elif route == r.P_FINISH:
            self.finished = body

    def _joined(self, member: Mapping[str, Any], now: float) -> None:
        uid = state.num(member["uid"])
        power, gear = r.power_of(member), r.gear_of(member)
        self.powers[uid] = power
        self.gears[uid] = gear
        if uid == self.uid:
            if power is not None:
                self.power = power
            self.gear = gear or self.gear
            self.dry = self.dry or r.abyss(member)
            self.mana = r.mana_seconds(member, self.panel) or self.mana
        if self.journal:
            self.journal.join(uid, member, power, now)

    def _ranked(self, body: Mapping[str, Any], now: float) -> None:
        self.kind = int(state.num(body.get("type")) or 0) or self.kind
        self.rows = _rows(body.get("high" if self.kind == 1 else "low"))
        past, self.seen = self.seen, {}
        for uid, (_, share) in self.rivals().items():
            self.seen[uid] = [(t, v) for t, v in past.get(uid, ()) if t >= now - r.TREND] + [(now, share)]
        self.others = _rows(body.get("low" if self.kind == 1 else "high"))
        for row in self.rows + self.others:
            uid, share = state.num(row.get("uid")), self._pct(row)
            if uid is not None and uid != self.uid and (uid not in self.moved or share > self.moved[uid][0]):
                self.moved[uid] = (share, [t for t in self.moved[uid][1] if t >= now - r.BUSY[0] - r.STALL[1]] + [now] if uid in self.moved else [])
        self.pct = max([self.pct] + [self._pct(row) for row in self.rows if state.num(row.get("uid")) == self.uid])

    @property
    def rank(self) -> int | None:
        return r.my_rank(self.rows, self.uid)

    def rivals(self) -> dict[int, tuple[float | None, float]]:
        """同榜榜单上的其他人：uid → (预估出伤是我的几倍, 占比)；他或我的战力读不到 → 倍数是 None。"""
        mine = r.output(self.power, self.gear)

        def times(uid: int) -> float | None:
            his = r.output(self.powers.get(uid), self.gears.get(uid))
            return his / mine if his and mine else None

        return {uid: (times(uid), self._pct(row)) for row in self.rows
                if (uid := state.num(row.get("uid"))) is not None and uid != self.uid}

    def wiped(self, now: float) -> tuple[float, int] | None:
        """同房间的人是不是刚一起倒了（见 STALL）：是 → (他们停手的时刻（取中间那个）, 倒了几个)；不是 → None。"""
        ages = sorted(now - rises[-1] for _, rises in self.moved.values()
                      if rises and now - rises[-1] <= r.STALL[1] and sum(1 for t in rises if t >= rises[-1] - r.BUSY[0]) >= r.BUSY[1])
        down = [age for age in ages if age >= r.STALL[0]]
        return (now - down[len(down) // 2], len(down)) if len(down) >= max(r.WIPE_MIN, len(ages) - len(down)) else None

    def pushing(self, now: float) -> bool:
        """要紧的对手里有没有人还在出伤（见 PEEK）。"""
        return any(share >= r.QUIT_SHARE and share > self.pct - self.lead and (rises := self.moved.get(uid, (0, ()))[1]) and now - rises[-1] <= r.PUSHING
                   for uid, (_, share) in self.rivals().items())

    def scoring(self, left: float = 0.0) -> list[tuple[float | None, float]]:
        """对手 = 同榜里伤害占比已到 QUIT_SHARE 的人：[(预估出伤是我的几倍, 占比)]。还没出伤的路人不算。
        ``left`` > 0 → 占比换成估到 ``left`` 秒以后的：加上他最近 TREND 秒的速度 × left，最多加到 Boss 剩下的血那么多。"""
        cap = self.hp / self.hp_max * 100 if self.hp is not None else 100.0
        out = []
        for uid, (times, share) in self.rivals().items():
            if share >= r.QUIT_SHARE:
                (t0, v0), (t1, _) = self.seen[uid][0], self.seen[uid][-1]
                speed = (share - v0) / (t1 - t0) if t1 - t0 >= r.TREND_MIN else 0.0
                out.append((times, share + min(max(0.0, speed) * left, cap)))
        return out

    def strong(self) -> list[float]:
        """对手里打不过的：预估出伤（战力 × 装备，用户 2026-10-11 定：不光比战力）比我高出 NEAR% 以上的，返回各是我的几倍。自己的战力读不到 → 不判断。"""
        return [times for times, _ in self.scoring() if times is not None and times > 1 + r.NEAR / 100]

    def roster(self, most: int = 8) -> str:
        """给日志看的一行：Boss 剩余血量 + 房间里其他玩家的战力和占比（同榜的在前，按占比从高到低；另一个榜的标“别榜”）。"""
        def one(power: float | None, pct: float, tag: str = "") -> str:
            return f"{tag}{'战力?' if power is None else f'{power / 10000:.1f}万'} {pct:.2f}%"

        mine = sorted(((self.powers.get(uid), pct) for uid, (_, pct) in self.rivals().items()), key=lambda v: -v[1])
        away = sorted(((self.powers.get(state.num(row.get("uid"))), self._pct(row)) for row in self.others), key=lambda v: -v[1])
        shown = [one(pw, pct) for pw, pct in mine] + [one(pw, pct, "别榜 ") for pw, pct in away]
        more = f" 等 {len(shown)} 人" if len(shown) > most else ""
        boss = "Boss 血量未知" if self.hp is None else f"Boss 剩 {max(0.0, self.hp) / self.hp_max:.1%}"
        return f"{boss}；其他玩家：{'、'.join(shown[:most]) + more if shown else '还没看到'}"

    def third(self, left: float = 0.0, ghosts: bool = False) -> float:
        """对手里第 TOP 高的占比（``left`` > 0 = 估到 left 秒以后的）：超过它才进得了前 TOP；对手不到 TOP 个 → 0。
        ``ghosts``：对手不到 TOP 个、Boss 还要活一阵（left > 0）时，缺的名额按后面会进来的人算（每人 GHOST × Boss 剩下的血）。"""
        shares = sorted((share for _, share in self.scoring(left)), reverse=True)
        if ghosts and left > 0 and self.hp is not None:
            shares = sorted(shares + [r.GHOST * self.hp / self.hp_max * 100] * (r.TOP - len(shares)), reverse=True)
        return shares[r.TOP - 1] if len(shares) >= r.TOP else 0.0

    def target(self, left: float = 0.0) -> float:
        """要保持在多少以上：稳进前 TOP 就行——比对手里第 TOP 高的（估到 left 秒以后的，不够 TOP 个时算上后面会进来的人）多一段领先；
        没有可比的 → 门槛出头。"""
        third = self.third(left, ghosts=True)
        return min(100.0, max(self.floor, third + self.lead)) if third else self.floor

    def hopeless(self, reach: float | None = None, left: float = 0.0) -> float | None:
        """到 Boss 死（还有 ``left`` 秒）我最多还能涨 ``reach`` 个百分点（不给 = 只看 Boss 剩下的血）也不够：到不了领奖门槛，
        或者超不过对手里第 TOP 高的 → 返回够不到的那个占比；还够得到 / 血量未知 → None。"""
        if self.hp is None:
            return None
        need, rest = max(self.threshold, self.third(left)), self.hp / self.hp_max * 100
        return need if self.pct + (rest if reach is None else min(rest, reach)) <= need else None

    def pressed(self) -> bool:
        """同榜对手里有出伤相近的人（预估出伤在我的 1 ± NEAR% 以内）排在我前面，而我现在的占比还没稳进前 TOP → 不等估算、也不等对手凑够 TOP 个，直接追。
        比我弱一截的排在前面不算（用户 2026-10-11 定）：他只是先进的房，照正常输出打一会儿就超过去了——真机上一个出伤只有我 0.42 倍的人刚到 3%，进房 8 秒就切了高档。
        稳 = 我现在的占比已经比对手里第 TOP 高的（不够 TOP 个时缺的名额按后面会进来的人算）多出一段领先：只看现在，不估以后——
        估“Boss 还能活多久”在人少的时候偏长（后面进来的人会把它打快），照它算会以为正常输出也够。自己的战力读不到 → 不判断。"""
        if self.power is None:
            return False
        rivals = self.scoring()
        if not any(times is not None and abs(times - 1) <= r.NEAR / 100 and share > self.pct for times, share in rivals):
            return False
        shares = sorted([share for _, share in rivals] + [r.GHOST * (100.0 if self.hp is None else self.hp / self.hp_max * 100)] * (r.TOP - len(rivals)), reverse=True)
        return self.pct < shares[r.TOP - 1] + self.lead

    def safe(self) -> bool:
        """Boss 快死了、自己已经达标且在前三：这时候退出等于白打。"""
        return self.hp is not None and self.hp <= self.hp_max * r.SAFE_HP and self.pct >= self.threshold and (self.rank or r.TOP + 1) <= r.TOP

    def lost(self) -> bool:
        """Boss 快死了（血量 ≤ LAST_HP）、榜单已经推过来而我不在前三：来不及了。"""
        return self.hp is not None and self.hp <= self.hp_max * r.LAST_HP and bool(self.rows) and (self.rank or r.TOP + 1) > r.TOP

    def eta(self, now: float) -> float | None:
        """按最近 ETA_WINDOW 秒的掉血速度估 Boss 还能活几秒；样本不够 → None。"""
        old = next(((t, hp) for t, hp in self.hps if t >= now - r.ETA_WINDOW), None)
        if old is None or self.hp is None or now - old[0] < 5 or old[1] <= self.hp:
            return None
        return self.hp / ((old[1] - self.hp) / (now - old[0]))


def verdict(room: Room) -> str:
    """结算推送 ``{success, hit, hpMax, rank, joinReward, rankReward, defeatReward}`` → 一句话结果。"""
    done = room.finished if isinstance(room.finished, Mapping) else {}
    hit, hp_max = r.real(done.get("hit")), r.real(done.get("hpMax")) or room.hp_max
    pct = hit / hp_max * 100 if hit else room.pct
    rank = int(state.num(done.get("rank")) or 0) or room.rank
    if done.get("rankReward"):
        prize = "已得排名奖励"
    elif not r.finish_success(done):
        prize = "Boss 未被击败，没有排名奖励"
    else:
        why = [s for s in (f"占比不到 {room.threshold:g}%" if pct < room.threshold else "", f"不在前 {r.TOP}" if (rank or r.TOP + 1) > r.TOP else "") if s]
        prize = "没有排名奖励" + (f"（{'、'.join(why)}）" if why else "")
    return f"占比 {pct:.2f}%，{r.BOARDS.get(room.kind, '榜未知')}第 {rank or '—'} 名，{prize}"
