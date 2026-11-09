"""登录有礼（15 天签到活动）：一键领取已到天数的奖励；开了高级（至尊）的连高级奖励一起领，没开的只领免费。

* 数据：``LandingReward15Helper`` 第 1 行给活动 id（对应 Activity 档期）；``LandingReward15`` 每行 ``day`` / ``levelType``（1 免费、2 高级），
  ``id`` 就是领取记录的键。进角 info 的 ``summerLogin[活动 id]`` = ``{days, zhizun, totalReward}``。
* 可领 = ``days >= day`` 且 ``totalReward[id]`` 为空，高级行还要 ``zhizun``；没有可领的不发请求。
* 这个存档的登录有礼结束 = 已到配置的最后签到天数，并领完当前可用档位的所有奖励；状态写入 profile。
* 领取：``getSummerLoginRewards {}``（客户端“领取”按钮，一次领完所有可领）。激活高级（``openSummerHighLvLogin``，花点券）不碰。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "login_gift"
END = "login_gift_end"  # 存档 profile 标记：当前登录有礼已经领完
R_CLAIM = "scene.activityHandler.getSummerLoginRewards"
FREE, PREMIUM = 1, 2


def activity_id() -> int | None:
    return state.num((state.row("LandingReward15Helper", 1) or {}).get("activityId"))


def _reward_rows() -> list[tuple[int, int, int]] | None:
    """配置奖励 (id, day, levelType)；读不全时不推断结束。"""
    rows, seen = [], set()
    for row in state.table_rows("LandingReward15"):
        rid, day, level = state.num(row.get("id")), state.num(row.get("day")), state.num(row.get("levelType"))
        if rid is None or day is None or day <= 0 or level not in (FREE, PREMIUM) or rid in seen:
            return None
        rows.append((rid, day, level))
        seen.add(rid)
    return rows or None


def _claimed(record: Mapping[str, Any]) -> set[int] | None:
    claimed = record.get("totalReward")
    if not isinstance(claimed, (dict, list)):
        return None
    entries = claimed.items() if isinstance(claimed, dict) else enumerate(claimed)
    out: set[int] = set()
    for key, value in entries:
        ident = state.num(key)
        if ident is None:
            continue
        if isinstance(value, str):
            value = value.strip().lower()
        if value in (True, 1, "1", "true", "yes"):
            out.add(ident)
        elif value not in (False, 0, "0", "", None, "false", "no"):
            number = state.num(value)
            if number is None or number < 0:
                return None
            if number > 0:
                out.add(ident)
    return out


def _premium(record: Mapping[str, Any]) -> bool | None:
    """读取客户端 zhizun 标记；避免把字符串 \"0\" 当成真。"""
    value = record.get("zhizun", 0)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("true", "yes"):
            return True
        if value in ("false", "no"):
            return False
    number = state.num(value)
    return bool(number) if number in (0, 1) else None


def claimable(record: Mapping[str, Any], premium: bool) -> list[int] | None:
    """还没领的可领奖励 id；配置或记录读不到返回 None。"""
    days, rows, got = state.num(record.get("days")), _reward_rows(), _claimed(record)
    if days is None or days < 0 or rows is None or got is None:
        return None
    return sorted(rid for rid, day, level in rows if day <= days and rid not in got and (level == FREE or premium))


def login_gift_end(info: Any, now: int | None = None) -> bool | None:
    """按登录信息判断当前存档是否领完或已过期；进度或配置缺失时返回 None。"""
    aid = activity_id()
    summer = info.get("summerLogin") if isinstance(info, Mapping) else None
    record = summer.get(str(aid)) if aid is not None and isinstance(summer, Mapping) else None
    rows = _reward_rows()
    if rows is None or aid is None:
        return None
    if not isinstance(record, Mapping):
        return state.activity_ended(aid, now)
    raw_end = record.get("endTime")
    if raw_end not in (None, ""):
        end_time = state.seconds(raw_end)
        if end_time is None or end_time <= 0:
            return None
        if now is not None and now > end_time:
            return True
    elif now is not None and state.activity_ended(aid, now) is True:
        # 旧存档可能没有角色覆盖的 endTime；和 run() 一样退回 Activity 档期。
        return True
    days, got = state.num(record.get("days")), _claimed(record)
    if days is None or days < 0 or got is None:
        return None
    if days < max(day for _, day, _ in rows):
        return False
    premium = _premium(record)
    if premium is None:
        return None
    required = {rid for rid, _, level in rows if level == FREE or premium}
    return required <= got


def _mark(end: bool | None) -> dict[str, bool] | None:
    return {END: end} if end is not None else None


def _closed(aid: int, now: int, end_time: Any = None) -> TaskResult | None:
    """活动不在档期内时要返回的结果；end_time 是角色自己的结束时间（旧存档没有）。"""
    if state.activity_open(aid, now, end_time=end_time):
        return None
    ended = state.activity_ended(aid, now, end_time=end_time)
    return TaskResult(ID, SKIPPED, "这个存档的登录有礼已结束" if ended else "活动未开放", profile=_mark(ended))


def _end_after_claim(ctx: TaskContext, response: Any, aid: int) -> tuple[bool | None, str]:
    """领完后重新判断结束没有：(结果, 判断不了时的说明)。领奖响应可能不带 summerLogin，没带时不能当成“没结束”，要刷新后再判断。"""
    summer = response.get("summerLogin") if isinstance(response, Mapping) else None
    end = login_gift_end(response, ctx.now) if isinstance(summer, Mapping) and str(aid) in summer else None
    if end is not None:
        return end, ""
    try:
        end = login_gift_end(ctx.refresh(), ctx.now)
    except p.AllianceError:
        return None, "；领取请求已返回，但刷新存档进度失败，结束状态待再次识别"
    return end, "；领取请求已返回，但没有读到可确认的存档进度，结束状态待再次识别" if end is None else ""


@task(ID, "登录有礼", writes={R_CLAIM}, group="活动")
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未领取登录有礼")
    aid = activity_id()
    if aid is None or ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少活动配置或服务器时间，未发送请求")
    summer = ctx.info.get("summerLogin")
    record = summer.get(str(aid)) if isinstance(summer, Mapping) else None
    if not isinstance(record, Mapping):
        return _closed(aid, ctx.now) or TaskResult(
            ID, BLOCKED, "服务器未返回登录有礼进度（summerLogin），无法确认可领奖励，未发送请求")
    end_time = record.get("endTime")
    if closed := _closed(aid, ctx.now, None if end_time == "" else end_time):
        return closed
    premium = _premium(record)
    if premium is None:
        return TaskResult(ID, BLOCKED, "登录有礼高级档位状态格式异常，未发送请求")
    ids = claimable(record, premium)
    if ids is None:
        return TaskResult(ID, BLOCKED, "登录有礼的进度或奖励配置读取不全，未发送请求")
    end = login_gift_end(ctx.info, ctx.now)
    if end:
        return TaskResult(ID, SKIPPED, "这个存档的登录有礼奖励已领完", profile=_mark(end))
    kind = "高级+免费" if premium else "免费"
    if not ids:
        return TaskResult(ID, SKIPPED, f"已登录 {record.get('days')} 天，{kind}奖励没有可领的", profile=_mark(end))
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "已停止，未领取登录有礼", profile=_mark(end))
    gains = dungeon.Gains()
    response = ctx.request(R_CLAIM, {})
    gains.add(response, ctx.session.drain_pushes())
    refresh_note = ""
    if end is False and state.num(record.get("days")) >= max(day for _, day, _ in _reward_rows()):
        updated, refresh_note = _end_after_claim(ctx, response, aid)
        end = end if updated is None else updated
    detail = f"已登录 {record.get('days')} 天，领取{kind}奖励 ×{len(ids)}" + dungeon.gain_note(gains) + refresh_note
    return TaskResult(ID, SUBMITTED, detail, profile=_mark(end))
