"""抽取狗卡（活动页）：按双十一活动表开放，只用免费次数，每抽一次刷新核对，对不上就停不重发。"""

from collections import namedtuple
from collections.abc import Mapping
from datetime import datetime

from ... import data, protocol as p, state
from ...core import BLOCKED, SKIPPED, SUBMITTED, TaskResult, UncertainOutcome, task

ID = "double11_dog"
R_DATA = "scene.activityHandler.getDouble11Data"
R_DRAW = "scene.activityHandler.double11Lottery"
MAX_DRAWS = 100  # 单次执行上限；不跟随中途新增的次数无限抽取


Config = namedtuple("Config", "start reward base keys")
Snapshot = namedtuple("Snapshot", "config used extra cards day")


def config():
    row = state.row("Double11Common", 1)
    if not row:
        raise ValueError("缺少双十一通用配置")
    start, reward, base = (state.num(row.get(k)) for k in ("startActivityId", "rewardGetActivityId", "dailyDrawNum"))
    keys = [r.get("consume_subType") for r in state.table_rows("Double11Dog")]
    if any(not isinstance(k, str) or not k.startswith("dog_") for k in keys):
        raise ValueError("狗卡类型配置异常")
    if (not start or start < 0 or not reward or reward < 0
            or not state.row("Activity", start) or not state.row("Activity", reward)
            or base is None or not 0 <= base <= MAX_DRAWS
            or row.get("type") != "coin" or not keys or len(set(keys)) != len(keys)
            or set(str(row.get("subType")).split(";")) != set(keys)):
        raise ValueError("双十一抽卡配置缺失或格式变化")
    return Config(start, reward, base, tuple(sorted(keys)))


def opened(now):
    try:
        cfg = config()
        return state.activity_open(cfg.start, now) and not state.activity_open(cfg.reward, now)
    except (ValueError, data.DataError):
        return False


def snapshot(ctx):
    ctx.refresh()
    cfg = config()
    if ctx.now is None or not opened(ctx.now):
        raise ValueError("活动未开放、已进入领奖期或缺少服务器时间")
    used, extra = state.used(ctx.info, "double11LotteryCnt"), state.used(ctx.info, "addDouble11LotteryCnt")
    if used is None or extra is None or used > cfg.base + extra:
        raise ValueError("双十一每日抽卡次数缺失或异常")
    cards = ctx.request(R_DATA, {}).get("data")
    if not isinstance(cards, Mapping):
        raise ValueError("服务器没有返回狗卡 data 状态")
    counts = {k: state.num(cards.get(k, 0)) for k in cfg.keys}
    if any(n is None or n < 0 for n in counts.values()):
        raise ValueError("服务器狗卡数量异常")
    return Snapshot(cfg, used, extra, counts, datetime.fromtimestamp(ctx.now, state._CN).date())


def drawn_card(reply, keys):
    """客户端 D11DLoopView 直接用 give[0].subType 显示抽得的狗卡。"""
    give = reply.get("give")
    if (not isinstance(give, list) or len(give) != 1 or not isinstance(give[0], Mapping)
            or give[0].get("type") != "coin" or give[0].get("subType") not in keys
            or state.num(give[0].get("count")) != 1):
        raise ValueError("抽卡奖励格式变化")
    return give[0]["subType"]


@task(ID, "抽取狗卡", reads={R_DATA}, writes={R_DRAW}, spends={R_DRAW}, group="活动", window=opened)
def run(ctx):
    done, got = 0, {}
    try:
        if ctx.stopped or ctx.winding:
            return TaskResult(ID, SKIPPED, "用户停止，未抽卡")
        if not opened(ctx.now):  # 未开放期间不发任何活动请求
            return TaskResult(ID, SKIPPED, "抽取狗卡尚未开放或已结束（按当前活动表）")
        before = snapshot(ctx)
        budget = min(MAX_DRAWS, before.config.base + before.extra - before.used)
        if budget == 0:
            return TaskResult(ID, SKIPPED, "今日免费狗卡抽取次数已用完")
        labels = {r["consume_subType"]: str(r.get("name") or r["consume_subType"]) for r in state.table_rows("Double11Dog")}
        for _ in range(budget):
            if (ctx.stopped or ctx.winding or not opened(ctx.now)
                    or datetime.fromtimestamp(ctx.now, state._CN).date() != before.day):
                break
            reply = ctx.request(R_DRAW, {})
            try:
                key = drawn_card(reply, before.config.keys)
                after = snapshot(ctx)
                expected = before._replace(used=before.used + 1, cards={**before.cards, key: before.cards[key] + 1})
                if after != expected:
                    raise ValueError("抽卡后次数、狗卡数量、活动配置或日期未能核对一致")
            except (ValueError, data.DataError, p.AllianceError) as exc:
                raise UncertainOutcome(f"抽取狗卡结果无法确认：{exc}；停止本账号后续任务，不重发") from exc
            done += 1
            got[labels[key]] = got.get(labels[key], 0) + 1
            before = after
            ctx.log(f"抽取狗卡：已确认{done}次，获得{labels[key]}")
        detail = "、".join(f"{name}×{n}" for name, n in got.items())
        status = SUBMITTED if done == budget else BLOCKED if done else SKIPPED
        rest = f"；已停止，还剩{budget - done}次没抽" if done < budget else ""
        return TaskResult(ID, status, f"已确认免费抽狗卡{done}次" + (f"：{detail}" if detail else "") + rest)
    except (ValueError, data.DataError, p.AllianceError) as exc:
        return TaskResult(ID, BLOCKED, f"抽取狗卡停止：{exc}；已确认{done}次")
