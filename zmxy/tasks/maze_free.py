"""造梦迷宫（日常页）：挑战别人上传的迷宫，只打能拿奖励的。

顺序：① 推荐榜（type 80）的迷宫，可能给元宝（同一迷宫只有一次奖励）；② 标准榜（90）/ 随机榜（110）里热度 ≥ 500
且通关率 < 10% 的迷宫，可能给迷宫币（客户端“迷宫说明”第 7 条）。迷宫币拿满每日上限（50）就整个结束；元宝满了只是不再打推荐榜那一类。

流程（客户端 FMRankView / FMProxy / FMGameView）：``userMazeHandler.getRank {page,type}`` 取榜 → ``getMazeData {uid,pos}`` 取地图
→ 校验房间传送门图（起点房间 1 能走到出口门 12000；入口门 11000 是客户端自己造的虚拟房间，不在服务器地图里）→
``beginChallenge {uid,pos}`` → ``endChallenge {type:"game pass", win:true, criticism:{0:0,1:0,2:0}}``
（客户端通关后的原样提交，三个评价没点选就是 0）。奖励只认响应里的 ``give``（``subType`` 为 ``yuanbao`` / ``freeMazeMoney``）。
``beginChallenge`` 之后任何失败都不重发结算。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "free_maze"
R_RANK = "scene.userMazeHandler.getRank"
R_DATA = "scene.userMazeHandler.getMazeData"
R_BEGIN = "scene.userMazeHandler.beginChallenge"
R_END = "scene.userMazeHandler.endChallenge"
RECOMMEND_RANK, STANDARD_RANK, RANDOM_RANK = 80, 90, 110
EXIT_DOOR = 12000
END_BODY = {"type": "game pass", "win": True, "criticism": {"0": 0, "1": 0, "2": 0}}
YUANBAO_MAX = 200  # FreeMazeCommon.yuanbaoDayMax：每日元宝上限（推荐榜上的迷宫数量有限，有就打，没有不强求）
COIN_MAX = 50  # FreeMazeCommon.freeMazeMoneyMax：每日迷宫币上限
HEAT_MIN = 500  # 迷宫说明：挑战的迷宫热度（挑战总数 inCnt）要达到 500
PASS_MAX = 0.10  # 通关率要小于 10%
MAX_PAGES = 4  # 每个榜最多翻几页
MAX_CHALLENGES = 30  # 一次执行最多打几个（掉落是概率的，不能无限打）
MAX_DRY = 6  # 连续这么多个都没掉奖励就停（多半是推荐榜已经打过、同一迷宫不再给奖励，或今日已满）


def _rows(value: Any, depth: int = 6) -> list[Mapping[str, Any]]:
    """榜单行：list 里的 dict，且至少有一行带 uid 和 pos（或 _id）。"""
    if depth < 0:
        return []
    if isinstance(value, list):
        rows = [r for r in value if isinstance(r, Mapping)]
        if any(state.num(r.get("uid")) is not None and (state.num(r.get("pos")) is not None or r.get("_id")) for r in rows):
            return rows
        children = value
    elif isinstance(value, Mapping):
        children = list(value.values())
    else:
        return []
    for child in children:
        if found := _rows(child, depth - 1):
            return found
    return []


def target(row: Mapping[str, Any]) -> tuple[int, int, str] | None:
    uid, pos = state.num(row.get("uid")), state.num(row.get("pos"))
    if pos is None and row.get("_id"):  # "uid_pos"
        parts = str(row["_id"]).split("_")
        if len(parts) >= 2:
            uid, pos = uid if uid is not None else state.num(parts[0]), state.num(parts[1])
    if uid is None or uid <= 0 or pos is None or pos < 0:
        return None
    return uid, pos, str(row.get("name") or f"迷宫 {uid}_{pos}")


def pays_coin(row: Mapping[str, Any]) -> bool:
    """榜单行里的热度和通关率满足迷宫币条件；行里没有挑战数据 = 不确定 = 不打。"""
    inn, out = state.num(row.get("inCnt")), state.num(row.get("outCnt"))
    if inn is None or inn < HEAT_MIN or out is None or out < 0:
        return False
    return out / inn < PASS_MAX


def route_ok(raw: Any) -> str | None:
    """地图可通关返回 None，否则是原因。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return "地图 data 不是有效 JSON"
    if not isinstance(raw, Mapping):
        return "地图 data 不是房间对象"
    rooms = {}
    for key, room in raw.items():
        room_id = state.num(room.get("roomId", key)) if isinstance(room, Mapping) else None
        if room_id and room_id > 0:
            rooms[room_id] = room
    if 1 not in rooms:
        return "地图缺少起始房间 1"
    graph: dict[int, set[int]] = {r: set() for r in rooms}
    exits: set[int] = set()
    for room_id, room in rooms.items():
        doors = room.get("door")
        for door in (doors.values() if isinstance(doors, Mapping) else doors if isinstance(doors, list) else []):
            dest = state.num(door.get("id")) if isinstance(door, Mapping) else None
            if dest == EXIT_DOOR:
                exits.add(room_id)
            elif dest in rooms:
                graph[room_id].add(dest)
    reached, pending = {1}, [1]
    while pending:
        for nxt in graph[pending.pop()] - reached:
            reached.add(nxt)
            pending.append(nxt)
    if not exits:
        return "地图没有出口门"
    return None if reached & exits else "起点房间无法到达出口"


def _board(ctx: TaskContext, rank: int, pages: int) -> list[Mapping[str, Any]]:
    """一个榜的行（读不到就当空榜，不算错误）。"""
    out: list[Mapping[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            rows = _rows(ctx.request(R_RANK, {"page": page, "type": rank}))
        except p.AllianceError:
            break
        if not rows:
            break
        out += rows
    return out


def _left_after(cap: int, reported: int | None, left: int, gained: int) -> int:
    """结算后还能拿多少：服务器报了今日已得数就以它为准（客户端也是这样更新的），否则按掉落累减。"""
    if reported is not None and reported >= 0:
        return max(0, cap - reported)
    return max(0, left - gained)


class Gave:
    """今日还能拿多少元宝 / 迷宫币（登录信息里的已得数 - 本次已得）。"""

    def __init__(self, info: Any) -> None:
        yuanbao = state.used(info, "userMazeYuanbao")
        coin = state.used(info, "userMazeMoney")
        self.valid = yuanbao is not None and coin is not None
        self.yuanbao = max(0, YUANBAO_MAX - yuanbao) if yuanbao is not None else 0
        self.coin = max(0, COIN_MAX - coin) if coin is not None else 0

    def take(self, gains: dungeon.Gains, response: Any) -> None:
        yuanbao, coin = (state.num(state.find(response, k, depth=6)) for k in ("userMazeYuanbao", "userMazeMoney"))
        self.yuanbao = _left_after(YUANBAO_MAX, yuanbao, self.yuanbao, int(gains.items.get("yuanbao", 0)))
        self.coin = _left_after(COIN_MAX, coin, self.coin, int(gains.items.get("freeMazeMoney", 0)))


def _candidates(ctx: TaskContext, me: int, left: Gave) -> list[tuple[int, int, str]]:
    """按优先级排好的目标：推荐榜（元宝没满时）在前，其后是满足迷宫币条件的（迷宫币没满时）。"""
    seen: set[tuple[int, int]] = set()
    ordered: list[tuple[int, int, str]] = []

    def add(rows: list[Mapping[str, Any]], keep) -> None:
        for row in rows:
            t = target(row)
            if t and t[0] != me and (t[0], t[1]) not in seen and keep(row):
                seen.add((t[0], t[1]))
                ordered.append(t)

    recommended = _board(ctx, RECOMMEND_RANK, MAX_PAGES)
    if left.yuanbao:
        add(recommended, lambda row: True)
    if left.coin:
        add(recommended, pays_coin)
        add(_board(ctx, STANDARD_RANK, MAX_PAGES), pays_coin)
        add(_board(ctx, RANDOM_RANK, 1), pays_coin)
    return ordered


def challenge(ctx: TaskContext, uid: int, pos: int, name: str) -> tuple[tuple[dungeon.Gains, Any] | None, str]:
    """打一个。返回 ((奖励, 结算响应), 说明)：没开始打（地图读不到/不能通关）→ (None, 原因)；开始后结算失败抛 AllianceError。"""
    label = f"{name}({uid}_{pos})"
    try:
        shown = ctx.request(R_DATA, {"uid": uid, "pos": pos})
    except p.AllianceError as exc:
        return None, f"{label} 读取地图失败：{exc}"
    maze = shown.get("data")
    if not isinstance(maze, Mapping):
        return None, f"{label} 服务器没有返回地图数据"
    if why := route_ok(maze.get("data")):
        return None, f"{label} {why}，未挑战"
    try:
        ctx.request(R_BEGIN, {"uid": uid, "pos": pos})
    except p.AllianceError as exc:
        return None, f"{label} 开始失败：{exc}，未结算"
    gains = dungeon.Gains()
    try:
        response = ctx.request(R_END, END_BODY)
        gains.add(response, ctx.session.drain_pushes())
    except p.AllianceError as exc:
        raise p.AllianceError(f"已开始挑战 {label}，结算失败：{exc}；不重发结算") from exc
    return (gains, response), label


@task(ID, "造梦迷宫", reads={R_RANK, R_DATA}, writes={R_BEGIN, R_END})
def run(ctx: TaskContext) -> TaskResult:
    me = state.num(ctx.info.get("uid"))
    if me is None:
        return TaskResult(ID, BLOCKED, "无法确认当前角色 UID，未选择目标")
    left = Gave(ctx.info)
    if not left.valid:
        return TaskResult(ID, BLOCKED, "无法确认今日迷宫奖励计数，未挑战")
    if not left.coin:
        return TaskResult(ID, SKIPPED, "今日迷宫币已达上限")
    targets = _candidates(ctx, me, left)
    if not targets:
        return TaskResult(ID, SKIPPED, "榜里没有能拿奖励的他人迷宫，不强求")
    total, notes, skipped = dungeon.Gains(), [], 0
    done = dry = 0
    for uid, pos, name in targets:
        if ctx.stopped or done >= MAX_CHALLENGES or dry >= MAX_DRY or not left.coin:  # 迷宫币拿满 50 就停，不再为元宝继续打
            break
        try:
            outcome, label = challenge(ctx, uid, pos, name)
        except p.AllianceError as exc:
            notes.append(str(exc))
            break
        if outcome is None:
            skipped += 1
            ctx.log(f"造梦迷宫 跳过：{label}")
            continue
        gains, response = outcome
        done += 1
        dry = 0 if gains.items else dry + 1
        total.items.update(gains.items)
        left.take(gains, response)
        ctx.log(f"造梦迷宫 {done}：{label}" + (dungeon.gain_note(gains) or "（没掉奖励）"))
    if not done:
        return TaskResult(ID, BLOCKED, notes[0] if notes else f"没有挑战成功（跳过 {skipped} 个）")
    head = f"挑战 {done} 个" + (f"，跳过 {skipped} 个" if skipped else "")
    return TaskResult(ID, SUBMITTED, "；".join([head + (dungeon.gain_note(total) or "；没有掉落奖励"), *notes]))
