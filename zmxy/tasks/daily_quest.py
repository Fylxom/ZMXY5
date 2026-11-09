"""日常任务：接取精英关卡的日常任务（每天最多 5 个），打完领奖，并顺带补足今日万能强化石（4 个）。

规则：日常任务 = ``TaskType`` 里 label 2 的任务，``dungeonId`` 就是对应的精英关卡；状态 0 未接 / 1 进行中 / 2 可领 / 3 已领。
今日可接数 = min(5, VIP 的 ``dailyTaskAccNum``) - ``everyDaySetting.dailyQuestCnt``。

执行分两段（排序规则见 ``core.TaskSpec.prelude``）：
1. 前置步骤 ``accept``：在所有任务（含福禄寿）之前，接取“选关”里勾选的关卡的日常任务；
2. 本体 ``run``（位置不变）：刷新任务状态，只把勾选关卡中日常任务仍在进行中的关各打 1 次（从最新解锁的关往前；可领 / 已领不重打） → 重新进入角色后当场领取已完成的日常任务奖励
   （不留到所有任务做完以后，这一行直接出最终结果） → 今日万能强化石不到 4 个就把勾选的关照原顺序再打一遍，还不够再从第一关起往后补，每关合计至多 2 次。
今日已得数量以服务器 ``everyDaySetting.eliteSweep`` 为准，每次挑战后用响应里的最新值，响应没给时才用掉落里的万能强化石数量。
次数、免费翻牌的校验与精英关卡任务相同（``dungeon.run_level``）。对话框开关“只捡强化石”：地面掉落只拾取强化道具。
"""

from __future__ import annotations

from .. import dungeon, quest, state
from .. import protocol as p
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, TaskContext, TaskResult, UncertainOutcome, is_bag_full, task
from .events import festival
from .rewards import dungeon_gate_open, dungeon_history, passed

ID = "daily_quest"
MAX_QUESTS = 5       # 每天最多接取几个
DAILY_LIMIT = 4      # 每天万能强化石目标
PER_LEVEL = 2        # 补强化石时每关最多打几次
STONE_ID = "101005001"  # 万能强化石
_LABEL = 2           # TaskType.label：日常任务
ACCEPTED, ACCEPT_FAILED = f"{ID}:accepted", f"{ID}:accept_failed"  # 前置步骤留给本体的 session.carry 键


def quest_of() -> dict[int, int]:
    """精英关卡 id → 对应的日常任务 id。"""
    out = {}
    for r in state.table_rows("TaskType"):
        mid, qid = state.num(r.get("dungeonId")), state.num(r.get("id"))
        if state.num(r.get("label")) == _LABEL and mid is not None and mid > 0 and qid is not None:
            out[mid] = qid
    return out


def unlocked(info) -> list[int] | None:
    """已解锁的精英关卡（按编号升序）：prevId 里每个前置关卡的通关记录 passNum > 0。读不到通关记录 → None。"""
    history = dungeon_history(info)
    if history is None:
        return None
    out = []
    for mid, _ in dungeon.catalog(dungeon.ELITE):
        prev = [state.num(x) for x in str((state.row("Dungeon", mid) or {}).get("prevId", "")).split(";")]
        if any(n is None or n <= 0 for n in prev):
            return None
        if all(passed(history, n) for n in prev):
            out.append(mid)
    return out


def can_accept(info, qid: int) -> bool:
    """客户端只展示已满足 TaskType.perDungeonId 通关条件的日常任务。"""
    return dungeon_gate_open(info, state.row("TaskType", qid))


def stones_today(info) -> int | None:
    daily = state.daily(info)
    if daily is None or "eliteSweep" not in daily:
        return None
    count = state.num(daily.get("eliteSweep"))
    return count if count is not None and count >= 0 else None


def chosen(ctx: TaskContext) -> list[int]:
    """勾选的精英关卡（保持勾选顺序，去重，最多 5 个）。"""
    known = {mid for mid, _ in dungeon.catalog(dungeon.ELITE)}
    out: list[int] = []
    for raw in ctx.options.get("levels") or []:
        mid = state.num(raw)
        if mid in known and mid not in out:
            out.append(mid)
    return out[:MAX_QUESTS]


def accept_left(info) -> int | None:
    """今日还能接几个；读不到已接数量 → None。"""
    used = state.used(info, "dailyQuestCnt")
    if used is None:
        return None
    vip = state.vip_level(info)
    row = state.row("VIP", vip) if vip is not None else None
    cap = state.num((row or {}).get("dailyTaskAccNum"))
    if cap is None or cap < 0:
        return None
    return max(0, min(MAX_QUESTS, cap) - used)


def accept(ctx: TaskContext) -> TaskResult | None:
    """前置步骤：接取勾选关卡的日常任务。返回 TaskResult = 中止本任务；None = 继续（说明留在 session.carry）。"""
    carry = ctx.session.carry
    for key in (ID, ACCEPTED, ACCEPT_FAILED):
        carry.pop(key, None)
    quests = {mid: qid for mid in chosen(ctx) if (qid := quest_of().get(mid))}
    if not quests:
        return None
    status = quest.status(ctx.info)
    if status is None:
        return TaskResult(ID, BLOCKED, "服务器未返回任务列表，无法确认哪些日常任务已接取，未接取")
    fresh_all = [qid for qid in quests.values() if status.get(qid, quest.FRESH) == quest.FRESH]
    fresh = [qid for qid in fresh_all if can_accept(ctx.info, qid)]
    left = accept_left(ctx.info) if fresh else 0
    if left is None:
        return TaskResult(ID, BLOCKED, "服务器未返回 VIP 等级、日常接取上限或 everyDaySetting，无法确认今日可接数量，未接取")
    got, failed = [], []
    try:
        for qid in fresh[:left]:
            if ctx.stopped:
                break
            try:
                quest.accept(ctx.request, qid)
            except p.AllianceError as exc:
                failed.append(f"{qid}({exc})")
                continue
            got.append(qid)
            ctx.log(f"已接取日常任务 {len(got)}/{min(len(fresh), left)}")
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"接取日常任务：已接 {len(got)} 个；{exc}") from exc
    note = f"已接取日常任务 {len(got)} 个"
    if len(quests) - len(fresh_all):
        note += f"，{len(quests) - len(fresh_all)} 个今天已接过"
    if len(fresh) > left:
        note += f"，{len(fresh) - left} 个超出今日可接数量"
    if len(fresh_all) - len(fresh):
        note += f"，{len(fresh_all) - len(fresh)} 个未确认已通关前置精英关卡，未尝试接取"
    if failed:
        note += f"，接取被拒绝：{'、'.join(failed)}"
    carry[ID] = note
    carry[ACCEPTED] = str(len(got))
    carry[ACCEPT_FAILED] = "1" if failed else "0"
    return None


class _Run:
    """日常任务本体的一次执行：记着今日强化石数量、每关打了几次、已结算的关卡。"""

    def __init__(self, ctx: TaskContext, opened: list[int], count: int | None, quests: dict[int, int], note: str,
                 accepted: int = 0, accept_failed: bool = False) -> None:
        self.ctx, self.opened, self.count, self.quests = ctx, opened, count, quests
        self.accepted = accepted
        self.accept_failed = accept_failed
        self.gains, self.tried, self.started = dungeon.Gains(), dict.fromkeys(opened, 0), 0
        self.festival_stats, self.festival_items, festival_issue = festival.dungeon_bonuses(ctx, ID)
        self.parts: list[str] = []
        self.notes = [note] if note else []
        if festival_issue:
            self.notes.append(f"盛典关卡道具未执行：{festival_issue}")
        self.claimed, self.got, self.refused = "", 0, False  # 领取日常任务奖励的说明、领到几个、有没有被拒绝

    def result(self, status: str, why: str = "") -> TaskResult:
        head = [f"今日万能强化石 {self.count}/{DAILY_LIMIT}"] if self.count is not None else []
        settled = [f"已结算 {'、'.join(self.parts)}"] if self.parts else []
        text = "；".join(x for x in (*self.notes, *head, *settled, why, self.claimed) if x)
        if self.refused or self.accept_failed:
            status = BLOCKED
        elif status == SKIPPED and (self.got or self.accepted):  # 副本没打成，但已有确认的接取或领奖
            status = SUBMITTED
        return TaskResult(ID, status, text)

    def commit_ready(self, ready: list[int], rewards: dungeon.Gains, failed: list[str]) -> bool:
        """逐个领奖，用户停止或背包满就不再往下领。返回背包是否满了。"""
        ctx = self.ctx
        try:
            for qid in ready:
                if ctx.stopped:
                    break
                try:
                    rewards.add(quest.commit(ctx.request, qid), ctx.session.drain_pushes())
                except p.AllianceError as exc:
                    failed.append(f"{qid}({exc})")
                    if is_bag_full(exc) or ctx.session.bag_full:
                        return True
                    continue
                self.got += 1
                ctx.log(f"已领取日常任务奖励 {self.got}/{len(ready)}")
        except UncertainOutcome as exc:
            unresolved = max(0, len(ready) - self.got - len(failed))
            progress = f"已确认领取 {self.got} 个日常任务奖励{dungeon.gain_note(rewards)}"
            if unresolved:
                progress += f"；另有 {unresolved} 个奖励结果未确认或未尝试"
            raise UncertainOutcome(f"{progress}；{exc}") from exc
        return False

    def claim(self) -> None:
        """勾选的关打完就领：重新进入角色，领取已完成（状态 2）的日常任务奖励，顺带刷新今日强化石数量。"""
        ctx = self.ctx
        try:
            info = ctx.refresh()
        except p.AllianceError as exc:
            self.notes.append(f"{exc}，未刷新强化石数量，未领取日常任务奖励")
            self.refused = True
            return
        fresh = stones_today(info)
        if fresh is not None:
            self.count = max(self.count or 0, fresh)
        status = quest.status(info)
        if status is None:
            self.claimed = "服务器未返回任务列表，无法确认日常任务状态，未领取奖励"
            self.refused = True
            return
        ready = [q for q in self.quests.values() if status.get(q) == quest.READY]
        rewards, failed = dungeon.Gains(), []
        bag_full = self.commit_ready(ready, rewards, failed)
        unattempted = len(ready) - self.got - len(failed)
        waiting = sum(1 for q in self.quests.values() if status.get(q) == quest.DOING)
        self.claimed = f"已领取日常任务 {self.got} 个奖励{dungeon.gain_note(rewards)}"
        self.claimed += f"，{waiting} 个未完成" if waiting else ""
        self.claimed += f"，领取被拒绝：{'、'.join(failed)}" if failed else ""
        if unattempted:
            if bag_full:
                self.claimed += f"，背包已满，尚有 {unattempted} 个奖励未尝试"
            elif ctx.stopped:
                self.claimed += f"，用户停止，尚有 {unattempted} 个奖励未领取"
        self.refused = bool(failed)

    def attempt(self, mid: int) -> tuple[int, str | None]:
        """打一次。返回 (已结算次数, 停止原因)。"""
        ctx, gains = self.ctx, self.gains
        if ctx.stopped:
            return 0, "已被用户停止"
        lv = dungeon.level(mid)
        before = gains.items[STONE_ID]
        done, stop = dungeon.run_level(ctx, lv, 1, gains, battle_stats=self.festival_stats,
                                       battle_items=self.festival_items)
        if done:
            self.festival_stats, self.festival_items = {}, []  # 盛典目标只算在第一场
            self.tried[mid] += 1
            self.started += 1
            self.parts.append(lv.name)
            if self.count is not None:
                self.count = max(self.count, gains.elite_sweep or 0, min(DAILY_LIMIT, self.count + gains.items[STONE_ID] - before))
            elif gains.elite_sweep is not None:
                self.count = gains.elite_sweep
        elif not stop:  # 本关免费次数已用完
            self.tried[mid] = PER_LEVEL
        return done, f"{lv.name}：{stop}" if stop else None

    def finish(self, stop: str | None = None) -> TaskResult:
        if stop and stop.endswith("已被用户停止"):
            return self.result(SUBMITTED if self.started or self.got or self.accepted else SKIPPED, "已被用户停止")
        if stop:
            return self.result(BLOCKED, f"停止于 {stop}")
        if not self.started and not self.got:
            if self.count is not None and self.count >= DAILY_LIMIT:
                return self.result(SKIPPED, "已达今日强化石目标")
            return self.result(SKIPPED, "没有可用的精英免费次数")
        if self.count is None:
            return self.result(SUBMITTED, "未读到 everyDaySetting.eliteSweep，未补强化石")
        return self.result(SUBMITTED, "已达今日强化石目标" if self.count >= DAILY_LIMIT else "免费次数已用完")

    def top_up(self, mids: list[int]) -> TaskResult:
        """不够 4 个：勾选的关照原顺序再打一遍，还不够就从第一关起往后补；每关合计至多 2 次。"""
        for mid in (*mids, *self.opened):
            while self.count < DAILY_LIMIT and self.tried[mid] < PER_LEVEL:
                done, stop = self.attempt(mid)
                if stop:
                    return self.finish(stop)
                if not done:
                    break
        return self.finish()

    def play(self, mids: list[int], pending: list[int], ready: list[int]) -> TaskResult:
        """先打已接取且尚未完成的日常任务（最新的先打），当场领奖，再补强化石。"""
        ctx = self.ctx
        if ctx.stopped:
            return self.finish("已被用户停止")
        stop = None
        for mid in pending:
            _, stop = self.attempt(mid)
            if stop:
                break
        # 中途停在某一关：前面打完的照领；用户停止 / 背包满不领
        if (pending or ready) and not (stop and (ctx.stopped or is_bag_full(stop))):
            self.claim()
        if ctx.session.bag_full:
            return self.finish("背包已满，停止后续副本操作")
        if self.refused:
            return self.finish("日常任务领奖状态未确认，停止后续副本操作")
        if ctx.stopped:
            return self.finish("已被用户停止")
        if stop:
            return self.finish(stop)
        return self.finish() if self.count is None else self.top_up(mids)


@task(ID, "日常任务", writes=dungeon.ROUTES[dungeon.ELITE] | {dungeon.R_USE_ITEM, quest.R_ACCEPT, quest.R_COMMIT}, prelude=accept, needs=(),  # 没选关也照打精英拿强化石
      choices=Choices(lambda: dungeon.catalog(dungeon.ELITE), "关", toggles=(("stones_only", "只捡强化石"),), limit=MAX_QUESTS))
def run(ctx: TaskContext) -> TaskResult:
    carry = ctx.session.carry
    note = carry.pop(ID, "")
    accepted = max(0, state.num(carry.pop(ACCEPTED, "0")) or 0)
    accept_failed = carry.pop(ACCEPT_FAILED, "0") == "1"

    def early(status: str, detail: str) -> TaskResult:
        if accept_failed:
            status = BLOCKED
        return TaskResult(ID, status, f"{note}；{detail}" if note else detail)

    try:
        info = ctx.refresh()  # 前面的任务（含接取）会改变进度，不能用登录时的快照
    except p.AllianceError as exc:
        return early(BLOCKED, f"刷新角色状态失败，未开始日常副本或领奖：{exc}")
    opened = unlocked(info)
    if opened is None:
        return early(BLOCKED, "服务器未返回副本通关记录，无法确认已解锁的精英关卡，未创建副本")
    if not opened:
        return early(SKIPPED, "尚未解锁精英副本")
    count = stones_today(info)
    picked = chosen(ctx)
    mids = [m for m in reversed(opened) if m in picked]  # 勾选的关从最新解锁的往前打，不按勾选顺序
    quests = {m: q for m in mids if (q := quest_of().get(m))}
    status = quest.status(info)
    if quests and status is None:
        return early(BLOCKED, "服务器未返回任务列表，无法确认哪些日常任务未完成，未创建副本")
    status = status or {}
    pending = [m for m, q in quests.items() if status.get(q) == quest.DOING]
    ready = [q for q in quests.values() if status.get(q) == quest.READY]
    if not pending and not ready and count is None:
        return early(BLOCKED, "服务器未返回 everyDaySetting.eliteSweep，无法确认今日万能强化石数量，未创建副本")
    if not mids and count >= DAILY_LIMIT:
        return early(SKIPPED, f"今日已获得 {count}/{DAILY_LIMIT} 个万能强化石，已达上限")

    job = _Run(ctx, opened, count, quests, note, accepted, accept_failed)
    completed = sum(status.get(q) in (quest.READY, quest.DONE) for q in quests.values())
    if completed:
        job.notes.append(f"{completed} 个日常任务已完成")
    try:
        return job.play(mids, pending, ready)
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"{job.result(SUBMITTED).detail}；{exc}") from exc
