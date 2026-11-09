"""藏宝洞：每次从服务器给的三张推荐里挑中间那张（高星）挑战，用完今日免费次数。

次数 = ``Common.cardChallengeMaxNum - everyDaySetting.landformChallengeCnt``（客户端同一算法；
共享宝箱的 dungeonTreasure 不是挑战次数）。读不到或表里上限不唯一 → 不挑战。
战斗本身不做：``beginBattle {entityId}`` 之后直接 ``endBattle {entityId, win:1}``，与客户端提交的字段一致。
任何一步失败都不重发；``beginBattleWithTicket`` 等点券路由没有声明，也会被守卫拒绝。
挑战之后顺带领取已发布藏宝洞满一小时的仙魄（``getPublishLandform {rid}`` → 逐洞 ``getReward {entityId}``；2026-11-08 从妖幡卷商店挪过来）。
次数用完、读不到次数也照样去领；挑战结果未知时不领。
"""

from __future__ import annotations

from collections.abc import Mapping
import time

from .. import daycut, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

ID = "treasure_cave"
R_RECOMMEND = "scene.cardHandler.getRecommend"
R_BEGIN = "scene.cardHandler.beginBattle"
R_END = "scene.cardHandler.endBattle"
R_PUBLISHED = "scene.cardHandler.getPublishLandform"
R_REWARD = "scene.cardHandler.getReward"
MAX_RUNS = 50


def daily_cap() -> int | None:
    caps = {n for r in state.table_rows("Common") if (n := state.num(r.get("cardChallengeMaxNum"))) and n > 0}
    return caps.pop() if len(caps) == 1 else None


def recommendations(response) -> list | None:
    """三项推荐（左 / 中 / 右）。只认至少三个对象组成的列表，不把角色对象当成推荐。"""
    for key in ("recommend", "recommendList", "recommend_list", "rivals", "targets", "cards", "cardList", "list", "data", "result"):
        child = response.get(key) if isinstance(response, dict) else None
        if isinstance(child, dict):
            child = list(child.values()) if len(child) >= 3 and all(isinstance(v, dict) for v in child.values()) else child
        if isinstance(child, list) and len(child) >= 3 and all(isinstance(x, dict) for x in child[:3]):
            return child
        if isinstance(child, dict) and (found := recommendations(child)):
            return found
    return None


@task(ID, "藏宝洞", reads={R_RECOMMEND, R_PUBLISHED}, writes={R_BEGIN, R_END, R_REWARD})
def run(ctx: TaskContext) -> TaskResult:
    cave = challenge(ctx)
    try:
        fairy = claim_fairy(ctx)
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"{cave.detail}；{exc}") from exc
    statuses = {cave.status, fairy.status}
    status = BLOCKED if BLOCKED in statuses else SUBMITTED if SUBMITTED in statuses else SKIPPED
    return TaskResult(ID, status, f"{cave.detail}；{fairy.detail}")


def challenge(ctx: TaskContext) -> TaskResult:
    cap, used = daily_cap(), state.used(ctx.info, "landformChallengeCnt")
    if cap is None or used is None:
        return TaskResult(ID, BLOCKED, "无法确认藏宝洞免费次数（缺少 cardChallengeMaxNum 或 landformChallengeCnt），未挑战")
    spent = ctx.session.spent
    left = cap - used - spent[("cave",)]
    if left <= 0:
        return TaskResult(ID, SKIPPED, f"藏宝洞今日免费次数已用完（上限 {cap}）")
    gains, done = dungeon.Gains(), 0
    total = min(left, MAX_RUNS)
    try:
        for _ in range(total):
            if ctx.stopped:
                break
            if why := daycut.reason(ctx.session):
                return _end(done, gains, SUBMITTED if done else SKIPPED, why)
            if done == 0:
                ctx.log(f"藏宝洞 {done}/{total}")
            try:
                options = recommendations(ctx.request(R_RECOMMEND))
                if options is None:
                    return _end(done, gains, BLOCKED, "服务器未返回三项推荐，未开始战斗")
                entity = state.find(options[1], "entityId", "entity_id", depth=5)
                if entity in (None, ""):
                    return _end(done, gains, BLOCKED, "中间推荐缺少 entityId，未开始战斗")
                spent[("cave",)] += 1  # 先占用：开始战斗后的任何失败都可能已消耗次数
                gains.add([ctx.request(R_BEGIN, {"entityId": entity}), ctx.request(R_END, {"entityId": entity, "win": 1})],
                          ctx.session.drain_pushes())
            except p.AllianceError as exc:
                return _end(done, gains, BLOCKED, f"第 {done + 1} 次未完成：{exc}")
            done += 1
            ctx.log(f"藏宝洞 {done}/{total}")
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"藏宝洞已完成 {done} 次；{exc}") from exc
    return _end(done, gains, SUBMITTED if done else SKIPPED, "已被用户停止" if ctx.stopped else "")


def _end(done: int, gains: dungeon.Gains, status: str, why: str) -> TaskResult:
    text = f"藏宝洞挑战 {done} 次"
    if gains.items:
        text += f"；获得 {gains.text()}"
    if why:
        text += f"；{why}"
    return TaskResult(ID, status, text)


def server_clock(ctx):
    """读取进角服务器时钟；info 只认 serverTime，不扫描角色历史中的 time。"""
    link = ctx.session._link
    entry = getattr(link, 'entry', {}) if link is not None else {}

    def clock(value, depth=0):
        if not isinstance(value, Mapping) or depth > 3:
            return None
        # EnterRecordCmd.onBack 明确以 param1.info.serverTime 同步服务器时间。
        info = value.get('info')
        if isinstance(info, Mapping) and 'serverTime' in info:
            return state.seconds(info['serverTime'])
        for key in ('time', 'serverTime', 'server_time'):
            if value.get(key) not in (None, ''):
                return state.seconds(value[key])
        for key in ('data', 'result', 'entry'):
            found = clock(value.get(key), depth+1)
            if found is not None:
                return found
        return None

    base = clock(entry)
    if base is None or base <= 0:
        return None
    # 不再调用会递归命中历史 time 的 Session.now；只推进已验证的服务器时钟。
    return base + max(0, int(time.monotonic() - link.at))


def _ripe(rows: list, history: Mapping, now: int) -> list[str] | str:
    """满一小时可领的藏宝洞 entityId；数据对不上返回阻止原因。"""
    ready, seen = [], {}
    for row in rows:
        if not isinstance(row, Mapping):
            return "已发布藏宝洞数据异常，未领取仙魄"
        booth, entity = state.num(row.get("id")), row.get("entityId")
        record = history.get(str(booth), history.get(booth))
        if booth is None or booth <= 0 or not isinstance(entity, str) or not entity.strip() or not isinstance(record, Mapping):
            return "无法确认已发布藏宝洞及其领取记录，未领取仙魄"
        published = state.seconds(record.get("time"))
        claimed = state.seconds(record.get("rewardTime", 0))
        if published is None or claimed is None or min(published, claimed) < 0:
            return "藏宝洞发布时间或领取时间异常，未领取仙魄"
        if booth in seen and seen[booth] != entity:
            return "同一藏宝洞展位返回不同记录，未领取仙魄"
        seen[booth] = entity
        since = max(published, claimed)
        if since > 0 and now - since >= 3600 and entity not in ready:
            ready.append(entity)
    return ready


def claim_fairy(ctx: TaskContext) -> TaskResult:
    """领取仙魄（原在妖幡卷商店）。ReleaseMediator/ReleaseView：按当前存档查询已发布洞，满一小时后逐洞领取一次。"""
    done, gains = 0, dungeon.Gains()

    def result(status: str, detail: str) -> TaskResult:
        prefix = f"已领取 {done} 个藏宝洞的仙魄{dungeon.gain_note(gains)}；" if done else ""
        return TaskResult(ID, status, prefix + detail)

    try:
        if ctx.stopped or ctx.winding:
            return result(SKIPPED, "已停止，未领取仙魄")
        ctx.refresh()
        rid = ctx.session.record_id
        if not rid:
            return result(BLOCKED, "无法确认当前存档，未领取仙魄")
        if ctx.stopped or ctx.winding:
            return result(SKIPPED, "已停止，未领取仙魄")
        rows = ctx.request(R_PUBLISHED, {"rid": rid}).get("data")
        if not isinstance(rows, list):
            return result(BLOCKED, "服务器未返回已发布藏宝洞列表，未领取仙魄")
        if not rows:
            return result(SKIPPED, "没有已发布的藏宝洞，未领取仙魄")
        history = state.section(ctx.info, "landformPublish")
        if history is None:
            return result(BLOCKED, "服务器未返回藏宝洞领取记录（landformPublish），未领取仙魄")
        # 只用进角响应的服务器时钟，加上本连接经过时间。
        now = server_clock(ctx)
        if now is None or now <= 0:
            return result(BLOCKED, "无法确认服务器时间，未领取仙魄")
        ready = _ripe(rows, history, now)
        if isinstance(ready, str):
            return result(BLOCKED, ready)
        for entity in ready:
            if ctx.stopped or ctx.winding:
                return result(SUBMITTED if done else SKIPPED, "已停止")
            received = dungeon.Gains()
            received.add(ctx.request(R_REWARD, {"entityId": entity}), [])
            if received.items["fairyMoney"] <= 0:
                return result(BLOCKED, "领取响应未确认仙魄，本次不再领取，请登录游戏核对")
            gains.items.update(received.items)
            done += 1
        return result(SUBMITTED if done else SKIPPED, "领取完成" if done else "已发布藏宝洞的仙魄尚未满一小时可领")
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"此前已领取 {done} 个藏宝洞的仙魄{dungeon.gain_note(gains)}；{exc}") from exc
    except p.AllianceError as exc:
        return result(BLOCKED, f"领取仙魄失败：{exc}；本次不再领取")
