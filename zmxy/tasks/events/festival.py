"""秋枫盛典（活动页，日常队列末尾）：做每日挑战，领已开放的每日/周任务和等级奖励。

对照客户端 HWManager / HW2View / HW2Mediator / UseItemCmd / StoveProxy：

* 任务页：HolidayWelfareHelper 的 ``initTask_N``，按 ``activityTimeId`` 的日期开放；周任务只领不代做，普通任务只领 READY。
* 每日战斗目标（最高连击 / 击败怪物 / 使用法宝）映射到首场副本的结算字段，关卡道具在副本内使用。
  按 日常任务 → 主线 → 精英 复用已选的第一场副本；三个都没选且战斗目标未完成时，用主线第一关失败结算兜底，不拾取、不增加通关记录。
* 炼丹炉：只在勾选的类别里找已解锁、材料够的普通配方，炼一次。
* 在线目标：最后才等，等够后在同一连接直接领取再刷新确认；重新进角会重置客户端计时，所以中间不刷新，也不要求先变 READY。
* 等级奖励：按盛典积分算等级，免费档总领，``open`` 有档位 2 才领付费档。不自动使用清心符。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ... import data, dungeon, protocol as p, quest, state
from ...core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, task
from ..rewards import dungeon_history, passed

ID = "festival"
HELPER_ID = 1
MAX_CLAIMS = 96
MAX_LEVEL_CLAIMS = 160
R_USE_ITEM = dungeon.R_USE_ITEM
R_COMPOSE = "scene.itemHandler.compose"
R_SERVER_TIME = "scene.userHandler.getCurDateTime"
R_LEVEL_CLAIM = "scene.activityHandler.getHolidayWelfare"
_ONLINE, _COMPOSE, _ITEMS = 27, 28, 32  # HolidayWelfareTask.taskType
_BATTLE_FIELDS = {33: "maxCombo", 40: "killMonsterCnt", 34: "useMagicWeaponCnt"}
_BATTLE_ORDER = {kind: i for i, kind in enumerate(_BATTLE_FIELDS)}  # 待办里战斗目标排最前
_UNFINISHED = (quest.FRESH, quest.DOING)
_PENDING = (*_UNFINISHED, quest.READY)
_LEVEL_ITEMS = {101009001: ("一品回元露", 1009), 101010003: ("一阶烈火咒", 1010), 101010001: ("一阶寒冰咒", 1010)}
_COMPOSE_OPTIONS = (("return_1", "一品回元露"), ("return_2", "二品回元露"), ("return_3", "三品回元露"), ("gem_all", "全部宝石"))
_RETURN_LABEL, _GEM_LABELS = 11, frozenset((41, 42, 43, 44, 45, 46))  # Compose.label：回元露页；2～6 级宝石页
_DAILY_PAGE = 1
_DUNGEONS = {"daily_quest": "日常任务", "dungeon_main": "主线", "dungeon_elite": "精英"}  # 复用优先级
_LOST = "在线等待期间 Pomelo 连接中断"


def _running_id(ctx: TaskContext) -> str | None:
    spec = ctx.session._spec
    return spec.id if spec else None


def _selected(ctx: TaskContext) -> bool:
    return ID in ctx.tasks or _running_id(ctx) == ID


def dungeon_runner(ctx: TaskContext) -> str | None:
    """本次承载盛典副本目标的任务：日常 → 主线 → 精英 里最先选中的。"""
    if not _selected(ctx):
        return None
    return next((tid for tid in _DUNGEONS if tid in ctx.tasks or tid == _running_id(ctx)), None)


def _ids(text: Any) -> list[int]:
    return [n for part in str(text or "").split(";") if (n := state.num(part)) and n > 0]


def is_open(now: int) -> bool:
    main_id = state.num((state.row("HolidayWelfareHelper", HELPER_ID) or {}).get("activityId"))
    return main_id is not None and state.activity_open(main_id, now)


def _open_task_pages(helper: Mapping[str, Any], now: int) -> list[tuple[int, list[int]]]:
    """客户端当前显示的任务页：HWManager.openNum 数开放的 Activity，日期窗口依次开放。"""
    main_id = state.num(helper.get("activityId"))
    if main_id is None or not state.activity_open(main_id, now):
        return []
    windows = sorted(_ids(helper.get("activityTimeId")))
    page_count = 0 if windows else 1
    for activity_id in windows:
        if not state.activity_open(activity_id, now):
            break
        page_count += 1
    pages, seen = [], set()
    for page in range(1, min(page_count, 8) + 1):
        ids = [i for i in dict.fromkeys(_ids(helper.get(f"initTask_{page}"))) if i not in seen]
        seen.update(ids)
        if ids:
            pages.append((page, ids))
    return pages


def _open_task_ids(helper: Mapping[str, Any], now: int) -> list[int]:
    pages = _open_task_pages(helper, now)
    return pages[0][1] if pages and pages[0][0] == _DAILY_PAGE else []


def _quest_records(info: Mapping[str, Any]) -> dict[int, Mapping[str, Any]] | None:
    raw = state.find(info, "quest")
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return None
    return {i: r for r in raw if isinstance(r, Mapping) and (i := state.num(r.get("id", r.get("qId")))) is not None}


def _current_task(base_id: int, statuses: Mapping[int, int]) -> Mapping[str, Any] | None:
    """沿已领取的任务链走到当前一项（客户端 HWManager 同一规则）。"""
    ident, seen = base_id, set()
    while ident > 0 and ident not in seen:
        seen.add(ident)
        row = state.row("HolidayWelfareTask", ident)
        status = statuses.get(ident)
        if row is None or status is None:
            return None
        next_id = state.num(row.get("nextTaskId"))
        if next_id is None or next_id <= 0 or status != quest.DONE:
            return row
        ident = next_id
    return None


def _current_tasks(page_ids: list[int], statuses: Mapping[int, int]) -> list[tuple[int, Mapping[str, Any]]]:
    rows = (_current_task(base_id, statuses) for base_id in page_ids)
    return list({i: row for row in rows if row and (i := state.num(row.get("id"))) in statuses}.items())


def _targets(row: Mapping[str, Any]) -> list[tuple[int, int]] | None:
    ids, goals = (str(row.get(key) or "").split(";") for key in ("targetId", "targetNum"))
    pairs = [(state.num(i), state.num(g)) for i, g in zip(ids, goals)]
    valid = len(ids) == len(goals) and all(i is not None and g is not None and g >= 0 for i, g in pairs)
    return pairs if valid else None


def _progress_value(record: Mapping[str, Any], target_id: int) -> int | None:
    """TaskDataVo.progress 默认是空对象，没出现过的目标键按 0。"""
    progress = record.get("progress") or {}
    if not isinstance(progress, Mapping):
        return None
    raw = progress.get(str(target_id), progress.get(target_id))
    value = 0 if raw in (None, "") else state.num(raw)
    return value if value is not None and value >= 0 else None


def _battle_stats_for(info: Mapping[str, Any], helper: Mapping[str, Any], now: int) -> dict[str, int]:
    """未完成的每日战斗目标值 → 副本结算字段。"""
    daily_ids, statuses = _open_task_ids(helper, now), quest.status(info)
    if not daily_ids or statuses is None:
        return {}
    out = {}
    for ident, row in _current_tasks(daily_ids, statuses):
        field = _BATTLE_FIELDS.get(state.num(row.get("taskType")))
        goal = state.num(str(row.get("targetNum") or "").split(";")[0])
        if field and statuses[ident] in _UNFINISHED and goal and goal > 0:
            out[field] = goal
    return out


def battle_stats(ctx: TaskContext) -> dict[str, int]:
    if not _selected(ctx) or ctx.now is None:
        return {}
    try:
        helper = state.row("HolidayWelfareHelper", HELPER_ID)
        return _battle_stats_for(ctx.info, helper, ctx.now) if helper else {}
    except data.DataError:
        return {}


def dungeon_bonuses(ctx: TaskContext, runner_id: str) -> tuple[dict[str, int], list[int], str | None]:
    """把盛典统计和关卡道具交给优先级最高的已选副本。"""
    if dungeon_runner(ctx) != runner_id:
        return {}, [], None
    try:
        items, issue = battle_item_plan(ctx)
    except data.DataError as exc:
        items, issue = [], f"读取盛典关卡道具配置失败：{exc}"
    return battle_stats(ctx), items, issue


def _options(ctx: TaskContext) -> Mapping[str, Any]:
    """盛典自己的选项；日常副本执行时在 all_options 里。"""
    return ctx.all_options.get(ID) or (ctx.options if _running_id(ctx) == ID else {})


def _enabled(ctx: TaskContext, key: str) -> bool:
    return bool(_options(ctx).get(key, True))


def _usable_prop(ident: int, prop_type: int) -> bool:
    row = state.row("Prop", ident)
    return (row is not None and state.num(row.get("propType")) == prop_type
            and row.get("type") in (None, "item") and state.num(row.get("isBattle")) == 1)


def battle_item_plan(ctx: TaskContext) -> tuple[list[int], str | None]:
    """规划一次副本内用的关卡道具；有一类凑不齐就整个不用，不发部分请求。"""
    if not _enabled(ctx, "do_items") or not _selected(ctx) or ctx.now is None:
        return [], None
    helper = state.row("HolidayWelfareHelper", HELPER_ID)
    if not helper:
        return [], "盛典配置缺失，无法读取关卡道具目标"
    page_ids, statuses, records = _open_task_ids(helper, ctx.now), quest.status(ctx.info), _quest_records(ctx.info)
    if not page_ids or statuses is None or records is None:
        return [], "服务器未返回盛典任务状态，无法规划关卡道具"
    row = next((row for ident, row in _current_tasks(page_ids, statuses)
                if statuses[ident] in _UNFINISHED and state.num(row.get("taskType")) == _ITEMS), None)
    if row is None:
        return [], None
    targets = _targets(row)
    if not targets:
        return [], "关卡道具任务目标配置无法解析"
    wanted = {state.num(raw) for raw in _options(ctx).get("items") or ()}
    selected = [ident for ident in _LEVEL_ITEMS if ident in wanted]
    if not selected:
        return [], "未配置盛典关卡道具"
    owned = {ident: state.bag_count(ctx.info, ident) for ident in selected}
    if None in owned.values():
        return [], "服务器未返回背包，无法规划盛典关卡道具"
    record = records.get(state.num(row.get("id")), {})
    plan: list[int] = []
    for prop_type, goal in targets:
        current = _progress_value(record, prop_type)
        if current is None:
            return [], f"服务器未返回关卡道具 {prop_type} 的任务进度"
        need = goal - current
        configured = [ident for ident in selected if _LEVEL_ITEMS[ident][1] == prop_type]
        if need <= 0 or not configured:
            continue  # 1050 清心符这类没配置的类型不擅自补用
        usable = [ident for ident in configured if owned[ident] > 0 and _usable_prop(ident, prop_type)]
        if not usable:
            return [], f"已配置 Prop.propType={prop_type}，但背包中没有可用关卡道具"
        for ident in usable:
            used = min(need, owned[ident])
            plan += [ident] * used
            need -= used
        if need:
            return [], f"{_LEVEL_ITEMS[usable[0]][0]} 背包数量不足，需再使用 {need} 次"
    return plan, None


class _OnlineWait:
    """等服务器时间走够 need 秒。只认同一连接上服务器时间的增量，最多每 300 秒校时；时钟异常不猜测已完成。"""

    def __init__(self, ctx: TaskContext, goal: int, known: int, need: int) -> None:
        self.ctx, self.goal, self.known, self.need = ctx, goal, known, need  # known：开始等之前已在线多久
        self.connection = ctx.session._conn
        self.waited = self.elapsed = 0

    def interrupted(self) -> str | None:
        ctx = self.ctx
        if ctx.stopped:
            return "已被用户停止"
        if not ctx.session.live or ctx.session._conn is not self.connection:
            reason = ctx.session.idle_error
            return f"{_LOST}，在线进度未确认" + (f"：{reason}" if reason else "")
        return None

    def reached(self) -> tuple[bool, None]:
        self.ctx.log(f"盛典在线时间已达标 {self.goal}/{self.goal}，准备领取并刷新确认")
        return True, None

    def server_time(self) -> int | None:
        return state.seconds(self.ctx.request(R_SERVER_TIME, {}).get("serverTime"))

    def sleep(self, pending: int) -> str | None:
        """分小段睡够 pending 秒，每段都看一次停止和断线。"""
        while pending > 0:
            if issue := self.interrupted():
                return issue
            step = min(30, pending)
            if not self.ctx.sleep(step, background=False):
                return self.interrupted() or "在线等待被中断"
            pending -= step
            self.waited += step
            self.ctx.log(f"盛典在线等待 {self.waited}/{self.need} 秒（已确认进度 {min(self.goal, self.known + self.elapsed)}/{self.goal}）")
        return None

    def run(self) -> tuple[bool, str | None]:
        if issue := self.interrupted():
            return False, issue
        start = self.server_time()
        if start is None or start <= 0:
            return False, "服务器时间同步失败，在线进度未确认"
        if self.need <= 0:
            return self.reached()
        previous = start
        limit = self.need + 60  # 宽限整秒取整；时钟异常也不无限等
        self.ctx.log(f"等待盛典在线时间 {self.need} 秒（已在线约 {self.known}/{self.goal}）")
        while self.waited < limit:
            if issue := self.sleep(min(300, self.need - self.elapsed + 1, limit - self.waited)) or self.interrupted():
                return False, issue
            now = self.server_time()
            if issue := self.interrupted():
                return False, issue
            if now is None or now <= previous:
                return False, "服务器时间未正常推进，在线进度未确认"
            if (now + 28800) // 86400 != (start + 28800) // 86400:
                return False, "在线等待跨越服务器每日重置，需刷新今日任务后重新执行"
            previous, self.elapsed = now, now - start
            if self.elapsed >= self.need:
                return self.reached()
        return False, "服务器时间累计不足，在线进度未确认"


def _wait_online(ctx: TaskContext, row: Mapping[str, Any], record: Mapping[str, Any]) -> tuple[bool, str | None]:
    """等到在线目标达标。"""
    targets = _targets(row)
    if not targets:
        return False, "在线任务目标配置无法解析"
    target_id, goal = targets[0]
    current = _progress_value(record, target_id)
    if current is None:
        return False, "服务器未返回在线任务进度"
    remaining = goal - current
    if remaining <= 0:
        return True, None
    # 客户端在线计时从首次进角累计，前面任务的耗时也算；留 5 秒给初始化和取整。
    credit = min(remaining, max(0, ctx.session.online_seconds - 5))
    return _OnlineWait(ctx, goal, current + credit, remaining - credit).run()


def _compose_category(row: Mapping[str, Any]) -> str | None:
    """按 Compose 的目标页和品级分类，不从配方名猜宝石种类。"""
    if row.get("targetType") != "item":
        return None
    label = state.num(row.get("label"))
    if label == _RETURN_LABEL:
        name = str(row.get("name") or "").replace(" ", "")
        return next((key for key, title in _COMPOSE_OPTIONS if title == name and key != "gem_all"), None)
    return "gem_all" if label in _GEM_LABELS else None


def _unlocked(row: Mapping[str, Any], info: Mapping[str, Any]) -> bool:
    dungeon_id = state.num(row.get("dungeonId"))
    return dungeon_id == -1 or passed(dungeon_history(info), dungeon_id)


def _affordable(row: Mapping[str, Any], info: Mapping[str, Any]) -> bool:
    """配方只吃背包物品，且每种材料都够；配置读不懂按不够。"""
    kinds, subs, counts = (str(row.get(key) or "").split(";") for key in ("consume_type", "consume_subType", "consume_count"))
    if not len(kinds) == len(subs) == len(counts) or any(kind.strip() != "item" for kind in kinds):
        return False
    required: dict[int, int] = {}
    for sub_group, count_group in zip(subs, counts):
        item_ids, item_counts = sub_group.split(","), count_group.split(",")
        if len(item_ids) != len(item_counts):
            return False
        for raw_id, raw_count in zip(item_ids, item_counts):
            item_id, count = state.num(raw_id), state.num(raw_count)
            if item_id is None or count is None or count < 0:
                return False
            required[item_id] = required.get(item_id, 0) + count
    return bool(required) and all((state.bag_count(info, i) or 0) >= n for i, n in required.items())


def _choose_recipe(info: Mapping[str, Any], wanted: set[str]) -> tuple[Mapping[str, Any] | None, str | None]:
    if not wanted:
        return None, "未配置盛典炼丹炉配方"
    level = state.num((state.section(info, "compose") or {}).get("level"))
    if level is None:
        return None, "服务器未返回炼丹炉等级"
    if state.bag_count(info, 0) is None:
        return None, "服务器未返回背包，无法炼丹"
    for row in sorted(state.table_rows("Compose"),
                      key=lambda r: [state.num(r.get(key)) or 999999 for key in ("composeLevel", "order", "id")]):
        recipe_level = state.num(row.get("composeLevel"))
        if (state.num(row.get("id")) is not None and recipe_level is not None and recipe_level <= level
                and state.num(row.get("mifangId")) == -1 and _compose_category(row) in wanted
                and _unlocked(row, info) and _affordable(row, info)):
            return row, None
    return None, "所选炼丹炉类型没有已解锁且材料足够的普通配方"


def _compose_once(ctx: TaskContext, row: Mapping[str, Any], record: Mapping[str, Any], gains: dungeon.Gains) -> tuple[bool, str | None]:
    targets = _targets(row)
    if not targets:
        return False, "炼丹炉任务目标配置无法解析"
    current = _progress_value(record, targets[0][0])
    if current is None:
        return False, "服务器未返回炼丹炉任务进度"
    if current >= targets[0][1]:
        return False, None
    recipe, issue = _choose_recipe(ctx.info, {str(v) for v in _options(ctx).get("compose") or ()})
    if recipe is None:
        return False, issue
    recipe_id = state.num(recipe.get("id"))
    try:
        response = ctx.request(R_COMPOSE, {"composeId": recipe_id})
    except p.AllianceError as exc:
        return False, f"炼丹炉炼制被拒：{exc}"
    gains.add(response, ctx.session.drain_pushes())
    ctx.log(f"已提交盛典炼丹炉目标（{dict(_COMPOSE_OPTIONS)[_compose_category(recipe)]}，配方 {recipe_id}）")
    return True, None


def _daily_actions(ctx: TaskContext, tasks: list[tuple[int, Mapping[str, Any]]], statuses: Mapping[int, int],
                   records: Mapping[int, Mapping[str, Any]], gains: dungeon.Gains,
                   *, online_ready: set[int] | None = None) -> tuple[bool, list[str]]:
    """执行每日在线/炼丹炉目标。关卡道具只能由副本内使用，这里不碰。返回 (是否发生了动作, 问题)。"""
    attempted, issues = False, []
    for ident, row in tasks:
        if ctx.stopped or statuses.get(ident) not in _UNFINISHED:
            continue
        kind, record = state.num(row.get("taskType")), records.get(ident, {})
        if kind == _ONLINE and _enabled(ctx, "do_online"):
            changed, issue = _wait_online(ctx, row, record)
            if changed and online_ready is not None:
                online_ready.add(ident)
        elif kind == _COMPOSE and _enabled(ctx, "do_compose"):
            changed, issue = _compose_once(ctx, row, record, gains)
        else:
            continue
        attempted = attempted or changed
        if issue:
            issues.append(f"{row.get('name') or row.get('describe') or ident}：{issue}")
    return attempted, issues


def _level_reward_ids(info: Mapping[str, Any], helper: Mapping[str, Any]) -> tuple[list[int] | None, str | None]:
    """可领的等级奖励 id；配置缺行时带上问题说明，读不到状态时返回 (None, 问题)。"""
    welfare = state.section(info, "holidayWelfare")
    if welfare is None:
        return None, "服务器未返回 holidayWelfare 等级奖励状态"
    money, step, level_max = (state.num(source.get(key)) for source, key in (
        (welfare, "holidayMoney"), (helper, "oneHolidayMoney"), (helper, "levelMax")))
    if money is None or money < 0 or not step or step < 0 or not level_max or level_max < 0:
        return None, "盛典积分或等级配置缺失，无法确认可领取等级"
    opened = welfare.get("open")
    if opened is None:
        opened = {"1": 1}  # HWManager.initData 的默认值
    if not isinstance(opened, Mapping):
        return None, "服务器返回的 holidayWelfare.open 格式异常"
    history = welfare.get("rewardHistory") or {}
    if not isinstance(history, Mapping):
        return None, "服务器返回的等级奖励领取记录格式异常"
    tracks = [track for track in (1, 2) if track in opened or str(track) in opened]
    out, missing = [], []
    for level in range(1, min(money // step + 1, level_max) + 1):
        for ident in (track * 1000 + level for track in tracks):
            row = state.row("HolidayWelfare", ident)
            if row is None or state.num(row.get("levelType")) != ident // 1000:
                missing.append(ident)
            elif str(row.get("subType")) != "-1" and ident not in history and str(ident) not in history:
                out.append(ident)
    return out, f"等级奖励配置缺少 id：{','.join(map(str, missing[:8]))}" if missing else None


def _progress_text(row: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    target = str(row.get("targetId") or "").split(";")[0]
    progress = record.get("progress")
    have = state.num(progress.get(target, progress.get(state.num(target)))) if isinstance(progress, Mapping) else None
    goal = state.num(str(row.get("targetNum") or "").split(";")[0])
    name = str(row.get("name") or row.get("describe") or "盛典任务")
    detail = f"{name} {have}/{goal}" if have is not None and goal is not None else name
    return detail + ("（需游戏内真实战斗）" if state.num(row.get("taskType")) in _BATTLE_FIELDS else "")


def _pending(page_ids: list[int], statuses: Mapping[int, int], records: Mapping[int, Mapping[str, Any]]) -> list[str]:
    rows = [(ident, row) for ident, row in _current_tasks(page_ids, statuses) if statuses[ident] in _PENDING]
    rows.sort(key=lambda item: _BATTLE_ORDER.get(state.num(item[1].get("taskType")), 9))
    return [_progress_text(row, records.get(ident, {})) for ident, row in rows]


class _Halt(Exception):
    """后续步骤不能继续；已确认的明细保留，结果标 blocked。"""


class _Run:
    """一次执行的进度：每步都以最新的任务状态为准，已领取的明细在中途停下时也留着。"""

    def __init__(self, ctx: TaskContext, helper: Mapping[str, Any], pages: list[tuple[int, list[int]]],
                 info: Mapping[str, Any], statuses: dict[int, int], records: dict[int, Mapping[str, Any]]) -> None:
        self.ctx, self.helper, self.info, self.statuses, self.records = ctx, helper, info, statuses, records
        self.daily_ids = _open_task_ids(helper, ctx.now)
        self.claim_ids = [ident for _, ids in pages for ident in ids]
        self.gains = dungeon.Gains()
        self.claimed: list[int] = []
        self.level_claimed: list[int] = []
        self.issues: list[str] = []
        self.level_issue: str | None = None
        self.composed = self.fallback_used = False
        self.reused: str | None = None

    def reload(self, done: str) -> None:
        try:
            self.info = self.ctx.refresh()
        except p.AllianceError as exc:
            raise _Halt(f"{done}，但刷新盛典状态失败：{exc}") from exc
        self.statuses, self.records = quest.status(self.info), _quest_records(self.info)
        if self.statuses is None or self.records is None:
            raise _Halt(f"{done}，但服务器未返回后续 quest 列表")

    def claim_ready(self, online_ready: set[int] = frozenset()) -> None:
        """领 READY 的任务；online_ready 是本次真实等够的在线目标，服务器不一定已推成 READY。"""
        ctx = self.ctx

        def claimable(ident: int) -> bool:
            status = self.statuses.get(ident)
            return ident not in self.claimed and (status == quest.READY or (ident in online_ready and status in _UNFINISHED))

        while not ctx.stopped and len(self.claimed) < MAX_CLAIMS:
            ready = next((ident for ident, _ in _current_tasks(self.claim_ids, self.statuses) if claimable(ident)), None)
            if ready is None:
                return
            try:
                response = quest.commit(ctx.request, ready)
            except p.AllianceError as exc:
                raise _Halt(f"领取任务 {ready} 被拒：{exc}；停止后续领取") from exc
            self.claimed.append(ready)
            self.gains.add(response, ctx.session.drain_pushes())
            ctx.log(f"已领取盛典任务奖励 {len(self.claimed)} 项")
            self.reload("奖励已领取")
        if len(self.claimed) >= MAX_CLAIMS:
            raise _Halt(f"达到单次领取上限 {MAX_CLAIMS}，停止继续领取")

    def daily_tasks(self, kind: int, toggle: str) -> list[tuple[int, Mapping[str, Any]]]:
        if self.ctx.stopped or not _enabled(self.ctx, toggle):
            return []
        return [(i, row) for i, row in _current_tasks(self.daily_ids, self.statuses) if state.num(row.get("taskType")) == kind]

    def compose(self) -> None:
        tasks = self.daily_tasks(_COMPOSE, "do_compose")
        if not tasks:
            return
        self.composed, self.issues = _daily_actions(self.ctx, tasks, self.statuses, self.records, self.gains)
        if not self.ctx.stopped:
            self.reload("每日目标已提交")

    def main_fallback(self) -> None:
        """三类副本都没选时，用主线第一关失败结算完成战斗目标和关卡道具目标。"""
        ctx = self.ctx
        stats = _battle_stats_for(self.info, self.helper, ctx.now)
        items = battle_item_plan(ctx)[0]
        if ctx.stopped or not (stats or items):
            return
        first_main = dungeon.level(1)
        if first_main is None or first_main.kind != dungeon.MAIN:
            self.issues.append("主线第一关配置缺失，盛典目标未完成")
            return
        done, stop = dungeon.run_level(ctx, first_main, 1, self.gains, battle_stats=stats, collect=False,
                                       battle_items=items, win=False)
        if not done:
            if not ctx.stopped:
                self.issues.append(f"主线第一关未结算，盛典目标未完成：{stop or '副本未结算'}")
            return
        self.fallback_used = True
        if stop and not ctx.stopped:
            self.issues.append(f"主线第一关已结算，但副本执行未正常结束：{stop}")
        self.reload("主线第一关已结算")
        self.claim_ready()

    def online(self) -> None:
        tasks = self.daily_tasks(_ONLINE, "do_online")
        if not tasks:
            return
        ready: set[int] = set()
        _, issues = _daily_actions(self.ctx, tasks, self.statuses, self.records, self.gains, online_ready=ready)
        self.issues += [issue for issue in issues if issue not in self.issues]
        if any(_LOST in issue for issue in issues):
            raise _Halt("每日目标执行问题：" + "；".join(self.issues))
        if self.ctx.stopped:
            return
        self.claim_ready(ready)
        if any(ident in ready and self.statuses.get(ident) in _PENDING for ident in self.claimed):
            raise _Halt("在线奖励已提交，但刷新后的任务状态未确认已领取；未重复提交")

    def check_items(self, runner: str | None) -> None:
        plan, issue = battle_item_plan(self.ctx)
        if issue:
            self.issues.append(f"关卡道具目标：{issue}")
        elif plan and runner:
            self.issues.append(f"关卡道具目标：已按{_DUNGEONS[runner]}复用副本，但关卡道具尚未完成")
        elif plan and self.fallback_used:
            self.issues.append("关卡道具目标：已执行主线第一关兜底，但关卡道具目标尚未确认完成")

    def claim_levels(self) -> None:
        ctx = self.ctx
        while not ctx.stopped and len(self.level_claimed) < MAX_LEVEL_CLAIMS:
            ids, issue = _level_reward_ids(self.info, self.helper)
            self.level_issue = self.level_issue or issue
            ready = next((ident for ident in ids or () if ident not in self.level_claimed), None)
            if ready is None:
                return
            try:
                response = ctx.request(R_LEVEL_CLAIM, {"id": ready})
            except p.AllianceError as exc:
                raise _Halt(f"领取等级奖励 {ready} 被拒：{exc}；停止后续领取") from exc
            self.level_claimed.append(ready)
            self.gains.add(response, ctx.session.drain_pushes())
            ctx.log(f"已领取盛典等级奖励 {len(self.level_claimed)} 项")
            self.reload("等级奖励已领取")
        if len(self.level_claimed) >= MAX_LEVEL_CLAIMS:
            self.level_issue = f"达到等级奖励单次领取上限 {MAX_LEVEL_CLAIMS}，停止继续领取"

    def execute(self) -> None:
        runner = dungeon_runner(self.ctx)
        if runner and (battle_item_plan(self.ctx)[0] or _battle_stats_for(self.info, self.helper, self.ctx.now)):
            self.reused = runner
        self.claim_ready()
        self.compose()
        self.claim_ready()
        if runner is None:
            self.main_fallback()
        self.online()
        self.check_items(runner)
        self.claim_levels()

    def detail(self, suffix: str = "") -> str:
        parts = [f"{label} {len(ids)} 项" for label, ids in (("任务奖励", self.claimed), ("等级奖励", self.level_claimed)) if ids]
        text = "已领取盛典" + "、".join(parts) if parts else "没有可领取的盛典任务或等级奖励"
        text += dungeon.gain_note(self.gains)
        return f"{text}；{suffix}" if suffix else text

    def result(self) -> TaskResult:
        stopped = self.ctx.stopped
        waiting = _pending(self.daily_ids, self.statuses, self.records)
        todo = "尚未完成：" + "、".join(waiting[:8]) + (f" 等{len(waiting)}项" if len(waiting) > 8 else "") if waiting else ""
        notes = []
        if stopped:
            notes.append("用户停止")
        elif self.fallback_used:
            notes.append("已执行主线第一关失败结算，不增加通关记录、不拾取掉落")
        elif self.reused and waiting:
            notes.append(f"已按{_DUNGEONS[self.reused]}复用副本，未另开主线副本")
        if self.issues:
            notes.append("每日目标执行问题：" + "；".join(self.issues))
        if self.level_issue:
            notes.append("等级奖励领取问题：" + self.level_issue)
        done = bool(self.claimed or self.level_claimed or self.fallback_used)
        if self.issues or self.level_issue or (stopped and (done or self.composed)):
            status = BLOCKED
        else:
            status = SUBMITTED if done else SKIPPED
        return TaskResult(ID, status, self.detail("；".join(notes)), todo=todo)


@task(
    ID,
    "秋枫盛典",
    needs=(),  # 炼丹炉没选配方也照做在线任务、关卡道具
    reads={R_SERVER_TIME},
    writes=dungeon.ROUTES[dungeon.MAIN] | {quest.R_COMMIT, R_LEVEL_CLAIM, R_USE_ITEM, R_COMPOSE},
    group="活动",
    last=True,
    after_saves=True,
    window=is_open,
    choices=Choices(
        lambda: [(ident, name) for ident, (name, _) in _LEVEL_ITEMS.items()],
        "道具",
        option="items",
        cols=3,
        caption="使用关卡道具 / 炼丹炉",
        button="选择",
        toggles=(
            ("do_items", "使用关卡道具"),
            ("do_compose", "做炼丹炉"),
            ("do_online", "做在线任务"),
        ),
        toggle_defaults=(
            ("do_items", True),
            ("do_compose", True),
            ("do_online", True),
        ),
        picks=(Pick("compose", "炼丹炉", lambda _acc: list(_COMPOSE_OPTIONS), single=False),),
        note="战斗目标借日常任务 → 主线 → 精英里第一场选了的副本完成；都没选就打主线第一关按失败结算兜底（不捡掉落）。\n"
             "炼丹炉只在勾选的类别里找有材料的配方。",
    ),
)
def run(ctx: TaskContext) -> TaskResult:
    if ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少服务器时间，无法判断盛典任务页是否开放；未领取")
    try:
        helper = state.row("HolidayWelfareHelper", HELPER_ID)
        if not helper:
            return TaskResult(ID, BLOCKED, "HolidayWelfareHelper 配置缺失；未领取")
        pages = _open_task_pages(helper, ctx.now)
        if not pages:
            return TaskResult(ID, SKIPPED, "秋枫盛典当前未开放任务页")
        info = ctx.refresh()
    except (data.DataError, p.AllianceError) as exc:
        return TaskResult(ID, BLOCKED, f"读取盛典任务状态失败：{exc}；未领取")
    statuses, records = quest.status(info), _quest_records(info)
    if statuses is None or records is None:
        return TaskResult(ID, BLOCKED, "服务器未返回可读取的 quest 列表；未领取")
    job = _Run(ctx, helper, pages, info, statuses, records)
    try:
        job.execute()
    except _Halt as exc:
        return TaskResult(ID, BLOCKED, job.detail(str(exc)))
    except (data.DataError, p.AllianceError) as exc:
        return TaskResult(ID, BLOCKED, job.detail(f"盛典执行中断：{exc}"))
    return job.result()
