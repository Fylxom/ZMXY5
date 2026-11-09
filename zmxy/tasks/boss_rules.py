"""世界 Boss 的打法参数和不看房间局面的小算法。各参数怎么量的见 docs/CHANGELOG.md、docs/LESSONS.md。

别的文件一律写 ``r.PACE`` 取（不用 from import）：测试和离线对照实验替换这里的值、函数时，各处才都看得到。
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from .. import api, data, state
from . import boss_beat

R_WORLD_LIST = "scene.worldBossHandler.getWorldBossRoomList"
R_WORLD_JOIN = "scene.worldBossHandler.joinWorldBossCopy"
R_WORLD_HIT = "scene.worldBossHandler.attack"
R_WORLD_EXIT = "scene.worldBossHandler.exitWorldBossCopy"
R_LOOK = api.R_OFFLINE_USER_INFO  # 点开别人看资料：user.powerAttr 是最终面板
LOOK_GAP = (2.0, 5.0)        # 对局中查同场玩家的面板，隔这么多秒查一个
FALLBACK_SECONDS = 1800
GRACE_SECONDS = 60
# 世界 Boss
TOP = 3                      # 排名奖励：自己榜的前三
THRESHOLD = 5.0              # 排名奖励的伤害门槛（%）；以 WorldBossHelper.damage 为准
DEFAULT_PERCENT = (5.2, 5.8) # 追赶目标的下限（%），每次执行随机取
LEAD = ((0.0, 1.0), (0.1, 1.5), (0.2, 3.0), (0.4, 5.5), (0.6, 6.0), (1.0, 6.0))  # 要比对手里第 3 高的多出几个百分点：(Boss 剩余血量比例, 百分点)
SWING = (0.9, 1.15)          # LEAD 再乘这个范围里的一个倍数，每追完一次重新取
RESUME = 0.4                 # 领先只剩 LEAD 的这么多就开始追赶
GHOST = 0.15                 # 对手不够 3 个时，缺的名额按“后面会进来的人”算：每人打掉 Boss 剩余血量的这么多
LIVE = 0.61                  # 正常输出时在打的时间占比（实测）
NEAR = 10.0                  # 预估出伤不比我高出这么多（%）的对手算“打得过”，高出的算打不过
QUIT_SHARE = 3.0             # 同榜的人占比到这个数（%）才算对手
LAST_HP = 0.05               # Boss 血量不到这个比例还不在前三：退出换房间
SAFE_HP = 0.1                # Boss 血量不到这个比例、已达标在前三：来了强敌也不退
WORLD_INTERVAL = (2.0, 3.0)  # 两次上报的间隔（秒）
# 进房后过多久才报第一笔：(累计比例, 秒)。实测 2026-10-11 旁观记录里看着进房的 124 人次（一成位 16、中位 21.5、九成位 32；40 秒以上的 6 个不学）。
# 原来只取出手快的那四分之一（14–17.5 秒）：单看一场没事，盲测里 5 场都落在同一格，太整齐
# 只学到 27 秒（真人里八成在这以内），慢的那两成不学（用户 2026-10-11：38 秒太多，影响出伤）：平均 19.7 秒，比原来晚 4 秒，一场少打 0.2 个点左右
# 2026-10-12 用户改主意，慢的也学：第 4 轮带样本的检验方拿“第一笔伤害不在 10–30 秒里”当真人的记号（真人 68 条里 30 秒以后才出手的占 18%，程序一条没有）。
# 学到 50 秒（再慢的那 5% 不学）：平均 22.5 秒，比原来晚 3 秒
WARMUP = ((0.0, 13.0), (0.12, 16.0), (0.35, 18.5), (0.6, 21.5), (0.8, 27.0), (0.9, 32.0), (0.96, 40.0), (1.0, 50.0))
STURDY = 2_000_000           # LIFE 里分高低两档的战力线
# 一条命被普通招式打死要多久（实测，只用半血以前的命）：{祖巫: ((战力从多少起, ((累计比例, 秒), …)), …)}。200 万以上的两档数据少，偏保守
LIFE = {961301: ((0, ((0.0, 15.0), (0.1, 28.0), (0.25, 35.0), (0.5, 49.0), (0.75, 63.0), (0.9, 95.0), (1.0, 115.0))),  # 五行 200 万以下
                 (STURDY, ((0.0, 40.0), (0.14, 63.0), (0.36, 71.0), (0.57, 77.0), (1.0, 115.0)))),  # 五行 200 万以上
        961401: ((0, ((0.0, 3.0), (0.1, 22.0), (0.25, 40.0), (0.5, 81.0), (0.75, 112.0), (0.9, 143.0), (1.0, 200.0))),),  # 四象
        961501: ((0, ((0.0, 9.0), (0.1, 23.0), (0.25, 39.0), (0.5, 68.0), (0.75, 97.0), (0.9, 132.0), (1.0, 184.0))),  # 三才 200 万以下
                 (STURDY, ((0.0, 85.0), (0.11, 94.0), (0.22, 101.0), (0.33, 105.0), (0.47, 115.0), (0.64, 130.0), (1.0, 142.0))))}  # 三才 200 万以上
LIFE_ANY = ((0.0, 3.0), (0.1, 26.0), (0.25, 42.0), (0.5, 74.0), (0.75, 107.0), (0.9, 143.0), (1.0, 200.0))  # 别的祖巫：三个合起来的
# 一条命里出伤怎么变（实测）：(打到第几秒, 是平均的几倍)
CURVE = ((0.0, 0.97), (14.0, 0.97), (16.0, 1.03), (44.0, 1.03), (46.0, 0.94), (59.0, 0.94), (61.0, 0.87))
# 曲线只量到 61 秒（常人一条命就这么长）。活得更久的原来一直按最后的 0.87 算：2026-10-11 真机三场，一条命 85–142 秒，在打时伤害只有同条件的人的 0.88 倍。
# 用户定：过了 CURVE_END 秒就从 CURVE_LOOP 秒那里再走一遍（技能一轮轮转着放），长命平均下来也是 1 左右。这一段是定的，不是量的
CURVE_LOOP, CURVE_END = 16.0, 61.0
LAG_MOST = 1.0               # 两次上报之间除了等的那几秒还有发请求、收应答的工夫（真机平均 0.23 秒）：这段也算在打，补进下一次的伤害里，最多补这么多秒
# 蓝空以后：(打到“满蓝能撑秒数”的几倍, 是平均的几倍)
DRAINED = ((1.0, 0.8), (1.5, 0.6))
CASTS, EASY = 4, 0.5         # 估满蓝能撑多久：最费蓝的 4 个技能、按冷却的一半频率放
MANA_USUAL = 65.0            # 算不出满蓝能撑多久时按这么多秒（实测中位）
MANA_RANGE = (30.0, 250.0)   # 算出来的只在这个范围里取
GIVE_UP = (5.0, 20.0)        # 追赶时蓝空了再撑这么多秒就送死回蓝
REVIVE_NOW = (2.0, 3.0)      # 追赶时倒下后过这么多秒接着打（用户定）
# 追赶时倒下不是一律马上起来（用户 2026-10-12 定）：先等 PEEK 秒看对手——要紧的对手（同榜、占比到 QUIT_SHARE、不比我落后一段领先以上）
# 最近 PUSHING 秒里还在涨 → 马上起来；都没在涨（多半一起挨了全场秒杀）→ 照常躺着，谁先接着打了我再起来。
# 原来一律两三秒就起来：全场秒杀时别人都倒、就我站着，线上一长段没有平台，用户自己看图认得出
# 躺不起的时候不管对手打不打都起来（用户 2026-10-12 让我定）：差得多、Boss 血又少——躺完这一回，剩下的时间照追赶的速度打到 Boss 死
# 也够不到“对手里第 3 高的（估到 Boss 死时）”，那就不躺（``pressing``）。估不出 Boss 还能活多久时不算
PEEK = (3.0, 5.0)
PUSHING = 5.0
# 被全场秒杀打倒的那几回，Boss 血越多越肯躺（用户 2026-10-12 定：半血那一下尽量多扛，血越少越要少扛）：(Boss 血量比例, 对手还在打时我起来的可能)。
# 半血时后面还长，躺一回追得回来；快死了躺不起。躺不起（pressing）时不看这个，照样起来。被普通招式打死的不看这个
KEEN = ((0.1, 1.0), (0.5, 0.2))
KILL_HP = 0.5                # 全场秒杀：Boss 血量掉到这个比例以下才放，活着的必倒
KILL_WARM = 30.0             # 进房不到这么久不放第一次
KILL_EVERY = 90.0            # 之后隔这么久再放
KILL_BOSSES = (960501, 960701, 960801, 960901, 961301, 961401, 961501)  # 有全场秒杀的祖巫：土、雨、雷、电、五行、四象、三才
KILL_CAST = (1.0, 5.0)       # 放一次本身要的工夫：两次实际隔 KILL_EVERY + 这么多秒（实测 2026-10-11 旁观记录 16 个间隔：中位 91.8、平均 93.4）
KILL_LAG = (0.0, 6.0)        # 过线后几秒内倒下
# 看同房间的人认全场秒杀（盲测第 2 轮同场检验方：我按自己的钟倒，和房间里多数人对不上，别人一起倒的时候我站着还打出最高的一段）。
# 只看正在打的人（停手前 BUSY[0] 秒里占比涨过 BUSY[1] 回以上；挂着偶尔蹭一下的不算）：占比已经 STALL[0]–STALL[1] 秒没涨 = 刚倒；
# 这样的人有 WIPE_MIN 个以上、又不少于还在涨的人 = 房间刚挨了一次。只在半血以后认
STALL = (6.0, 14.0)
BUSY = (12.0, 3)
WIPE_MIN = 2
WIPE_SAME = 20.0             # 看到的和自己钟上的差在这么多秒以内 = 同一次：钟已经报过就只对钟，不再倒一回
WIPE_ODD = 3                 # 和钟对不上的（差得比 WIPE_SAME 多）要这么多人一起倒才认：两个人碰巧同时倒不算
# 倒下后停多久（实测，只学 38 秒以内的）：(累计比例, 秒)。量的是“最后一笔伤害到下一笔伤害”，所以从这条命最后一次上报算起、到点就出手
# （原来从发现倒下那一刻算、醒了还要等到下一轮：实际隔 39–40 秒，比同房间的人晚起来一格。2026-10-11 重量：四象中位 35.5、五行 36.5、三才 37.6）
REST = ((0.0, 28.0), (0.011, 30.0), (0.067, 32.0), (0.292, 34.0), (0.672, 36.0), (1.0, 38.0))
# 复活了没马上接着打（用户 2026-10-12 定，学）：真人 241 次倒地里 17% 从最后一笔到下一笔隔了 40 秒以上（多 3–8 秒的占四成多，最长一分多钟），
# 41% 的人整场至少有一次；程序一次都没有，带样本的检验方拿这个当真人的记号。LINGER = 多躺几秒：(累计比例, 秒)。追赶时不学，到点就起
LINGER_ODDS = 0.17
LINGER = ((0.0, 3.0), (0.43, 8.0), (0.6, 13.0), (0.9, 30.0), (1.0, 60.0))
RISE = (0.5, 2.5)             # 起来以后再过这么多秒第一笔伤害才到（放进旁观房间按 5 秒一格比：不加的话躺 5 格的占 16%，真人只有 4%）
PAUSE = ((0.0, 6.0), (0.6, 7.6), (0.82, 9.3), (0.95, 13.1), (1.0, 19.0))  # 活着时的短停有多长（实测）
PAUSE_EVERY = 53.0           # 活着时平均多少秒短停一次（实测）
# 一条命刚开始、刚短停过的这么多秒里不短停（第 4 轮带样本的检验方靠“碎”认出来的：真人站住了就连着打十几二十格，
# 程序的短停撒得太匀——开场打一两下就停的真人 68 条里 1 条、程序两成；夹在两次停手中间的小段是真人的两倍多）。平均多久一次不变
CALM = 20.0
PACE = (0.207, 0.293)        # 正常输出：BASE_POWER 战力、主线饰品、没有红莲时每秒伤害 = 战力 × 这个范围里的数（实测 0.255 的 0.81–1.15 倍）
CHASE = (0.268, 0.293)       # 追赶：只取 PACE 靠上的一段；深渊 + 红莲的 200 万号不超过 0.44（用户定）
ABYSS = 1.33                 # 深渊品质的饰品（quality ≥ 8）：伤害乘这么多（实测）
ABYSS_EVERY = 53.0           # 带深渊饰品的多少秒短停一次。原来的说法（用户 2026-10-10）：深渊饰品加的蓝和回蓝少、技能断得勤，按 35 秒一次、整场只多两成算；
                             # 2026-10-11 新表里看不出来（带深渊的 58 秒一次、带主线的 43 秒一次，主线只有 39 人次），先和别人用同一个数，数据多了再看
ABYSS_STAY = 0.97            # 带深渊饰品的在打时间是别人的这么多（实测）
LOTUS = 1.09                 # 红莲 5 级以上的器灵：再乘这么多（实测）
# 在打的时候一阵高一阵低：照搬真人的节拍（``boss_beat.BEATS``，``tools/boss_beat.py`` 从旁观记录量的），不自己编。
# 原来是两层随机数（单次大小 SPREAD + 每 5–13 秒换一次的 SURGE）：用户 2026-10-11 看走势图认程序，三组 6 个里认中 4 个——
# 真人的线上是一个个差不多大的山丘，程序的大小不一、没有规律。量出来真人隔 30 秒左右会再来一次大的（一格能到平均的 4 倍），相邻两格反而不像；随机数做不出这个
BASE_POWER = 2_000_000       # PACE / CHASE 是按这个战力量的
# 各祖巫上打出来的是 PACE 的几倍。五行 / 四象 / 三才是实测的；其余 12 个是按客户端伤害公式估算的（Boss 等级 = 玩家等级），没上场量过
HARD = {961301: 1.04, 961401: 1.04, 961501: 0.94,  # 实测：五行、四象、三才
        960101: 1.21, 960201: 1.21, 960301: 1.20, 960401: 1.20, 960501: 1.18, 960601: 1.18,  # 估算：金、木、水、火、土、风
        960701: 1.17, 960801: 1.16, 960901: 1.15, 961001: 1.14, 961101: 1.12, 961201: 1.11}  # 估算：雨、雷、电、天气、空间、时间
SCALE = 0.69                 # 系数随战力变：× (战力 / BASE_POWER) ** SCALE（实测）
CEIL_POWER = 3_500_000       # 量过的最高战力：再高系数不再涨
UNKNOWN_POWER = 1_000_000    # 战力读不到时按这个定速度
ETA_WINDOW = 60.0            # 按最近这么多秒的掉血速度估 Boss 还能活多久
TREND = 90.0                 # 按最近这么多秒估对手的速度；看到他不满 TREND_MIN 秒不估
TREND_MIN = 20.0
DUTY = 0.88                  # 追赶时在打的时间占比
DOOMED = 20.0                # “追不上”连续这么多秒都成立才退出
BOARDS = {1: "高战榜", 2: "普通榜"}
P_JOIN, P_LEAVE, P_SELF = "onJoinWorldBossCopy", "onExitWorldBossCopy", "onWorldBossSelfHit"
P_HP, P_RANK, P_FINISH = "onWorldBossHpChange", "onWorldBossCopyKillRank", "onFinishWorldBossCopy"
DONE, FAILED, QUIT, NO_REWARD = "done", "failed", "quit", "no_reward"
LOW, MID, HIGH = "low", "mid", "high"  # 三档：低 = 只正常输出；中 = 不够稳才追赶；高 = 全程追赶
GEARS = ((LOW, "低"), (MID, "中"), (HIGH, "高"))
LOW_LIFE = (90.0, 25.0)      # 低档一条命最长 = max(第一个数, 满蓝能撑秒数) + 第二个数（不给对比表判“无限蓝”的机会）


def real(v: Any) -> float | None:
    """占比、战力、血量可能带小数（state.num 会截成整数）。"""
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scale(power: float) -> float:
    """这个战力的系数是 BASE_POWER 战力的几倍。"""
    return (min(power, CEIL_POWER) / BASE_POWER) ** SCALE


def draw(table: tuple) -> float:
    """按 (累计比例, 值) 的分布表随机取一个值。"""
    pick = random.random()
    for (p0, v0), (p1, v1) in zip(table, table[1:]):
        if pick <= p1:
            return v0 + (v1 - v0) * (pick - p0) / (p1 - p0)
    return table[-1][1]


def along(table: tuple, x: float) -> float:
    """(x, 值) 的表里 x 处的值：相邻两行之间按直线取，超出两头取两头的。"""
    for (x0, v0), (x1, v1) in zip(table, table[1:]):
        if x <= x1:
            return v0 + (v1 - v0) * max(0.0, x - x0) / (x1 - x0)
    return table[-1][1]


def life_seconds(boss: int, power: float | None = None) -> float:
    """这条命过多久被这个祖巫的普通招式打死；``power`` = 自己的战力（读不到按低的那档）。"""
    bands = LIFE.get(boss)
    return draw(next(table for floor, table in reversed(bands) if (power or 0) >= floor) if bands else LIFE_ANY)


def flow(t: float, mana: float) -> float:
    """这条命打到第 ``t`` 秒时的出伤是平均的几倍；``mana`` = 满蓝能撑几秒，过了就掉下去。"""
    usual = along(CURVE, t if t <= CURVE_END else CURVE_LOOP + (t - CURVE_END) % (CURVE_END - CURVE_LOOP))
    return usual if t <= mana else min(usual, along(DRAINED, t / mana))


def _at_level(text: Any, level: int) -> float:
    """分号隔开的一列数里第 ``level`` 个；超出取最后一个。"""
    values = str(text).split(";")
    return real(values[min(level, len(values) - 1)]) or 0.0


def mana_seconds(member: Mapping[str, Any], panel: Any) -> float:
    """满蓝最多能撑几秒（宽着算）：带上场的技能里最费蓝的 CASTS 个只按冷却的一半频率放，再算上每秒回蓝。
    耗蓝和冷却按技能等级查 ``Skill`` 表（``consumeMP``、``worldBossCD``，分号隔开、第几个就是几级）。
    ``panel`` = 最终面板（``powerAttr``）：进房资料里的蓝只有面板的一成，不能用。算不出来 → 0。"""
    user = member.get("userdata")
    if not isinstance(user, Mapping) or not isinstance(panel, Mapping) or not real(panel.get("mpMax")):
        return 0.0
    keys = user.get("keyboard") if isinstance(user.get("keyboard"), Mapping) else {}
    carried = {str(v) for k, v in keys.items() if k.startswith("skill") and k[5:].isdigit()}  # 带上场的技能（skill1–5）；读不到就按学了的全算
    skills, spend = data.slim("Skill", "consumeMP", "cd", "worldBossCD"), []  # 整张技能表 40MB，只要这三列
    for sid, level in (user.get("skill") if isinstance(user.get("skill"), Mapping) else {}).items():
        one = skills.get(str(sid))
        if one and (not carried or str(sid) in carried):
            rank = int(real(level) or 0)
            cost, cd = _at_level(one.get("consumeMP"), rank), _at_level(one.get("worldBossCD") or one.get("cd"), rank)
            if cost > 0 and cd > 0:
                spend.append(cost / cd)
    if not spend:
        return 0.0
    drain = sum(sorted(spend, reverse=True)[:CASTS]) * EASY - (real(panel.get("reMP")) or 0.0)
    return MANA_RANGE[1] if drain <= 0 else min(max(real(panel["mpMax"]) / drain, MANA_RANGE[0]), MANA_RANGE[1])


def abyss(member: Mapping[str, Any]) -> bool:
    """房间成员带的是不是深渊品质的饰品（quality ≥ 8）；没带装备信息 → False。"""
    user = member.get("userdata")
    gear = user.get("equipments") if isinstance(user, Mapping) else None
    ornament = gear.get("ornament") if isinstance(gear, Mapping) else None
    return isinstance(ornament, Mapping) and (state.num(ornament.get("quality")) or 0) >= 8


def gear_of(member: Mapping[str, Any]) -> float | None:
    """房间成员的装备让他同战力下多打多少（倍数）：深渊饰品 × ABYSS，红莲 5 级以上的器灵 × LOTUS；没带装备信息 → None。"""
    user = member.get("userdata")
    gear = user.get("equipments") if isinstance(user, Mapping) else None
    if not isinstance(gear, Mapping):
        return None
    ornament, spirit = (gear.get(k) if isinstance(gear.get(k), Mapping) else {} for k in ("ornament", "weaponSpirit"))
    lotus = str(spirit.get("id") or "")  # 红莲：2800020 + 等级
    return (ABYSS if (state.num(ornament.get("quality")) or 0) >= 8 else 1.0) * (LOTUS if lotus.startswith("2800020") and lotus[-1] >= "5" else 1.0)


def lag(now: float, last: float | None, slept: float) -> float:
    """上一次上报到现在，比说好要等的 ``slept`` 秒多花了几秒（发请求、收应答）；这条命的第一下（``last`` 是 None）没有。最多 LAG_MOST。"""
    return 0.0 if last is None else min(LAG_MOST, max(0.0, now - last - slept))


def pressing(share: float, speed: float, left: float, rest: float, third: float) -> bool:
    """躺不起：现在占比 ``share``，躺完还要 ``rest`` 秒，Boss 还能活 ``left`` 秒，起来以后每秒涨 ``speed``，到 Boss 死够不到 ``third``。
    ``left`` / ``third`` 是 0（估不出来 / 没有可比的）→ 不算。"""
    return bool(left and third) and share + speed * max(0.0, left - rest) < third


def beat(boss: int) -> tuple[float, ...]:
    """抽一段真人的节拍（每 ``boss_beat.STEP`` 秒是平均的几倍）：先在这个祖巫的里面抽，没量过的祖巫在全部里面抽。"""
    return random.choice(boss_beat.BEATS.get(boss) or [part for parts in boss_beat.BEATS.values() for part in parts])


def beat_average(tempo: tuple[float, ...], at: float, start: float, end: float) -> float:
    """算一段上报时间跨过的节拍平均倍率，而不是只取结束时刻的那一格。"""
    if not tempo or end <= start:
        return 1.0
    step, total, cursor = boss_beat.STEP, 0.0, start
    length = len(tempo) * step
    while cursor < end:
        offset = cursor - at
        if offset < 0:
            index, edge = 0, at
        elif offset >= length:
            index, edge = len(tempo) - 1, end
        else:
            index = min(int(offset / step), len(tempo) - 1)
            edge = at + (index + 1) * step
        stop = min(end, edge)
        if stop <= cursor:
            stop = end
        total += tempo[index] * (stop - cursor)
        cursor = stop
    return total / (end - start)


def event_beat(boss: int) -> tuple[tuple[float, float], ...]:
    """用实际观察时刻的增量片段；旧节拍库按格尾转换，供旧数据兼容。"""
    pools = boss_beat.EVENTS
    parts = pools.get(boss) or [part for values in pools.values() for part in values]
    if parts:
        return random.choice(parts)[1]
    return tuple(((index+1)*boss_beat.STEP, value*boss_beat.STEP) for index,value in enumerate(beat(boss)))


def beat_damage(boss: int, tempo: tuple[tuple[float, float], ...], at: float, born: float, mana: float,
                start: float, end: float) -> tuple[float, tuple[tuple[float, float], ...], float]:
    """累计 (start, end] 内已发生的节拍伤害，返回倍率秒数及接续的片段。

    使用原记录的增量时刻，不再把伤害摊成连续速率后重采样。
    只累计有效攻击区间，跨片段时先取完旧片段的尾部。
    """
    if end <= start or not tempo:
        return 0.0, tempo, at
    total = 0.0
    while True:
        edge = at + tempo[-1][0]
        for offset, value in tempo:
            when = at + offset
            if start < when <= end:
                total += value * flow(when - born, mana)
        if end <= edge:
            return total, tempo, at
        at, tempo = edge, event_beat(boss)


def world_hurt(rate: float, gap: float) -> int:
    """这次上报多少：``rate`` = 这一下每秒打多少，``gap`` = 这一下算几秒的。"""
    return max(1, round(rate * gap))


def output(power: float | None, gear: float | None) -> float | None:
    """预估出伤（只用来比大小）：战力 × scale × 装备倍数；战力读不到 → None，装备读不到按 1。"""
    return None if not power else power * scale(power) * (gear or 1.0)


def power_of(member: Mapping[str, Any]) -> float | None:
    """房间成员 ``{uid, userdata{…}}`` 的战力；读不到 → None。"""
    for box in (member.get("userdata"), member):
        if isinstance(box, Mapping):
            for key in ("fightPower", "power"):
                if real(box.get(key)):
                    return real(box[key])
    return None


def members(body: Any) -> list[Mapping[str, Any]]:
    """进房应答 / onJoinWorldBossCopy 里的成员列表（直接是列表，或包在 ``message`` / ``playerInfos`` 里）。"""
    for key in ("message", "playerInfos"):
        if isinstance(body, Mapping):
            body = body.get(key, body)
    return [m for m in body if isinstance(m, Mapping) and state.num(m.get("uid")) is not None] if isinstance(body, list) else []


def my_rank(rows: Any, uid: int | None) -> int | None:
    """榜单行 ``{uid, rank, percent}`` 里我的名次；榜上找不到我 → None（视为没达标）。"""
    for row in rows if isinstance(rows, list) and uid is not None else ():
        if isinstance(row, Mapping) and state.num(row.get("uid")) == uid:
            return int(state.num(row.get("rank")) or 0) or None
    return None


def finish_success(done: Any) -> bool:
    """客户端按真值判断 success；兼容布尔值、0/1 数字串，缺字段按失败处理。"""
    if not isinstance(done, Mapping):
        return False
    value = done.get("success")
    return value is True or state.num(value) == 1 or str(value).lower() == "true"
