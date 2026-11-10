"""按开放时段的定时项（规则；设置和选哪些存档在 globalcfg）：每个任务的开放时段（读数据表）、到点判断、网络失败后的重试。
时间一律是服务器时间（servertime.clock，不带时区的北京时间）。

两类任务：
* 时段类（SESSION_TASKS）：斗神大会、灵宠乱斗、通天河三项、世界Boss。每天的开放时段读表（客户端同样是 OpenTime[类型 + 星期]，
  周日 = 0）：通天河平时只开周六周日，国庆前后的版本每天开，按当天的表走，不用选星期；世界Boss另外选星期。
  设置 {"session": noon/evening/both, "delay": 开场后几分钟, "weekdays"（只有世界Boss）}；“中午”= 当天第一个时段，“晚上”= 最后一个
  （只有一个时段的日子两个是同一个；世界Boss周末 9–21 点一整段按 15:00 劈成两场，见 sessions）。开场后 delay 分钟起、时段结束前 MARGIN 分钟内都算到点：关机错过了，开机还在时段里就补做。
* 报名类（APPLY_TASKS）：竞技场 / 斗宠大会报名，报名期（ChallengeApply，周五 16:00–周日 22:00）里第一次看到就去报，不用填时间。

做过的记在 cfg["done"] = {"day": 时段所在的日期, "windows": [时段序号]}（整个定时项一份，到点时选中的存档一起排队）；报名类的 day 是这一届报名期开始那天。
  "skip": [时段序号] = 刚打开 / 改设置时这一场已经过了开始时间，不补做（settle），设置再改时会重新看（unskip）；
  "full": [登录名#存档] = 这一天已经做满（任务结果 day_done），后面的场次不再登录。
登录时网络失败（连不上 4399 / 游戏服务器）：同一时段内过 RETRY_AFTER 分钟各再试一次，按 “登录名#存档” 各记各的，在 cfg["retry"][键]。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

DAILY = ("pvp_match", "pet_brawl")
TONGTIAN = ("tongtian_hire", "tongtian_escort", "tongtian_consult")
WORLD = "world_boss"
SESSION_TASKS = DAILY + TONGTIAN + (WORLD,)
APPLY_TASKS = ("arena_apply", "pet_arena_apply")
TIMER_IDS = frozenset(SESSION_TASKS + APPLY_TASKS)
SESSIONS = (("noon", "中午"), ("evening", "晚上"), ("both", "都做"))
DEFAULT_SESSION, DEFAULT_DELAY, MAX_DELAY = "evening", 5, 120
MARGIN = {"tongtian_escort": 1, WORLD: 10}  # 时段结束前留多少分钟不再开始：护送能发一条是一条（发不完的任务自己停），其余 5 分钟
MARGIN_DEFAULT = 5
SPLIT_AT = {WORLD: 15 * 60}  # 一整段跨过这个时刻（世界Boss周末 9–21 点）就按它劈成中午 / 晚上两场
RETRY_AFTER = (5, 15, 30)  # 网络失败后过几分钟再试（第 1、2、3 次）
APPLY_ROWS = {"arena_apply": 1, "pet_arena_apply": 2}  # ChallengeApply 的行
_CN = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Window:
    start: int  # 当天第几分钟
    end: int

    def text(self) -> str:
        return f"{_hm(self.start)}–{_hm(self.end)}"


def _hm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_windows(text: Any) -> list[Window]:
    """"12:00:00-14:00:00;19:00:00-21:00:00" → 时段列表；有一段读不懂就整个不认（宁可不做也不算错时间）。"""
    out = []
    for part in str(text or "").split(";"):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2}):(\d{2})-(\d{1,2}):(\d{2}):(\d{2})\s*", part)
        if not m:
            return []
        h1, m1, _s1, h2, m2, _s2 = map(int, m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59 or end <= start:
            return []
        out.append(Window(start, end))
    return sorted(out, key=lambda w: w.start)


def _week(weekday: int) -> int:
    """datetime.weekday()（周一 = 0）→ 表里的星期（周日 = 0）。"""
    return (weekday + 1) % 7


def _open_time(kind: Any, weekday: int) -> list[Window]:
    from . import data, state
    base = state.num(kind)
    row = data.row("OpenTime", base + _week(weekday)) if base is not None else None
    if not isinstance(row, Mapping) or row.get("isOpen") != 1:
        return []
    return parse_windows(row.get("time"))


def _common(key: str) -> Any:
    from . import data
    row = data.row("Common", 1)
    return row.get(key) if isinstance(row, Mapping) else None


def windows(tid: str, weekday: int) -> list[Window]:
    """这个任务某个星期几（周一 = 0）的开放时段；不开或读不到 = []。"""
    from . import data
    if tid == "pvp_match":  # 任务本身按 PvpTime 判断，这里跟它一致
        row = data.row("PvpTime", _week(weekday))
        return parse_windows(row.get("time")) if isinstance(row, Mapping) else []
    if tid == "pet_brawl":
        helper = data.row("PetBattleHelper", 1)
        return _open_time(helper.get("openTimeType") if isinstance(helper, Mapping) else None, weekday)
    if tid in TONGTIAN:
        return _open_time(_common("openTimeTongtian"), weekday)
    if tid == WORLD:
        return _open_time(_common("openTimeWorldBoss"), weekday)
    return []


def sessions(tid: str, weekday: int) -> tuple[list[Window], bool]:
    """定时用的场次：(时段列表, 是不是从一整段劈开的)。世界Boss周末一整段 9–21 点按 15:00 劈成中午 9–15、晚上 15–21；
    劈开的日子“都做”= 中午场做了晚上场就不做，中午场没运行（关机等）才在晚上场做。开不开、说明照原来的整段。"""
    wins, cut = windows(tid, weekday), SPLIT_AT.get(tid)
    if cut is not None and len(wins) == 1 and wins[0].start < cut < wins[0].end:
        return [Window(wins[0].start, cut), Window(cut, wins[0].end)], True
    return wins, False


DAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def describe(tid: str) -> str:
    """界面上的开放时间说明：同样时段的星期合在一起，如“周六、周日 12:00–15:00、19:00–22:00”。"""
    if tid in APPLY_TASKS:
        span = apply_span(tid)
        if span is None:
            return "读不到报名时间"
        (d1, m1), (d2, m2) = span
        return f"{DAY_NAMES[d1]} {_hm(m1)} – {DAY_NAMES[d2]} {_hm(m2)}"
    groups: dict[str, list[int]] = {}
    for day in range(7):
        text = "、".join(w.text() for w in windows(tid, day))
        if text:
            groups.setdefault(text, []).append(day)
    if not groups:
        return "读不到开放时间"
    if len(groups) == 1 and len(next(iter(groups.values()))) == 7:
        return f"每天 {next(iter(groups))}"
    return "；".join(f"{_days(days)} {text}" for text, days in groups.items())


def _days(days: list[int]) -> str:
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAY_NAMES[days[0]]}至{DAY_NAMES[days[-1]]}"
    return "、".join(DAY_NAMES[d] for d in days)


def apply_span(tid: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """报名期：((开始星期 周一=0, 第几分钟), (结束星期, 第几分钟))；读不到 = None。"""
    from . import data
    row = data.row("ChallengeApply", APPLY_ROWS.get(tid, 0))
    if not isinstance(row, Mapping):
        return None
    out = []
    for key in ("applyStartTime", "applyEndTime"):
        m = re.fullmatch(r"([1-7])-(\d{1,2}):(\d{2}):(\d{2})", str(row.get(key) or "").strip())
        if not m or int(m[2]) > 23 or int(m[3]) > 59:
            return None
        out.append((int(m[1]) - 1, int(m[2]) * 60 + int(m[3])))
    return out[0], out[1]


# ---- 设置
def session_of(cfg: Mapping[str, Any]) -> str | None:
    value = cfg.get("session")
    return value if value in dict(SESSIONS) else None


def delay_of(cfg: Mapping[str, Any]) -> int:
    value = cfg.get("delay")
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_DELAY else DEFAULT_DELAY


def weekdays_of(cfg: Mapping[str, Any]) -> tuple[int, ...]:
    raw = cfg.get("weekdays")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({d for d in raw if isinstance(d, int) and not isinstance(d, bool) and 0 <= d < 7}))


def valid(tid: str, cfg: Any) -> bool:
    """设置完整（时段类要选场次，世界Boss还要选星期）；开关和选存档由 globalcfg 看。"""
    if tid not in TIMER_IDS or not isinstance(cfg, dict):
        return False
    if tid in APPLY_TASKS:
        return True
    return session_of(cfg) is not None and (tid != WORLD or bool(weekdays_of(cfg)))


def _picked(session: str, count: int) -> list[int]:
    if count == 0:
        return []
    first, last = 0, count - 1
    return sorted({first, last}) if session == "both" else [first] if session == "noon" else [last]


def _done(cfg: dict[str, Any], day: str) -> list[int]:
    done = cfg.get("done")
    if isinstance(done, dict) and done.get("day") == day and isinstance(done.get("windows"), list):
        return [w for w in done["windows"] if isinstance(w, int)]
    return []


def _list(cfg: Mapping[str, Any], day: str, key: str) -> list[Any]:
    done = cfg.get("done")
    if isinstance(done, dict) and done.get("day") == day and isinstance(done.get(key), list):
        return list(done[key])
    return []


def _mark(cfg: dict[str, Any], day: str, window: int, skip: bool = False) -> None:
    old = cfg.get("done") if isinstance(cfg.get("done"), dict) and cfg["done"].get("day") == day else {}
    skipped = sorted(set(_list(cfg, day, "skip")) | {window}) if skip else [w for w in _list(cfg, day, "skip") if w != window]
    cfg["done"] = {**{k: v for k, v in old.items() if k != "skip"}, "day": day, "windows": sorted(set(_done(cfg, day)) | {window}),
                   **({"skip": skipped} if skipped else {})}


def settle(tid: str, cfg: Any, now: datetime) -> None:
    """设置刚改成“这一场会做”（打开、换场次、改开场后几分钟、勾上存档）：这一场已经过了开始时间就不补做，等下一场（调用方负责 save）。
    报名类不管：报名期好几天，中途打开也要去报。"""
    if tid not in SESSION_TASKS or not valid(tid, cfg):
        return
    here = current(tid, cfg, now)
    if here is None:
        return
    day, window, _last = here
    wins, _split = sessions(tid, now.weekday())
    if window not in _done(cfg, day) and now.hour * 60 + now.minute >= wins[window].start + delay_of(cfg):
        _mark(cfg, day, window, skip=True)


def unskip(tid: str, cfg: Any, now: datetime) -> None:
    """改设置前先撤掉今天“跳过”的场次（真做过的留着）；之后 settle 按新设置重新看（调用方负责 save）。"""
    if tid not in SESSION_TASKS or not isinstance(cfg, dict):
        return
    day = now.strftime("%Y-%m-%d")
    skipped = set(_list(cfg, day, "skip"))
    if skipped:
        cfg["done"] = {**{k: v for k, v in cfg["done"].items() if k != "skip"}, "windows": [w for w in _done(cfg, day) if w not in skipped]}


def full(cfg: Any, day: str) -> set[str]:
    """这一天已经做满的 “登录名#存档”。"""
    return {k for k in _list(cfg, day, "full") if isinstance(k, str)} if isinstance(cfg, dict) else set()


def mark_full(cfg: dict[str, Any], day: str, key: str) -> None:
    """这个存档今天做满了（任务结果 day_done）：后面的场次不再登录（调用方负责 save）。"""
    old = cfg.get("done") if isinstance(cfg.get("done"), dict) and cfg["done"].get("day") == day else {"day": day, "windows": []}
    cfg["done"] = {**old, "full": sorted(full(cfg, day) | {key})}


def current(tid: str, cfg: Mapping[str, Any], now: datetime) -> tuple[str, int, int] | None:
    """现在所在的、这个设置选了的时段：(日期, 时段序号, 时段结束前最后能开始的分钟)；不在 = None。"""
    if tid in APPLY_TASKS:
        span = apply_span(tid)
        if span is None:
            return None
        (d1, m1), (d2, m2) = span
        point = now.weekday() * 1440 + now.hour * 60 + now.minute
        start, end = d1 * 1440 + m1, d2 * 1440 + m2
        if start <= end and not start <= point < end or start > end and end <= point < start:
            return None
        begun = now - timedelta(minutes=(point - start) % (7 * 1440))
        return begun.strftime("%Y-%m-%d"), 0, 24 * 60  # 报名期跨天：结束由上面的判断管
    session = session_of(cfg)
    if session is None or (tid == WORLD and now.weekday() not in weekdays_of(cfg)):
        return None
    (wins, _split), minute = sessions(tid, now.weekday()), now.hour * 60 + now.minute
    for i in _picked(session, len(wins)):
        last = wins[i].end - MARGIN.get(tid, MARGIN_DEFAULT)
        if wins[i].start <= minute < last:
            return now.strftime("%Y-%m-%d"), i, last
    return None


def due(tid: str, cfg: Any, now: datetime) -> bool:
    """现在该不该开始，该 → 记成这个时段做过了（调用方负责 save）。
    时段类：开场 delay 分钟以后、时段结束前；报名类：报名期里。网络失败的重试另见 retries_due。"""
    if not valid(tid, cfg):
        return False
    here = current(tid, cfg, now)
    if here is None:
        return False
    day, window, _last = here
    minute = now.hour * 60 + now.minute
    if window in _done(cfg, day):
        return False
    if tid in SESSION_TASKS:
        wins, split = sessions(tid, now.weekday())
        if minute < wins[window].start + delay_of(cfg):
            return False
        if split and window > 0 and set(_done(cfg, day)) - set(_list(cfg, day, "skip")):  # 劈开的一整段：中午场真运行过（不是跳过），晚上场不再做
            return False
    _mark(cfg, day, window)
    cfg.pop("retry", None)
    return True


def _retries(cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg.get("retry"), dict):
        cfg["retry"] = {}
    return cfg["retry"]


def retries_due(tid: str, cfg: dict[str, Any], now: datetime) -> list[str]:
    """约好的重试时间到了的 [键]（调用方负责 save）；用掉这次（次数 n 留着，下一次失败接着往后数）。"""
    if not valid(tid, cfg) or not isinstance(cfg.get("retry"), dict):
        return []
    here = current(tid, cfg, now)
    if here is None:
        return []
    day, window, _last = here
    out = []
    for key, r in cfg["retry"].items():
        at = r.get("at") if isinstance(r, dict) and r.get("day") == day and r.get("window") == window else None
        if isinstance(at, str) and now.strftime("%Y-%m-%d %H:%M") >= at:
            r["at"] = None
            out.append(key)
    return out


def schedule_retry(tid: str, cfg: dict[str, Any], key: str, now: datetime) -> str | None:
    """登录时网络失败：给这个键（登录名#存档）约好下一次重试（调用方负责 save）。返回 "HH:MM"；次数用完或时段内来不及 = None。"""
    retries = _retries(cfg)
    here = current(tid, cfg, now)
    if here is None:
        retries.pop(key, None)
        return None
    day, window, last = here
    old = retries.get(key)
    n = old.get("n", 0) if isinstance(old, dict) and old.get("day") == day and old.get("window") == window else 0
    if not isinstance(n, int) or n >= len(RETRY_AFTER):
        retries.pop(key, None)
        return None
    at = now + timedelta(minutes=RETRY_AFTER[n])
    if tid in SESSION_TASKS and (at.date() != now.date() or at.hour * 60 + at.minute >= last):
        retries.pop(key, None)
        return None
    retries[key] = {"day": day, "window": window, "n": n + 1, "at": at.strftime("%Y-%m-%d %H:%M")}
    return at.strftime("%H:%M")


def closed(tid: str, now: int | float | None) -> str | None:
    """任务开始时看现在开不开（now = 服务器时间戳）：开 → None；不开或读不到 → 给用户看的说明（不登录游戏的界面也用它）。"""
    if now is None:
        return "读不到服务器时间，无法确认开放时间"
    here = datetime.fromtimestamp(now, _CN).replace(tzinfo=None)
    wins, minute = windows(tid, here.weekday()), here.hour * 60 + here.minute
    if any(w.start <= minute < w.end for w in wins):
        return None
    return f"现在不在开放时间（{describe(tid)}）"


def open_until(tid: str, now: int | float | None) -> float | None:
    """现在所在开放时段的结束时刻（服务器时间戳，秒）；不在时段里或读不到 = None。"""
    if now is None:
        return None
    here = datetime.fromtimestamp(now, _CN)
    minute = here.hour * 60 + here.minute
    for w in windows(tid, here.weekday()):
        if w.start <= minute < w.end:
            midnight = here.replace(hour=0, minute=0, second=0, microsecond=0)
            return (midnight + timedelta(minutes=w.end)).timestamp()
    return None


def network_failure(detail: str) -> bool:
    """登录失败的说明是不是网络问题（login.explain 的写法）；密码错、Cookie 失效、验证码这些重试也没用。"""
    return detail.startswith("登录/进入角色失败：") and ("连接 4399 平台失败" in detail or "连接游戏服务器失败" in detail)
