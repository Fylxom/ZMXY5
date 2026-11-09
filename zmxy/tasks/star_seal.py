"""诸星封印（宠物捕捉入口内）：消耗已有诸星挑战券通关取材料，不投葫芦、不买券。"""

from collections.abc import Mapping

from .. import data, dungeon, protocol as p, quota, state
from ..core import BLOCKED, PAUSED, SKIPPED, SUBMITTED, Choices, TaskContext, TaskResult, UncertainOutcome, task

ID = "star_seal"
MAX_TIMES = 1000000
MIDS = (10011, 10012, 10013, 10014)
PASS_ID = 101041001  # Prop：诸星挑战券


def config(mid: int) -> tuple[dungeon.Level, int, Mapping] | str:
    """(关卡, 每场券数, 活动行)；表里的入口或消耗类型变了就返回停止原因。"""
    row = data.row("Dungeon", mid) or {}
    aid = state.num(row.get("activityId"))
    activity = (data.row("Activity", aid) or {}) if aid else {}
    cost = state.num(row.get("consume_count"))
    lv = dungeon.plain_level(mid, dungeon.SPECIAL) if mid in MIDS else None
    if (lv is None or row.get("dailyCountMax") != 0 or row.get("consume_type") != "item"
            or state.num(row.get("consume_subType")) != PASS_ID or (cost or 0) <= 0
            or not 101 <= (aid or 0) <= 120 or str(activity.get("dungeonId")) != str(mid)):
        return "诸星封印关卡或挑战券消耗配置不符，未创建副本"
    return lv, cost, activity


def catalog() -> list[tuple[int, str]]:
    return [(mid, f"{cfg[0].name}·{cfg[1]}券") for mid in MIDS if not isinstance(cfg := config(mid), str)]


def _star_total(info: Mapping) -> int | None:
    """客户端 getAllLevelTotalStar：只累加 Dungeon.getStar>0 的关卡。"""
    records = info.get("dungeon")
    if isinstance(records, Mapping):
        entries = records.items()
    elif isinstance(records, list):
        entries = ((None, r) for r in records)
    else:
        return None
    total = 0
    for key, record in entries:
        if not isinstance(record, Mapping):
            return None
        if (state.num((data.row("Dungeon", state.num(record.get("id", key))) or {}).get("getStar")) or 0) > 0:
            stars = state.num(record.get("star"))
            if stars is None or not 0 <= stars <= 3:
                return None
            total += stars
    return total


def _refused(ctx: TaskContext, info: Mapping, mid: int, cost: int, activity: Mapping) -> str | None:
    """用最新角色信息核对进场条件，返回不能进场的原因。"""
    if ctx.now is None:
        return "缺少服务器时间，无法确认诸星封印是否开放"
    if not state.activity_open(activity.get("id"), ctx.now):
        return "诸星封印活动未开放"
    required, stars = state.num(activity.get("star")), _star_total(info)
    if required is None or required < 0 or stars is None or stars < required:
        return f"星数门槛未满足或无法确认（需要 {required}，当前 {stars}）"
    if locked := quota.locked(info, mid):
        return f"关卡未解锁：{locked}"
    owned = state.bag_count(info, PASS_ID)
    if owned is None:
        return "读不到诸星挑战券数量，未创建副本"
    if owned < cost:
        return f"诸星挑战券不足（剩余 {owned}，每次需要 {cost}），未创建副本"
    return None


@task(ID, "诸星封印", writes=dungeon.ROUTES[dungeon.SPECIAL], group="周常",
      times_max=MAX_TIMES, times_default=1,
      choices=Choices(catalog, "关卡", cols=4, limit=1,
                      caption="次数 = 挑战场数；用已有的诸星挑战券，通关拿材料。"))
def run_task(ctx: TaskContext) -> TaskResult:
    times = dungeon.read_times(ctx, ID, MAX_TIMES, 1)
    if isinstance(times, TaskResult):
        return times
    picked = ctx.options.get("levels") or []
    if not picked:
        return TaskResult(ID, SKIPPED, "没有选择诸星封印关卡")
    if len(picked) != 1 or state.num(picked[0]) not in MIDS:
        return TaskResult(ID, BLOCKED, "诸星封印只能选择一个挑战券关卡，未执行")
    return run(ctx, ID, state.num(picked[0]), times)


def run(ctx: TaskContext, task_id: str, mid: int, times: int) -> TaskResult:
    if times == 0:
        return TaskResult(task_id, SKIPPED, "次数留空，不执行")
    prior = dungeon.resume_done(ctx)
    ctx.progress.update(prior)
    done = prior.get(str(mid), 0)
    if done >= times:
        return TaskResult(task_id, SUBMITTED, "暂停前已经完成设置的诸星封印挑战次数")
    gains, why, spent, name = dungeon.Gains(), None, 0, str(mid)
    while done < times and not why:
        if ctx.stopped or ctx.winding:
            return TaskResult(task_id, PAUSED, f"{name} 已完成 {done}/{times} 次；已停止并保存进度",
                              progress=dict(ctx.progress))
        cfg = config(mid)  # 每场重读：周更换表后入口或消耗可能变
        if isinstance(cfg, str):
            why = cfg
            break
        lv, cost, activity = cfg
        name = lv.name
        try:
            info = ctx.refresh()  # 付券进场前取最新余额
        except p.AllianceError as exc:
            why = f"刷新挑战券数量失败：{exc}；未创建副本"
            break
        if ctx.stopped or ctx.winding:
            continue
        why = _refused(ctx, info, mid, cost, activity)
        if why:
            break
        ctx.log(f"诸星封印·{name} {done}/{times}；本次使用诸星挑战券 {cost} 张")
        settled = True
        try:
            dungeon.play(ctx, lv, gains, win=True)
        except UncertainOutcome as exc:
            raise UncertainOutcome(f"诸星封印·{name} 已确认完成 {done}/{times} 次；{exc}") from exc
        except dungeon.Stop as exc:  # 进场后的异常不重试，也不开下一场
            settled, why = exc.settled, str(exc)
        if settled:
            done += 1
            spent += cost
            ctx.progress[str(mid)] = done
    detail = f"诸星封印·{name} 已完成 {done}/{times} 次，本轮已结算场次按配置消耗挑战券 {spent} 张{dungeon.gain_note(gains)}"
    if why:
        return TaskResult(task_id, BLOCKED, f"{detail}；{why}", progress=dict(ctx.progress))
    return TaskResult(task_id, SUBMITTED, detail, progress=dict(ctx.progress))
