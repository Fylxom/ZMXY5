"""十绝阵·勇气（周常页）：一个人开在线副本，直接向战斗服务器提交结算，等服务器给出积分，再领奖励。

固定按 3000 分结算（不提供 1000 分）；次数由周常页行上的输入框决定（上限 5，也受本周免费奖励次数限制）。
流程（参照提取出的 ``halo_solo.py``，路由/字段对照客户端 HaloProxy / ExitBattleServerCmd）：

1. ``haloHandler.getDungeonId`` 读今天的关卡（必须在 ``HaloHelper.dayDungeonId`` 的勇气轮换里）；
2. ``createWaitingRoom {dungeonId, type:"halo"}`` → ``prepareCreateOnlineCopy {roomId}`` → 上报加载 100% → 等 ``onCreateOnlineCopy``
   （一次性的战斗服务器地址；``copyType`` 必须是今天的关卡）；
3. 连战斗服务器：``enter`` → 立刻 ``battleExit``（3 个席位，每个席位 1000 分）；
4. 主连接等 ``onHaloResult``：服务器给出的积分不到3000 分就不领奖；够了 → ``getHaloReward``。**只发一次，不重发**；
   以响应里的 ``give`` 为准，没有 ``give`` 时用刷新后的 ``haloRewardCnt`` 恰好少 1 来确认，都没有就不算成功。

读不到 ``info.haloRewardCnt`` 就不开房（它是免费奖励剩余次数，付费次数不碰）。任何一步失败都不继续，房间总会尽量退出。
"""

from __future__ import annotations

from .. import data, dungeon, online, protocol as p, state
from . import team_room
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from ..online import R_ENTER, R_EXIT_BATTLE, R_EXIT_ROOM, R_LOADING, R_PREPARE, R_ROOM

ID = "ten_jue"
R_DUNGEON = "scene.haloHandler.getDungeonId"
R_REWARD = "scene.copyHandler.getHaloReward"
SEAT_SCORE = 1000   # 每个席位的积分上限（HaloDungeon.seat1Integral）
TIER = 3000  # 固定 3000 分
MAX_RUNS = 5  # 次数输入框的上限


@data.cached
def rotation() -> frozenset[int]:
    """勇气轮换里的全部关卡编号；表对不上返回空集合。"""
    helper = data.row("HaloHelper", 1) or {}
    ids = [state.num(x) for x in str(helper.get("dayDungeonId", "")).split(";") if x]
    if not ids or len(ids) % 7 or any(i is None for i in ids):
        return frozenset()
    if any(str((data.row("HaloDungeon", i) or {}).get("icon1", "")).lower() != "yongqi.png" for i in ids):
        return frozenset()
    return frozenset(ids)


def reward_left(source) -> int | None:
    n = state.num(state.find(source, "haloRewardCnt", depth=8))
    return n if n is not None and n >= 0 else None


def seat_results(uid: str) -> list[dict]:
    return [{"win": True, "uid": uid, "hp": 1000, "hpMax": 1000, "frame": 0, "remainTime": 600, "time": 600,
             "remainHpPercent": 1000, "score": SEAT_SCORE} for _ in range(TIER // SEAT_SCORE)]


def battle(ctx: TaskContext, room_id: str, dungeon_id: int, uid: str) -> None:
    """提交结算并确认服务器给出的积分达到档位；任何不符抛 AllianceError。"""
    ctx.request(R_PREPARE, {"roomId": room_id})
    ctx.notify(R_LOADING, {"roomId": room_id, "percent": 100, "version": 1})
    created = ctx.wait_push("onCreateOnlineCopy", max(ctx.config.timeout, 20.0))
    where, info = online.endpoint(created), online.body(created) or {}
    if where is None:
        raise p.AllianceError("onCreateOnlineCopy 没有给出完整的 ip、port、instId")
    live = state.num(info.get("copyType", info.get("dungeonId")))
    if live is not None and live != dungeon_id:
        raise p.AllianceError(f"动态副本关卡 {live} 与今天的十绝阵 {dungeon_id} 不一致")
    with ctx.open_team(where.host, where.port) as team:
        team_room.enter_battle(team, where, uid, online.client_cg(ctx.now))  # 客户端无条件发自己的 CurrCg
        team.request(R_EXIT_BATTLE, {
            "battleResult": 5, "frameNum": 0, "data": seat_results(uid),
            "playerInfo": [{"playerId": uid, "hp": 1, "isme": True, "campId": 1, "dead": False, "exit": False, "from": ""}],
        })
    result = ctx.wait_push("onHaloResult", max(ctx.config.timeout, 10.0))
    score = state.num(state.find(result, "score", depth=3))
    if score is None or score < TIER:
        raise p.AllianceError(f"服务器结算积分 {score} 未达到 {TIER} 分")


def one_run(ctx: TaskContext, left: int, uid: str, gains: dungeon.Gains) -> int:
    """打一次并领奖，返回最新的剩余奖励次数；失败抛 dungeon.Stop。"""
    try:
        mid = state.num(ctx.request(R_DUNGEON, {}).get("dungeonId"))
    except p.AllianceError as exc:
        raise dungeon.Stop(f"读取今天的关卡失败：{exc}") from exc
    if mid not in rotation():
        raise dungeon.Stop(f"服务器关卡 {mid} 不在本地勇气轮换表里，未开房")
    online.client_cg(ctx.now)  # 开房前先把当前版本读好（第一次要联网，可能要好几秒）：不能等战斗服务器地址下来了才去读
    try:
        room = ctx.request(R_ROOM, {"dungeonId": mid, "type": "halo"})
    except p.AllianceError as exc:
        raise dungeon.Stop(f"创建房间失败：{exc}") from exc
    room_id = team_room.created_room_id(room)
    if not room_id:
        raise dungeon.Stop("创建房间的响应没有 roomId")
    try:
        battle(ctx, room_id, mid, uid)
    except p.AllianceError as exc:
        raise dungeon.Stop(f"在线战斗未完成：{exc}") from exc
    finally:
        team_room.leave_room(ctx, room_id)
    return claim(ctx, left, gains)


def claim(ctx: TaskContext, left: int, gains: dungeon.Gains) -> int:
    """领刚结算的这一场的奖励，返回最新的剩余奖励次数；失败抛 dungeon.Stop。
    背包满没领到时不能事后单独补领：清完背包要重新打一场才能领（用户告知），所以这一场不计次数，继续（断点）时重打。"""
    try:
        reward = ctx.request(R_REWARD, {})  # 一次性领取，丢了也不重发
    except p.AllianceError as exc:
        again = _refresh_left(ctx)
        if "奖励已领取" in str(exc) and again == left - 1:
            return again
        raise dungeon.Stop(f"在线战斗已结算，但领取奖励未确认：{exc}；不重发领取") from exc
    got = dungeon.Gains()
    got.add(reward, ctx.session.drain_pushes())
    gains.items.update(got.items)
    again = reward_left(reward)
    if again is None:
        again = _refresh_left(ctx)
    if not got.items and again != left - 1:
        raise dungeon.Stop("领奖响应没有 give 发放记录，剩余奖励次数也没有减少；本次不算成功，不重发")
    return again if again is not None else left - 1


def _refresh_left(ctx: TaskContext) -> int | None:
    try:
        return reward_left(ctx.refresh())
    except p.AllianceError:
        return None


def auto_times(ctx: TaskContext) -> int | None:
    """界面“自动配置”：进角信息里的剩余奖励次数 haloRewardCnt。"""
    return reward_left(ctx.info)


@task(ID, "十绝阵", reads={R_DUNGEON},
      writes={R_ROOM, R_PREPARE, R_LOADING, R_EXIT_ROOM, R_ENTER, R_EXIT_BATTLE, R_REWARD}, group="周常",
      times_max=MAX_RUNS, auto=auto_times, reserve=4)  # 次数留给翅膀光环养成周
def run(ctx: TaskContext) -> TaskResult:
    uid = state.num(ctx.info.get("uid"))
    if uid is None or uid <= 0:
        return TaskResult(ID, BLOCKED, "进角响应缺少角色 uid，未开房")
    times = dungeon.read_times(ctx, ID, MAX_RUNS)
    if isinstance(times, TaskResult):
        return times
    if times == 0:
        return TaskResult(ID, SKIPPED, "次数留空，不执行")
    prior = dungeon.resume_done(ctx).get("n", 0)  # 暂停前已打完的次数
    ctx.progress["n"] = prior
    if times - prior <= 0:
        return TaskResult(ID, SUBMITTED, "暂停前已经打完设置的次数")
    times -= prior
    left = reward_left(ctx.info)
    if left is None:
        return TaskResult(ID, BLOCKED, "服务器没有返回 haloRewardCnt（免费奖励剩余次数），未开房")
    if left == 0:
        return TaskResult(ID, SKIPPED, "本周免费奖励次数已用完")
    gains, done, why = dungeon.Gains(), 0, None
    total = min(left, times)  # 这次要打几次（left 在循环里会被服务器的最新值覆盖）
    try:
        for _ in range(total):
            if ctx.stopped:
                why = "已被用户停止"
                break
            try:
                left = one_run(ctx, left, str(uid), gains)
            except dungeon.Stop as stop:
                why = str(stop)
                break
            done += 1
            ctx.progress["n"] = prior + done
            ctx.log(f"十绝阵 {done}/{total}")
            if left <= 0:
                break
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"已完成 {done} 次；{exc}") from exc
    note = f"十绝阵·勇气 {TIER} 分 完成 {done} 次，剩余奖励次数 {left}" + dungeon.gain_note(gains)
    if why:
        return TaskResult(ID, SUBMITTED if done else BLOCKED, f"{note}；{why}" if done else why)
    return TaskResult(ID, SUBMITTED, note)
