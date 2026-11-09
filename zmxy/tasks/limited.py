"""活动页：云梯夺宝、中秋博饼、金秋送福、决战牛魔、挑战GM与挑战刑天。

* 云梯夺宝 / 中秋博饼：次数 = 表里的每日免费次数 - ``everyDaySetting`` 的已用计数（``cloudLadderCount`` / ``boCakeCnt``）。
  表值不是正整数、已用数读不到都阻止；发出请求就先记一次（本连接内不会重复用同一份次数），响应缺字段不重发。
* 云梯夺宝的阶段奖励（走到 ``CloudLadderHelp.cloudLadder`` 的 6/40/80/120 格）：走完后把达到了还没领的都领掉
  （``getCloudLadderReward {i: 第几档}``，客户端 CLRewardView 的“领取”）；今天没次数、已走到终点时也照样检查。
* 中秋博饼免费次数用完后接着用背包里的骰子（``MidAutumn.midAutumnPropId``，每次 ``midAutumnPropNum`` 个，``midBoCake {type:1}``，
  客户端 MidAutumnModule.update 同一顺序：免费 → 骰子）；**不发 type 2（点券）**；全天合计不超过 ``dayMaxNum``（已博次数 ``boCakeCnt``）。骰子是日常任务 / 限时任务的奖励，
  所以这个任务排在它们后面（``last``），用之前重新读一次背包。
* 决战牛魔（组类型 9）、挑战魔龙（组类型 7）、挑战GM（组类型 12）、挑战刑天（组类型 10）：两个单人关卡（普通 / 噩梦）共用每日免费次数，走普通的"创建→拾取→结算"；
  只开放期内才打（``Dungeon.activityId`` → Activity 档期）。难度选择同快速养成：行上点按，默认噩梦。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import data, dungeon, protocol as p, quota, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

LIMITED = "活动"
R_CLOUD = "scene.activityHandler.cloudLadder"
R_CLOUD_REWARD = "scene.activityHandler.getCloudLadderReward"
R_BOBING = "scene.activityHandler.midBoCake"
R_RED = "scene.activityHandler.getRedPacketReward"
MAX_FREE = 10  # 表里的每日免费次数超过这个值就当作读错了


def free_attempts(ctx: TaskContext, task_id: str, label: str, limit: int | None, counter: str) -> tuple[int, TaskResult | None]:
    """今日还能免费做几次；不能确定或没有次数时一并给出要返回的结果。"""
    if limit is None or not 1 <= limit <= MAX_FREE:
        return 0, TaskResult(task_id, BLOCKED, f"数据表没有给出{label}的每日免费次数，未发送请求")
    used = state.used(ctx.info, counter)
    if used is None:
        return 0, TaskResult(task_id, BLOCKED, f"服务器未返回 everyDaySetting.{counter}，无法确认{label}今日已用次数，未发送请求")
    left = limit - used - ctx.session.spent[(task_id,)]
    if left <= 0:
        return 0, TaskResult(task_id, SKIPPED, f"{label}今日免费次数已用完（{min(used, limit)}/{limit}）")
    return left, None


def attempt(ctx: TaskContext, task_id: str, route: str, body: dict) -> dict:
    ctx.session.spent[(task_id,)] += 1  # 先占用：请求发出后无论结果如何都可能已用掉次数，不退回
    return ctx.request(route, body)


# ---------------------------------------------------------------- 云梯夺宝

CLOUD_ID = "cloud_ladder"
CLOUD_END = "cloud_end"  # 存档 profile 里的标记：这个存档的云梯已走到终点（活动页据此把云梯夺宝放进“已结束”）


def _cloud_help() -> Mapping[str, Any]:
    return state.row("CloudLadderHelp", 1) or {}


def _cloud_open(now: int) -> bool:
    return state.activity_open(_cloud_help().get("activityId"), now)


def cloud_end(value: Any) -> bool | None:
    """这个存档这一段的云梯是否已走到终点（``cloudLadder.pos`` ≥ CloudLadderHelp.maxCount，客户端 CLCloud.isEnd 同一判断）。
    value 是登录信息或云梯夺宝的响应；没有 ``cloudLadder`` 对象 / 表里没有终点 → None（不确定）。pos 缺省按 0（客户端同样默认 0）。"""
    section, last = state.section(value, "cloudLadder"), state.num(_cloud_help().get("maxCount"))
    if section is None or last is None or last <= 0:
        return None
    return (state.num(section.get("pos")) or 0) >= last


MAX_CLOUD_THROWS = 20  # 一次任务最多扔几次（每日 1 次 + 连续走到“再来一次”格），防止服务器状态异常时一直扔


_CELL_EVENT = {1: "再来一次", 2: "随机前进"}


def _moves(operate: Any) -> str:
    """一次扔骰子的走法：59→65（6 点），中途踩到特殊格的写上格子效果。读不懂的步骤原样略过。"""
    parts = []
    for step in operate if isinstance(operate, list) else ():
        if not isinstance(step, Mapping):
            continue
        start, end, point = (state.num(step.get(k)) for k in ("start", "endPos", "point"))
        text = f"{start}→{end}" + (f"（{point} 点）" if point is not None else "")
        if (name := _CELL_EVENT.get(state.num(step.get("event")))):
            text += f"停在{name}"
        parts.append(text)
    return "，".join(parts) or "回执没有走法"


def free_again(cloud: Any) -> bool:
    """上一次扔骰子停在“再来一次”格（CloudLadder.event=1）：服务器记着 ``cloudLadder.event == 1``，
    可以免费再扔一次（客户端 CloudLadderModule.setInitData / 动画结束时同一判断，界面显示“免费再来一次”）。"""
    return isinstance(cloud, Mapping) and state.num(cloud.get("event")) == 1


def cloud_claims(cloud: Any) -> list[tuple[int, int]]:
    """现在能领的阶段奖励 [(第几档, 要求的格数)]：走到了（``pos`` ≥ 该档格数）而且 ``reward`` 里还没有这一档
    （客户端 CloudLadderProxy.isCanGetReward 同一判断）。云梯对象或表读不懂 → 空（不领）。"""
    row = _cloud_help()
    steps = [state.num(x) for x in str(row.get("cloudLadder") or "").split(";")]
    got = cloud.get("reward", {}) if isinstance(cloud, Mapping) else None
    if not isinstance(got, Mapping) or any(s is None or s <= 0 for s in steps) or len(steps) != len(str(row.get("rewardId") or "").split(";")):
        return []
    pos, taken = state.num(cloud.get("pos")) or 0, {str(k) for k in got}
    return [(i, s) for i, s in enumerate(steps) if pos >= s and str(i) not in taken]


def _claim_stages(ctx: TaskContext, cloud: Any, gains: dungeon.Gains) -> tuple[list[int], str]:
    """把能领的阶段奖励都领掉。返回 (领到的各档格数, 停止原因)。服务器拒绝、或回执里看不到这一档已领 → 停，不重发。"""
    taken: list[int] = []
    for index, step in cloud_claims(cloud):
        if ctx.stopped:
            break
        try:
            response = ctx.request(R_CLOUD_REWARD, {"i": index})
        except p.AllianceError as exc:
            return taken, f"领取 {step} 格阶段奖励被拒绝：{exc}"
        gains.add(response, ctx.session.drain_pushes())
        now = state.section(response, "cloudLadder")
        if now is None or any(i == index for i, _ in cloud_claims(now)):
            return taken, f"领取 {step} 格阶段奖励后服务器没有确认已领，停止且不重发"
        taken.append(step)
    return taken, ""


@task(CLOUD_ID, "云梯夺宝", writes={R_CLOUD, R_CLOUD_REWARD}, group=LIMITED, window=_cloud_open)
def run_cloud(ctx: TaskContext) -> TaskResult:
    end = cloud_end(ctx.info)  # 每次都把最新判断写回存档 profile（新的一段开始后 pos 归零，标记也跟着清掉）
    mark = lambda: {CLOUD_END: end} if end is not None else None  # noqa: E731
    cloud, gains, done, quiet = state.section(ctx.info, "cloudLadder"), dungeon.Gains(), 0, SKIPPED
    if end:
        head = "云梯已走到终点，这一段已结束"
    else:
        limit = state.num(_cloud_help().get("dayFreeCount"))
        left, stopped = free_attempts(ctx, CLOUD_ID, "云梯夺宝", limit, "cloudLadderCount")
        again = free_again(cloud) and (stopped is None or stopped.status == SKIPPED)  # 每日次数读不清时什么都不扔
        bonus, odd, moves = 0, [], []
        for _ in range(MAX_CLOUD_THROWS):
            if ctx.stopped or not (left > 0 or again):
                break
            # 客户端两种都发同一个免费的 cloudLadder（不走点券的 cloudLadderWithTicket），用掉哪一种由服务器定。
            # 2026-11-09 账号1存档2：程序扔过一次（到 59 格），当天 everyDaySetting 里仍没有 cloudLadderCount，原因未查明。
            # 所以没记着“再来一次”时，扔一次就按用掉每日次数算，不因服务器没记上而接着扔。
            had_again = again
            if left > 0:
                ctx.session.spent[(CLOUD_ID,)] += 1  # 先占用：同一连接里不再当作没用过
            response = ctx.request(R_CLOUD, {})
            gains.add(response, ctx.session.drain_pushes())
            if not isinstance(response.get("operate"), list):
                return TaskResult(CLOUD_ID, BLOCKED, "云梯夺宝请求已响应，但没有返回 operate 行动列表；结果不确定，未重试", profile=mark())
            done += 1
            used = state.used(response, "cloudLadderCount") if state.daily(response) is not None else None
            if had_again and left > 0:  # 两种都有：看服务器扣的是不是每日次数（回执没带就只读刷新一次）
                if used is None:
                    try:
                        used = state.used(ctx.refresh(), "cloudLadderCount")
                    except p.AllianceError:
                        used = None
                spent_daily = used is None or used > limit - left  # 读不到：按用掉每日次数算，宁可少扔
            else:
                spent_daily = left > 0
                if spent_daily and used is not None and used <= limit - left:
                    odd.append(f"扔完服务器的每日已用次数还是 {used}（没记上），不再接着扔")
            moves.append(f"{_moves(response.get('operate'))}（{'每日次数' if spent_daily else '再来一次'}"
                         + ("" if used is None else f"，服务器记今日已用 {used}") + "）")
            if spent_daily:
                left -= 1
            else:
                bonus += 1
                ctx.session.spent[(CLOUD_ID,)] -= 1  # 服务器用的是“再来一次”：每日次数没占用
            cloud = state.section(response, "cloudLadder") or cloud
            again = free_again(state.section(response, "cloudLadder"))  # 回执没带云梯对象：不猜，不再扔
            latest = cloud_end(response)
            end = end if latest is None else latest
            if end:
                break
        extra = (f"（含“再来一次”{bonus} 次）" if bonus else "") + (f"：{'；'.join(moves)}" if moves else "") + "".join(f"；{o}" for o in odd)
        if stopped and not done:
            head = stopped.detail
        else:
            head = f"每日免费挑战 ×{done - bonus}{extra}" + ("；已走到终点，这一段结束" if end else "")
        quiet = stopped.status if stopped else SKIPPED
    taken, why = _claim_stages(ctx, cloud, gains)  # 今天没次数 / 已到终点也检查：达到了还没领的阶段奖励都领掉
    note = head + (f"；领取阶段奖励 {'、'.join(map(str, taken))} 格" if taken else "") + dungeon.gain_note(gains)
    if why:
        return TaskResult(CLOUD_ID, BLOCKED, f"{note}；{why}", profile=mark())
    return TaskResult(CLOUD_ID, SUBMITTED if done or taken else quiet, note, profile=mark())


# ---------------------------------------------------------------- 中秋博饼

BOBING_ID = "mid_autumn_bobing"
BOBING_CONFIG = 1  # MidAutumn 表里中秋博饼用的那一行（客户端 MidAutumnProxy.help / MidConfigConst 都读第 1 行；第 2 行是拼图、第 3 行是 midAutumnMc，骰子不是同一种）


def _bobing_config() -> Mapping[str, Any]:
    return state.row("MidAutumn", BOBING_CONFIG) or {}


def _bobing_open(now: int) -> bool:
    return state.activity_open(_bobing_config().get("activityId"), now)


MAX_DICE_ROLLS = 200  # 一次最多用骰子博这么多次（防止背包数量读错时停不下来）


def _dice() -> tuple[int, int, str] | None:
    """博饼用的骰子：(物品 id, 每次要几个, 名字)；表对不上 → None。"""
    row = _bobing_config()
    item, need = state.num(row.get("midAutumnPropId")), state.num(row.get("midAutumnPropNum"))
    if not item or item <= 0 or not need or need <= 0:
        return None
    return item, need, dungeon.item_name(item) or "骰子"


def _dice_rolls(ctx: TaskContext) -> tuple[int, str, str]:
    """背包里的骰子现在够博几次：(次数, 骰子名, 不能用时的说明)。日常 / 限时任务刚发的骰子不在登录快照里，所以重新读一次。"""
    dice = _dice()
    if dice is None:
        return 0, "骰子", "数据表没有给出博饼用的骰子，未使用骰子"
    item, need, name = dice
    try:
        info = ctx.refresh()
    except p.AllianceError as exc:
        return 0, name, f"读不到背包（{exc}），未使用{name}"
    owned = state.bag_count(info, item)
    if owned is None:
        return 0, name, f"读不到背包里的{name}数量，未使用{name}"
    rolls = min(owned // need, MAX_DICE_ROLLS)
    cap, done = state.num(_bobing_config().get("dayMaxNum")), state.used(info, "boCakeCnt")
    if cap is not None and cap > 0 and done is not None:  # 今日可博饼次数（免费 + 骰子合计）
        if cap - done <= 0 and rolls:
            return 0, name, f"今日博饼次数已达上限（{cap} 次），未使用{name}"
        rolls = min(rolls, cap - done)
    return rolls, name, ""


@task(BOBING_ID, "中秋博饼", writes={R_BOBING}, group=LIMITED, window=_bobing_open, last=True)  # last：排在日常任务领奖、限时任务之后（骰子是它们的奖励）
def run_bobing(ctx: TaskContext) -> TaskResult:
    left, stopped = free_attempts(ctx, BOBING_ID, "中秋博饼", state.num(_bobing_config().get("midAutumnDayNum")), "boCakeCnt")
    if stopped and stopped.status != SKIPPED:
        return stopped  # 免费次数说不清：什么都不发
    gains, results, why = dungeon.Gains(), [], ""

    def roll(kind: int) -> bool:
        """博一次（0 = 免费，1 = 用骰子）；响应不完整 → False。"""
        response = attempt(ctx, BOBING_ID, R_BOBING, {"type": kind}) if kind == 0 else ctx.request(R_BOBING, {"type": kind})
        gains.add(response, ctx.session.drain_pushes())
        result, points = state.num(response.get("bcId")), response.get("points")
        if result is None or not isinstance(points, (list, tuple)):
            return False
        describe = (state.row("MidAutumnPoints", result) or {}).get("describe")
        results.append(str(describe) if describe else f"结果 {result}")
        return True

    free = used = 0
    for _ in range(left):
        if ctx.stopped:
            break
        if not roll(0):
            return TaskResult(BOBING_ID, BLOCKED, "中秋博饼请求已响应，但缺少 bcId/points 结果；状态不确定，未重试")
        free += 1
    rolls, name, note = (0, "骰子", "") if ctx.stopped else _dice_rolls(ctx)  # 免费的用完了：接着用背包里的骰子，不碰点券
    for _ in range(rolls):
        if ctx.stopped:
            break
        try:
            if not roll(1):
                why = f"用{name}博饼的请求已响应，但缺少 bcId/points 结果；状态不确定，未重试"
                break
        except p.AllianceError as exc:
            why = f"用{name}博饼被拒绝：{exc}"
            break
        used += 1
    parts = ([f"每日免费博饼 ×{free}"] if free else []) + ([f"用{name}博饼 ×{used}"] if used else [])
    text = "、".join(parts) + (f"（{'、'.join(results)}）" if results else "") + dungeon.gain_note(gains)
    if why:
        return TaskResult(BOBING_ID, BLOCKED, f"{text}；{why}" if text else why)
    if not free and not used:
        return TaskResult(BOBING_ID, SKIPPED, f"{stopped.detail if stopped else '没有博饼'}；{note or f'背包里的{name}不够博一次'}")
    return TaskResult(BOBING_ID, SUBMITTED, text + (f"；{note}" if note else ""))


# ---------------------------------------------------------------- 金秋送福

RED_ID = "golden_autumn"
RED_ACTIVITY = 2051  # RedPacket 表唯一一行，同时是 Activity 档期 id；金秋蜜意宝箱
RED_STOPPED = "用户停止，未继续领取"


def _red_packet_counts(value: Any) -> tuple[int | None, int | None]:
    """当前金秋红包的（累计消耗点券, 已开箱数）；缺字段不当成 0。"""
    if not isinstance(value, Mapping):
        return None, None
    activity = state.num(value.get("id"))
    tickets, opened = state.num(value.get("consumeTicket")), state.num(value.get("consumeCnt"))
    if activity != RED_ACTIVITY or tickets is None or opened is None or tickets < 0 or opened < 0:
        return None, None
    return tickets, opened


def _red_recharge_claims(info: Mapping[str, Any]) -> tuple[list[str] | None, str | None]:
    """待领取的充值记录 id；客户端 getRecId 同样按 activityId 找记录，并把 status != 2 当成待领取。"""
    records = info.get("rechargeActivity", {})
    if records is None:
        records = {}
    if not isinstance(records, Mapping):
        return None, "服务器返回的 rechargeActivity 结构无法识别"
    pending: list[str] = []
    for record in records.values():
        if not isinstance(record, Mapping) or state.num(record.get("activityId")) != RED_ACTIVITY:
            continue
        status, record_id = state.num(record.get("status")), record.get("id")
        if status is None or record_id in (None, ""):
            return None, "金秋送福充值记录缺少 id/status，未领取丰稔箱"
        if status != 2:
            pending.append(str(record_id))
    return pending, None


class _RedBoxes:
    """金秋送福的一次执行：每日免费箱 → 充值箱 → 点券箱，前一笔结果未知就不再发同路由的请求。"""

    def __init__(self, ctx: TaskContext, info: Mapping[str, Any]) -> None:
        self.ctx, self.info, self.row = ctx, info, state.row("RedPacket", RED_ACTIVITY) or {}
        self.gains = dungeon.Gains()
        self.free = self.recharge = self.ticket = 0
        self.issues: list[str] = []

    def open_box(self, kind: int, box_id: str, label: str) -> Any:
        """领一个箱子，返回响应；被拒绝或结果未知时记下问题，返回 None。"""
        try:
            response = attempt(self.ctx, RED_ID, R_RED, {"type": kind, "id": box_id})
        except UncertainOutcome:
            raise
        except Exception as exc:
            self.issues.append(f"{label}领取结果未知或被拒绝（{exc}），未重试")
            return None
        self.gains.add(response, self.ctx.session.drain_pushes())
        return response

    def free_box(self) -> bool:
        """每日免费箱。返回 False = 这一笔结果未知，后面的都不领。"""
        limit = state.num(self.row.get("freeGiftDayNum"))
        if limit is None or not 1 <= limit <= MAX_FREE:
            self.issues.append("RedPacket 表未提供有效的每日免费次数")
            return True
        used = state.used(self.info, "redPacketFreeGiftDayNum")
        if used is None:
            self.issues.append("服务器未返回 everyDaySetting.redPacketFreeGiftDayNum，未开每日免费箱")
            return True
        if used >= limit or self.ctx.stopped:
            return True
        if self.open_box(0, "", "每日免费箱") is None:
            return False
        self.free = 1
        return True

    def recharge_boxes(self) -> tuple[bool, str]:
        """充值箱。返回 (能不能接着领点券箱, 点券箱请求体里的 id)。"""
        pending, error = _red_recharge_claims(self.info)
        if error:
            self.issues.append(error)
            return False, ""
        batch_id = pending[-1] if pending else ""
        gift_day = state.num(self.row.get("rechargeGiftDay"))
        if gift_day is None:
            self.issues.append("RedPacket 表缺少 rechargeGiftDay，无法确认充值箱是否开放")
        if gift_day is None or gift_day <= 0:
            return True, batch_id
        for record_id in reversed(pending):  # 客户端 getRecId 返回最后一个待领奖记录
            if self.ctx.stopped:
                self.issues.append(RED_STOPPED)
                break
            response = self.open_box(1, record_id, f"金秋丰稔充值箱 {record_id} ")
            if response is None:
                return False, batch_id
            updated = response.get("rechargeActivity") if isinstance(response, Mapping) else None
            if not isinstance(updated, Mapping) or str(updated.get("id", "")) != record_id:
                self.issues.append(f"金秋丰稔充值箱 {record_id} 响应未确认对应记录，停止后续领取")
                return False, batch_id
            self.recharge += 1
        # 客户端所有充值箱领完后 getRecId 为空，点券箱也沿用该请求体。
        return True, "" if self.recharge == len(pending) else batch_id

    def ticket_boxes(self, tickets: int, opened: int, cost: int, batch_id: str) -> None:
        while tickets // cost - opened > 0 and not self.ctx.stopped:
            before = opened
            response = self.open_box(2, batch_id, "金秋丰稔点券箱")
            if response is None:
                break
            updated = response.get("redPacket") if isinstance(response, Mapping) else None
            tickets, opened = _red_packet_counts(updated)
            if tickets is None or opened is None or opened <= before:
                self.issues.append("金秋丰稔点券箱响应未确认 consumeCnt 增加，停止后续领取且不重试")
                break
            self.ticket += 1
        known = tickets is not None and opened is not None  # 读不到时上面已经记了“未确认”，不再算还剩几箱
        if self.ctx.stopped and known and tickets // cost - opened > 0:
            self.issues.append(RED_STOPPED)

    def paid_boxes(self) -> None:
        """金秋丰稔箱优先按客户端顺序领取充值记录，再领取累计消耗点券解锁的箱子。"""
        tickets, opened = _red_packet_counts(self.info.get("redPacket"))
        if tickets is None or opened is None:
            self.issues.append("服务器未返回有效的 redPacket 状态，无法确认丰稔箱次数")
            return
        go_on, batch_id = self.recharge_boxes()
        cost = state.num(self.row.get("consumeTicketGift"))
        if cost is None or cost <= 0:
            self.issues.append("RedPacket 表未提供有效的点券累计门槛，未领取点券丰稔箱")
        elif go_on:
            self.ticket_boxes(tickets, opened, cost, batch_id)

    def result(self) -> TaskResult:
        parts = [f"{label} ×{n}" for label, n in (("每日免费金秋蜜意箱", self.free), ("金秋丰稔充值箱", self.recharge),
                                                  ("金秋丰稔点券箱", self.ticket)) if n]
        detail = "、".join(parts) if parts else "没有可领取的金秋宝箱"
        if self.issues:
            detail += "；" + "；".join(dict.fromkeys(self.issues))
        detail += dungeon.gain_note(self.gains)
        status = BLOCKED if self.issues else SUBMITTED if parts else SKIPPED
        return TaskResult(RED_ID, status, detail)


@task(RED_ID, "金秋送福", writes={R_RED}, group=LIMITED, window=lambda now: state.activity_open(RED_ACTIVITY, now))
def run_red(ctx: TaskContext) -> TaskResult:
    """领每日免费蜜意箱及客户端确认已解锁的丰稔充值 / 点券箱；结果未知时不重试。"""
    if ctx.now is None:
        return TaskResult(RED_ID, BLOCKED, "缺少服务器时间，无法确认活动是否开放，未发送请求")
    if not state.activity_open(RED_ACTIVITY, ctx.now):
        return TaskResult(RED_ID, SKIPPED, "活动未开放")
    # 同一账号的其他任务可能先于本任务更新过状态；真实连接重新进角读取服务器快照。
    boxes = _RedBoxes(ctx, ctx.refresh() if ctx.session.live else ctx.info)
    go_on = boxes.free_box()
    if ctx.stopped:
        boxes.issues.append(RED_STOPPED)
    elif go_on:
        boxes.paid_boxes()
    return boxes.result()


# ---------------------------------------------------------------- 决战牛魔 / 挑战魔龙

# 两个活动的结构相同（同快速养成）：DungeonHelper 一个组类型里正好两个单人关卡（普通 / 噩梦），共用每日免费次数；
# 难度在行上点按切换（默认噩梦），用掉今日剩余的免费次数。
LEVELS = ("普通", "噩梦")
DEFAULT_LEVEL = "噩梦"
BULL_ID, DRAGON_ID, GM_ID, XING_TIAN_ID = "bull_demon", "magic_dragon", "gm_spar", "xing_tian"
CHALLENGE_GM_ID = "challenge_gm"


def pair_mids(helper_type: int) -> list[int]:
    return sorted(int(k) for k, r in data.table("DungeonHelper").items() if r.get("type") == helper_type)


def pair_level(helper_type: int, difficulty: str) -> dungeon.Level | str:
    """该活动所选难度的关卡；配置对不上返回原因。"""
    mids = pair_mids(helper_type)
    if len(mids) != 2 or difficulty not in LEVELS:
        return "活动的关卡配置无效"
    mid = mids[LEVELS.index(difficulty)]
    group, lv = quota._helper_group(mid), dungeon.plain_level(mid, dungeon.SPECIAL)
    if group is None or set(group[1]) != set(mids) or lv is None:
        return "共享免费次数规则无法确认"
    return lv


def run_pair(ctx: TaskContext, task_id: str, helper_type: int) -> TaskResult:
    difficulty = ctx.options.get("mode") or DEFAULT_LEVEL
    lv = pair_level(helper_type, difficulty)
    if isinstance(lv, str):
        return TaskResult(task_id, BLOCKED, f"{lv}，未创建副本")
    if task_id == CHALLENGE_GM_ID:
        try:
            ctx.refresh()  # GM免费次数耗尽后会直接扣点券，进场前刷新共享计数。
        except p.AllianceError as exc:
            return TaskResult(task_id, BLOCKED, f"刷新挑战GM免费次数失败：{exc}，未创建副本")
    if ctx.now is None:
        return TaskResult(task_id, BLOCKED, "缺少服务器时间，无法确认活动是否开放，未创建副本")
    if not state.activity_open((data.row("Dungeon", lv.mid) or {}).get("activityId"), ctx.now):
        return TaskResult(task_id, SKIPPED, "活动未开放")
    q = quota.of(ctx.info, lv.mid)
    if q.left is None:
        return TaskResult(task_id, BLOCKED, f"无法确认 {lv.name} 的免费次数（{q.why}），未创建副本")
    left = int(q.left) - ctx.session.spent[q.key]
    if left <= 0:
        return TaskResult(task_id, SKIPPED, "今日免费次数已用完")
    return dungeon.run_levels(ctx, task_id, [lv], lambda _: left)


def pair_open(helper_type: int, now: int) -> bool:
    """两个难度里有一个关卡所属的活动还在档期内。"""
    return any(state.activity_open((data.row("Dungeon", m) or {}).get("activityId"), now) for m in pair_mids(helper_type))


def register_pair(task_id: str, title: str, helper_type: int) -> None:
    @task(task_id, title, writes=dungeon.ROUTES[dungeon.SPECIAL], group=LIMITED,
          modes=tuple((d, d) for d in LEVELS), mode_default=DEFAULT_LEVEL, window=lambda now: pair_open(helper_type, now))
    def run(ctx: TaskContext) -> TaskResult:
        return run_pair(ctx, task_id, helper_type)


register_pair(BULL_ID, "决战牛魔", 9)
register_pair(DRAGON_ID, "挑战魔龙", 7)
register_pair(GM_ID, "GM切磋", 20)
register_pair(CHALLENGE_GM_ID, "挑战GM", 12)
register_pair(XING_TIAN_ID, "挑战刑天", 10)
