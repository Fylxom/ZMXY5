"""世界 Boss 进房后的上报循环：什么时候打、打多少、什么时候倒下和起来，都在 ``Fight`` 里。参数和小判断在 ``boss_rules``，局面在 ``boss_room``。

节奏经过盲测：判断的先后、随机数抽取的先后和次数都是定好的，整理时不能动。
``tools/boss_analysis/replay_audit.py`` 按属性名读 ``Fight`` 的状态（born、last、rate、window 等），改名要一起改。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Mapping

from .. import api, protocol as p
from ..core import TaskContext
from . import boss_rules as r
from .boss_room import Room, verdict

STOPPED = "已被用户停止（中途退出，本场伤害作废）"


def look(request: Callable[[str, dict], Any], journal: Any, now: float) -> None:
    """对局中顺手查一个同场玩家的最终面板记下来（只为攒数据：隔几个小时人家就换装备了，事后查对不上）。查不到不影响打。"""
    if journal is None or now < journal.look_at:
        return
    uid = journal.unseen()
    if uid is None:
        return
    journal.look_at = now + random.uniform(*r.LOOK_GAP)
    try:
        reply = api.offline_user_info(request, uid)
    except p.AllianceError:
        reply = None
    journal.panel(uid, reply.get("user") if isinstance(reply, Mapping) else None, now)


def recount(request: Callable[[str, dict], Any], journal: Any, idle: Callable[[float], Any]) -> None:
    """打完、退了房以后把当场数过囤货的人各再查一次，只数还魂丹：比开打时少了就是用了复活道具。查不到的跳过。"""
    for uid in journal.again():
        if not idle(random.uniform(*r.LOOK_GAP)):
            return
        try:
            reply = api.offline_user_info(request, uid)
        except p.AllianceError:
            continue
        journal.recount(uid, reply.get("user") if isinstance(reply, Mapping) else None, time.monotonic())


class Fight:
    """一场世界 Boss 的出手节奏。"""

    def __init__(self, ctx: TaskContext, room: Room, hit_body: Callable[[float], dict], left: float) -> None:
        self.ctx, self.room, self.hit_body = ctx, room, hit_body
        self.start = time.monotonic()
        self.end = self.start + (left if left > 0 else r.FALLBACK_SECONDS)
        self.deadline = self.end + (r.GRACE_SECONDS if left > 0 else 0)
        self.might = self.top = self.usual = self.every = 0.0  # 见 tune
        self.tuned = None             # 上一次按什么装备算的速度
        self.sent = 0
        self.plan = None
        self.chasing = room.gear_mode == r.HIGH  # 正在追（高档从头追到尾，低档从不追）
        self.doomed = None            # 从什么时候起一直追不上
        self.swing = random.uniform(*r.SWING)
        self.kill_at = None           # 下一次全场秒杀是什么时候（None = Boss 还没掉到半血 / 这个祖巫没有）
        self.struck = None            # 上一次全场秒杀是什么时候
        self.synced = None            # 哪一次（struck）已经照同房间的人对过钟了：一次只对一回
        self.last = None              # 这条命实际发出上一笔伤害的时刻；复活间隔从这里算
        self.accounted = self.start   # 伤害已处理到哪里
        self.calm = 0.0               # 这之前不短停（见 CALM）
        self.tune_at, self.tempo = 0.0, ()  # 现在这段节拍是从什么时候开始的、节拍本身（见 beat）
        self.peek = None              # 追赶时倒下了，从什么时候起看对手还打不打（None = 没在看，见 PEEK）
        self.swept, self.keen = False, True  # 这条命是被全场秒杀打倒的；这一回对手还在打的话起不起来（见 KEEN）
        self.wake = self.start + r.draw(r.WARMUP)  # 这之前不上报
        self.burst = None             # 这条命打到什么时候（None = 没在打）
        self.rate = 0.0               # 这条命平均每秒打多少
        self.born = self.start        # 这条命从什么时候开始的
        self.report_gap = random.uniform(*r.WORLD_INTERVAL)
        self.active_at = max(self.start, self.wake - self.report_gap)  # 短停结束前不产生伤害；先开始攻击，实际累计一轮后才到首次上报时刻
        self.window = (self.start, self.start)  # 这次上报算的时间段
        self.area = 0.0               # 这段时间的节拍伤害（倍率秒数）

    def tune(self) -> None:
        """按现在读到的装备定速度。自己的装备、技能不在进房应答里，在随后推来的 onJoinWorldBossCopy 里：开打时多半还没到，到了要重算
        （2026-10-11 真机：一直按“读不到”打，装备倍数从没生效）。"""
        room = self.room
        signature = (room.power, room.gear, room.dry, room.mana)
        if self.tuned == signature:
            return
        first, old_might, self.tuned = self.tuned is None, self.might, signature
        might = room.power or r.UNKNOWN_POWER
        might *= r.scale(might) * (room.gear or 1.0) * r.HARD.get(room.boss, 1.0)
        if not first and self.rate and old_might:
            self.rate *= might / old_might
        self.might = might
        self.top = r.CHASE[1] * might / room.hp_max * 100  # 追赶时在打的时候最快每秒打掉 Boss 百分之几
        self.every = r.ABYSS_EVERY if room.dry else r.PAUSE_EVERY  # 正常输出时平均多少秒短停一次
        self.usual = (r.PACE[0] + r.PACE[1]) / 2 * might * r.LIVE * (r.ABYSS_STAY if room.dry else 1.0) / room.hp_max * 100  # 照正常输出平均每秒打掉 Boss 百分之几（算上等复活）
        gear = f"{room.gear:.2f}" if room.gear else "读不到，按主线饰品、没有红莲算"
        head = f"进房后先等 {self.wake - self.start:.0f} 秒再出手；" if first else "房间推来的资料和进房前查的不一样，按新的算；"
        tail = f"；{dict(r.GEARS)[room.gear_mode]}档" + ("；读不到自己的战力，按 100 万战力的速度打，不做强敌退出判断" if room.power is None else "") if first else ""
        self.ctx.log(f"世界Boss：{head}装备倍数 {gear}；满蓝大约能撑 {room.mana:.0f} 秒{tail}")

    def absorb(self) -> None:
        for route, body in self.ctx.session.drain_routed():
            self.room.absorb(route, body, time.monotonic())
        self.tune()
        look(self.ctx.request, self.room.journal, time.monotonic())

    def standing(self) -> str:
        room = self.room
        return f"占比 {room.pct:.2f}%，{r.BOARDS.get(room.kind, '榜未知')}第 {room.rank or '—'} 名"

    def spent(self) -> float:
        """追赶时这条命打到什么时候：被普通招式打死，或者蓝空了再撑几秒就送死回蓝，哪个先到算哪个。"""
        room = self.room
        return self.born + min(r.life_seconds(room.boss, room.power), room.mana + random.uniform(*r.GIVE_UP))

    def quit_reason(self, now: float, left: float) -> str | None:
        """这个房间该不该走；该走 → 说明。"""
        room = self.room
        if room.lost():
            return f"Boss 血量只剩 {room.hp / room.hp_max:.1%}，我还不在前 {r.TOP}（{self.standing()}）"
        low = room.gear_mode == r.LOW  # 低档不追：照正常输出够不到就算够不到
        need = room.hopeless((self.usual if low else self.top * r.DUTY) * left if left else None, left)
        self.doomed = None if need is None else now if self.doomed is None else self.doomed
        if need is not None and now - self.doomed >= r.DOOMED:
            why = f"领奖门槛 {need:g}%" if need <= room.threshold else f"对手里第 {r.TOP} 高的（估到 Boss 死时 {need:.2f}%）"
            return f"Boss 血量只剩 {room.hp / room.hp_max:.1%}，按{'正常输出' if low else '追赶'}的速度打到它死也够不到{why}（{self.standing()}）"
        strong = sorted(room.strong(), reverse=True)
        if len(strong) >= r.TOP and not room.safe():
            return f"同榜有 {len(strong)} 人占比已到 {r.QUIT_SHARE:g}%、按战力和装备估的出伤比我高 {r.NEAR:g}% 以上（最高是我的 {strong[0]:.2f} 倍），争不到前 {r.TOP}"
        return None

    def steer(self, now: float, left: float, share: float | None) -> float:
        """定这一轮追不追、记下打法的变化；返回对手里第 3 高的（估到 Boss 死时）。"""
        room = self.room
        room.lead = r.along(r.LEAD, 1.0 if share is None else share) * self.swing  # Boss 血越多，后面的变数越大，领先要留得越多
        target, rivals, third = room.target(left), len(room.scoring()), room.third(left, ghosts=True)
        mine = room.pct + self.usual * left  # 照正常输出打到 Boss 死，我能到多少（left = 0：估不出来，就看现在的）
        pressed = room.pressed()  # 用户 2026-10-10 定：同榜有战力相近的人压着，不等估算、不等凑够 3 个对手，直接追
        if room.gear_mode == r.MID:  # 低档从不追、高档一直追：不切换
            self.switch(now, mine, target, third, pressed)
        if self.plan != (rivals >= r.TOP, self.chasing):
            self.plan = (rivals >= r.TOP, self.chasing)
            line = f"{self.plan_line(rivals, mine, target, pressed)}；{self.standing()}"
            self.ctx.log(f"世界Boss：{line}；{room.roster()}")
            room.journal and room.journal.plan(target, line, now)
        return third

    def switch(self, now: float, mine: float, target: float, third: float, pressed: bool) -> None:
        room = self.room
        if self.chasing:
            if mine >= target and not pressed:  # 照正常输出也稳了：从下一条命起回到正常输出，领先重新取
                self.chasing, self.swing = False, random.uniform(*r.SWING)
        elif pressed or (third and mine < third + room.lead * r.RESUME):  # 照正常输出打下去领先不够 / 会落后，或者被战力相近的人压着
            self.chasing = True
            if self.burst is not None:
                self.burst, self.rate = max(now, self.spent()), random.uniform(*r.CHASE) * self.might
            elif self.sent:  # 正等复活：看对手还打不打，再定起不起来（还没到第一次出手的不算）
                self.peek = now

    def plan_line(self, rivals: int, mine: float, target: float, pressed: bool) -> str:
        room = self.room
        if room.gear_mode == r.HIGH:
            way = "高档：全程追赶"
        elif room.gear_mode == r.LOW:
            way = "低档：只照正常输出"
        elif self.chasing:
            why = "前面有出伤相近的人，" if pressed else f"照正常输出打到 Boss 死估计 {mine:.2f}%，不够稳，"
            way = f"{why}加大输出追到估计 {target:.2f}%（按他们现在的速度估，多留 {room.lead:.1f}）"
        else:
            way = "正常输出"
        return f"占比到 {r.QUIT_SHARE:g}% 的同榜对手 {rivals} 人，{way}"

    def kill_clock(self, now: float, share: float | None) -> None:
        """全场秒杀的钟：我正在等复活 / 还没出手就不算。"""
        room = self.room
        if self.kill_at is None and share is not None and share <= r.KILL_HP and room.boss in r.KILL_BOSSES:  # 掉到半血：放第一次（进房还不到 KILL_WARM 秒就等到满）
            self.kill_at = max(now, self.start + r.KILL_WARM)
        if self.kill_at is not None and now >= self.kill_at:
            self.kill_at, self.struck = self.kill_at + r.KILL_EVERY + random.uniform(*r.KILL_CAST), now
            if self.burst is not None:
                self.burst, self.swept = min(self.burst, now + random.uniform(*r.KILL_LAG)), True
        fell, crowd = (self.kill_at is not None and room.wiped(now)) or (None, 0)
        if fell is None:
            return
        told = self.struck is not None and fell - self.struck <= r.WIPE_SAME  # 这一次我的钟已经报过了
        if (told or self.kill_at - fell <= r.WIPE_SAME or crowd >= r.WIPE_ODD) and not (told and self.synced == self.struck):  # 同房间的人刚一起倒了：钟照他们的对
            if not told and self.burst is not None and self.born < fell:  # 钟没报这一次、这条命那时候就在打：跟着倒
                self.burst, self.swept = now, True
            # 一次秒杀只对一回，用最先看到的那批人：原来看到几轮对几轮，后几轮里掺进了晚几秒才停手的，“他们停手的时刻”越对越晚，
            # 下一次就比同房间的人晚倒八到十秒（2026-10-12 拿 10-11 晚上三场重放看出来的：别人 299–312 秒停，我 312 秒才跟着倒）
            self.kill_at, self.struck = fell + r.KILL_EVERY + random.uniform(*r.KILL_CAST), max(fell, self.struck or fell)
            self.synced = self.struck

    def get_up_early(self, now: float, left: float, third: float) -> None:
        """追赶时躺着：对手还在打 / 躺不起就用复活道具起来。"""
        room = self.room
        if self.peek is not None and now < self.wake and now >= self.peek and self.chasing and (
                self.keen and room.pushing(now) or r.pressing(room.pct, self.top * r.DUTY, left, self.wake - now, third)):
            self.wake, self.peek = min(self.wake, now + random.uniform(*r.REVIVE_NOW)), None
            self.active_at = min(self.active_at, max(now, self.wake - self.report_gap))

    def new_life(self, now: float) -> None:
        """新的一条命：蓝是满的。"""
        room = self.room
        self.born, self.last, self.swept, self.calm = now, None, False, now + r.CALM
        self.accounted = self.active_at = now
        self.tune_at, self.tempo = now, r.event_beat(room.boss)
        self.burst, self.rate = (self.spent() if self.chasing else now + r.life_seconds(room.boss, room.power)), random.uniform(*(r.CHASE if self.chasing else r.PACE)) * self.might
        if room.gear_mode == r.LOW:
            self.burst = min(self.burst, now + max(r.LOW_LIFE[0], room.mana) + r.LOW_LIFE[1])

    def fall(self, now: float, gap: float, share: float | None) -> None:
        """这条命到头了：等复活。"""
        self.wake, self.burst = (now if self.last is None else self.last) + r.draw(r.REST) + random.uniform(*r.RISE), None
        self.last = None
        if not self.chasing and random.random() < r.LINGER_ODDS:
            self.wake += r.draw(r.LINGER)
        self.report_gap = gap
        self.active_at = max(now, self.wake - self.report_gap)
        self.peek, self.keen = now + random.uniform(*r.PEEK) if self.chasing else None, not self.swept or random.random() < r.along(r.KEEN, 1.0 if share is None else share)

    def measure(self, now: float) -> float:
        """这一轮算到哪里：记下时间段和节拍伤害，返回有效秒数。零伤害也推进，下一次不重复计算这个区间。"""
        self.window = (max(self.accounted, self.active_at, self.born), now)
        self.accounted = now
        self.area, self.tempo, self.tune_at = r.beat_damage(self.room.boss, self.tempo, self.tune_at, self.born, self.room.mana, *self.window)
        return max(0.0, self.window[1] - self.window[0])

    def reported(self, now: float, hurt: int, reply: Any) -> None:
        self.sent += 1
        self.last = now
        room = self.room
        room.journal and room.journal.sent(hurt, reply, time.monotonic())
        self.absorb()
        if self.sent % 4 == 0:  # 在打的时候大约 10 秒一条
            self.ctx.log(f"世界Boss：已上报 {self.sent} 次，{self.standing()}；{room.roster()}")

    def next_gap(self, now: float, elapsed: float, gap: float) -> float:
        """上报完等多久：按已过去的攻击时间算短停的机会，不短停就等到下一个伤害格。"""
        if now >= self.calm and elapsed > 0 and random.random() < min(1.0, elapsed / ((r.PAUSE_EVERY if self.chasing else self.every) - r.CALM)):
            self.report_gap = gap
            gap = r.draw(r.PAUSE)
            self.active_at = time.monotonic() + gap
            self.wake = self.active_at + self.report_gap  # 恢复攻击后先实际累计一轮，避免立即报一个极小的补偿包
            self.calm = now + gap + r.CALM
            return gap
        clock = time.monotonic()
        next_beat = next((self.tune_at + offset for offset, _ in self.tempo if self.tune_at + offset > clock), self.tune_at + self.tempo[-1][0])
        return min(gap, max(0.05, next_beat - clock))  # 到下一个伤害格再收推送，避免空轮再等完整一轮而攒成大包

    def run(self) -> tuple[str, str]:
        """返回 (DONE / FAILED / QUIT / NO_REWARD, 说明)。退出副本由调用方负责。"""
        ctx, room = self.ctx, self.room
        while time.monotonic() < self.deadline:
            if ctx.stopped:
                return r.FAILED, STOPPED
            self.absorb()
            if room.finished is not None:
                break
            if room.hp is not None and room.hp <= 0:  # Boss 已倒：等结算推送
                try:
                    room.finished = ctx.wait_push(r.P_FINISH, max(1.0, self.deadline - time.monotonic()))
                except p.AllianceError:
                    pass
                break
            now = time.monotonic()
            left = min(max(0.0, self.end - now), room.eta(now) or 0.0)  # Boss 还能活几秒；估不出来 → 0（对手的占比就看现在的）
            reason = self.quit_reason(now, left)
            if reason:
                return r.QUIT, reason
            share = None if room.hp is None else room.hp / room.hp_max
            third = self.steer(now, left, share)
            self.kill_clock(now, share)
            gap = random.uniform(*r.WORLD_INTERVAL)
            self.get_up_early(now, left, third)
            if self.burst is None and now < self.active_at:  # 还没有恢复攻击，只收推送
                if not ctx.sleep(max(0.2, min(gap, self.active_at - now)), background=False):
                    return r.FAILED, STOPPED
                continue
            if self.burst is None:
                self.new_life(now)
            elif now >= self.burst:
                self.fall(now, gap, share)
                continue
            send_at = max(self.wake, self.last + r.WORLD_INTERVAL[0] if self.last is not None else self.wake)
            if now < send_at:  # 先累计真实伤害；单次观察推送过密也不突破客户端的最短发送间隔
                if not ctx.sleep(max(0.2, min(gap, send_at - now)), background=False):
                    return r.FAILED, STOPPED
                continue
            elapsed = self.measure(now)
            if elapsed > 0 and self.area > 0:  # 原记录中有效的正伤害均保留；零增量不报
                hurt = r.world_hurt(self.rate * (self.area / elapsed), elapsed)
                try:
                    reply = ctx.request(r.R_WORLD_HIT, self.hit_body(hurt))
                except p.AllianceError as exc:
                    self.absorb()
                    if room.finished is None:
                        return r.FAILED, f"伤害上报停止：{exc}"
                    break
                self.reported(now, hurt, reply)
            gap = self.next_gap(now, elapsed, gap)
            if room.finished is None and not ctx.sleep(gap, background=False):
                return r.FAILED, STOPPED
        if room.finished is None:  # 时间到了还没收到结算：再等一小会儿
            try:
                room.finished = ctx.wait_push(r.P_FINISH, 10)
            except p.AllianceError:
                return r.FAILED, f"上报 {self.sent} 次，{self.standing()}；等待结算推送超时"
        note = verdict(room)
        return (r.DONE if r.finish_success(room.finished) else r.NO_REWARD), note


def world_fight(ctx: TaskContext, room: Room, hit_body: Callable[[float], dict], left: float) -> tuple[str, str]:
    return Fight(ctx, room, hit_body, left).run()
