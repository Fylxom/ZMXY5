"""噩梦天树王（周常页）：``Dungeon.type=12`` 的三个妖王，服务器每周轮换其中一个，走翅膀副本的专用路由。

流程（客户端 CreateBattleServerCmd / ExitBattleServerCmd / PickUpDropItemCmd）：
``getDungeonWingId`` 读当前妖王 ``dungeonId`` 和剩余次数 ``cnt`` → ``beginDungeonWing`` 返回宝箱掉落数组 ``drop``
→ 逐项 ``getDungeonWingReward {index}`` 领取 → ``endDungeonWing {win:0, 战斗计数…}``（客户端退出战斗就是发 ``win:0``）。
结算本身不发宝箱，所以 drop 缺失/为空/领取失败都不结算。没有选项：每次按服务器剩余次数打完（再查一次确认还有次数才继续）。
购买次数的路由（``buyDungeonWingCnt``）由 guard 拒绝。
"""

from __future__ import annotations

from .. import data, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "tree_king_nightmare"
R_ID = "scene.copyHandler.getDungeonWingId"
R_BEGIN = "scene.copyHandler.beginDungeonWing"
R_END = "scene.copyHandler.endDungeonWing"
R_REWARD = "scene.copyHandler.getDungeonWingReward"
END_BODY = {"win": 0, "maxCombo": 0, "killMonsterCnt": 0, "useMagicWeaponCnt": 0, "useBossCardCnt": 0}
MAX_RUNS = 2  # 次数输入框的上限


def bosses() -> dict[int, str]:
    return {int(k): str(r.get("name")) for k, r in data.table("Dungeon").items() if r.get("type") == 12}


def current(ctx: TaskContext) -> tuple[int, int]:
    """(当前妖王编号, 剩余次数)；读不到或不是已知妖王就抛 AllianceError。"""
    response = ctx.request(R_ID, {})
    mid, left = dungeon._find(response, ("dungeonId", "dungeon_id")), dungeon._find(response, ("cnt",))
    if mid not in bosses():
        raise p.AllianceError(f"{R_ID} 没有返回已知的噩梦天树王关卡（{mid!r}）")
    if left is None or left < 0:
        raise p.AllianceError("服务器没有返回有效的剩余次数")
    return mid, left


def run_once(ctx: TaskContext, gains: dungeon.Gains) -> None:
    """打一次。失败抛 dungeon.Stop；成功返回即已结算。"""
    response = ctx.request(R_BEGIN, {})
    gains.add(response, ctx.session.drain_pushes())
    drops = response.get("drop")
    if not isinstance(drops, list) or not any(d is not None for d in drops):
        raise dungeon.Stop("beginDungeonWing 没有返回宝箱掉落数组，未猜测领取序号，未结算")
    for position, drop in enumerate(drops):
        if drop is None:
            continue
        if not isinstance(drop, dict):
            raise dungeon.Stop(f"drop[{position}] 不是掉落对象，未结算")
        index = state.num(drop["dropIndex"]) if "dropIndex" in drop else position
        if index is None or index < 0:
            raise dungeon.Stop(f"drop[{position}] 的 dropIndex 无效，未结算")
        try:
            claimed = ctx.request(R_REWARD, {"index": index})
        except p.AllianceError as exc:
            raise dungeon.Stop(f"宝箱 {position} 领取失败：{exc}；未结算，请先在游戏内核对") from exc
        gains.add(claimed, ctx.session.drain_pushes())
    try:
        gains.add(ctx.request(R_END, END_BODY), ctx.session.drain_pushes())
    except p.AllianceError as exc:
        raise dungeon.Stop(f"结算未确认：{exc}；请先在游戏内核对") from exc


def _count_dropped(ctx: TaskContext, before: int) -> bool:
    """这一场开打后没做完就停了：服务器的剩余次数是不是已经比开打前少（只读）。读不到按没少。"""
    try:
        return current(ctx)[1] < before
    except p.AllianceError:
        return False


def auto_times(ctx: TaskContext) -> int | None:
    """界面“自动配置”：服务器给的当前妖王剩余次数（只读 getDungeonWingId）。"""
    return current(ctx)[1]


@task(ID, "噩梦天树王", reads={R_ID}, writes={R_BEGIN, R_END, R_REWARD}, group="周常",
      times_max=MAX_RUNS, auto=auto_times)
def run(ctx: TaskContext) -> TaskResult:
    times = dungeon.read_times(ctx, ID, MAX_RUNS)
    if isinstance(times, TaskResult):
        return times
    if times == 0:
        return TaskResult(ID, SKIPPED, "次数留空，不执行")
    try:
        mid, left = current(ctx)
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"无法确认当前妖王和剩余次数：{exc}；未创建")
    name = bosses()[mid]
    if left <= 0:
        return TaskResult(ID, SKIPPED, f"{name}今日次数已用完")
    gains, done, why = dungeon.Gains(), 0, None
    prior = dungeon.resume_done(ctx).get("n", 0)  # 暂停前已打完的次数
    ctx.progress["n"] = prior
    times -= prior
    if times <= 0:
        return TaskResult(ID, SUBMITTED, "暂停前已经打完设置的次数")
    have, left = left, min(left, times)  # have：开打前服务器的剩余次数
    for _ in range(left):
        if ctx.stopped:
            why = "已被用户停止"
            break
        if done:  # 第二次起重新确认妖王没换、次数还有
            try:
                again, remaining = current(ctx)
            except p.AllianceError as exc:
                why = f"无法确认剩余次数：{exc}"
                break
            if again != mid or remaining <= 0:
                break
            have = remaining
        try:
            run_once(ctx, gains)
        except dungeon.Stop as stop:
            why = str(stop)
            if _count_dropped(ctx, have):  # 次数已扣：没结算也算一次，继续（断点）时不再补
                ctx.progress["n"] = prior + done + 1
            break
        done += 1
        ctx.progress["n"] = prior + done
        ctx.log(f"{name} {done}/{left}")
    note = f"{name} 已结算 ×{done}" + dungeon.gain_note(gains)
    if why:
        return TaskResult(ID, BLOCKED, f"{note}；{why}" if done else f"{name}：{why}")
    return TaskResult(ID, SUBMITTED if done else SKIPPED, note if done else "没有可用次数")
