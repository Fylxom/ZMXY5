"""星斗幻境（周常页）：三层地图（MazeDeploy 三章）推进一轮，再免费领取每周灵玉宝箱。

协议全部来自客户端（xingdou/MazeDungeonManager、MazeDungeonGoToNodeCmd、MazeDungeonMapView、StarMazeMediator）：

* ``getMapData`` → ``data``：``map[行][列]`` = 下一行里可去的列号列表；``event[行][列]`` = ``[事件, 参数]``；
  ``pos = {x:行, y:列, status}``（``status==1`` 表示该节点已完成；没有 ``pos`` 就是还没开始，第 0 行任选）；
  ``chapter`` / ``chapterPassed``；``mazeItem`` 里 ``id=101`` 的是体力（客户端要求每步体力 ≥ 5）。
* 走一步：``goto {x,y}``，按返回的 ``event`` 处理：101 答题 → ``selectOption {index:1}``；201/202/203 战斗 → ``enterDungeon {}`` →
  ``finishDungeon {win:1,time:0}``；301 商店 → 只 ``getShopData``（**不购买**）；401 温泉，``goto`` 自己结算；500/501 宝箱 →
  ``getRewardList {}`` → ``chooseReward {index:0}``。
* 本章通关（``chapterPassed``）且不是最后一章 → ``nextChapter``；最后一章通关后 ``reset``（失败不致命，可能还在冷却），
  再按客户端公式 ``RewardMax − rewardMazeCnt`` 和库存 ``mazeReward[5270]`` 免费开宝箱 ``getFinishRewardFree {type:0, rewardId:5270}``。
  点券开启（``getFinishRewardWithTicket``）、商店购买、体力购买一律不碰。

只沿服务器给出的连线走，多个候选时取列号最小的；任何一步对不上（没有位置、没有下一步、未知事件）就停止，下次运行从服务器的当前位置继续。
一次执行最多推进 ``MAX_STEPS`` 步。每步按服务器结果为准；体力状态缺失或不足 5 时不发送 ``goto``，避免消耗状态不明。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from .. import data, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "star_maze"
R_MAP = "scene.mazeHandler.getMapData"
R_GOTO = "scene.mazeHandler.goto"
R_ENTER = "scene.mazeHandler.enterDungeon"
R_FINISH = "scene.mazeHandler.finishDungeon"
R_OPTION = "scene.mazeHandler.selectOption"
R_SHOP = "scene.mazeHandler.getShopData"
R_LIST = "scene.mazeHandler.getRewardList"
R_CHOOSE = "scene.mazeHandler.chooseReward"
R_NEXT = "scene.mazeHandler.nextChapter"
R_RESET = "scene.mazeHandler.reset"
R_BOX = "scene.mazeHandler.getFinishRewardFree"
JADE_BOX = 5270  # MazeDeploy.mazeReward 的第三个（灵玉宝箱）
STRENGTH = 101   # mazeProp 编号：体力
MIN_STRENGTH = 5
MAX_STEPS = 300
COOLDOWN_GRACE = 2.0  # 冷却结束后多等几秒
MAX_RUNS = 10  # 次数输入框的上限（一轮 = 三层通关再重置）
TEXT, BATTLES, SHOP, SPRING, CHESTS = 101, (201, 202, 203), 301, 401, (500, 501)


@dataclass(frozen=True)
class Maze:
    rows: list
    events: list
    pos: tuple[int, int, int] | None  # (x, y, status)
    chapter: int
    passed: bool
    strength: int | None


def parse(response: Mapping[str, Any]) -> Maze:
    body = response.get("data")
    if not isinstance(body, Mapping) or not isinstance(body.get("map"), list) or not isinstance(body.get("event"), list):
        raise ValueError("地图响应缺少 data.map / data.event")
    pos = None
    if isinstance(body.get("pos"), Mapping):
        x, y = state.num(body["pos"].get("x")), state.num(body["pos"].get("y"))
        if x is None or y is None:
            raise ValueError("pos 的 x/y 无效")
        pos = (x, y, state.num(body.get("status", body["pos"].get("status"))) or 0)
    chapter = state.num(body.get("chapter"))
    if chapter is None or chapter < 1:
        raise ValueError("响应缺少 chapter")
    items = body.get("mazeItem")
    strength = None
    if isinstance(items, (list, Mapping)):
        values = items.values() if isinstance(items, Mapping) else items
        strength = sum(state.num(i.get("count")) or 0 for i in values if isinstance(i, Mapping) and state.num(i.get("id")) == STRENGTH)
    return Maze(body["map"], body["event"], pos, chapter, body.get("chapterPassed") is True, strength)


def chapter_max() -> int:
    return len(data.table("MazeDeploy"))


def next_nodes(maze: Maze, x: int, y: int) -> list[tuple[int, int]]:
    """(x, y) 之后可去的节点：只认 map[x][y] 里的列号，且必须在下一行真实存在。"""
    try:
        links = maze.rows[x][y]
    except (IndexError, TypeError):
        raise ValueError(f"地图里没有节点 {x},{y}") from None
    if x + 1 >= len(maze.rows) or not isinstance(links, list):
        return []
    return sorted((x + 1, n) for n in {state.num(v) for v in links} if n is not None and 0 <= n < len(maze.rows[x + 1]))


def event_of(maze: Maze, x: int, y: int) -> int | None:
    """事件号；节点不是完整的 [事件, 参数] 就是 None。"""
    try:
        ev = maze.events[x][y]
        return state.num(ev[0]) if len(ev) >= 2 else None
    except (IndexError, TypeError, KeyError):
        return None


class Halt(Exception):
    pass


class Walker:
    def __init__(self, ctx: TaskContext) -> None:
        self.ctx, self.steps, self.counts = ctx, 0, {"战斗": 0, "答题": 0, "商店": 0, "宝箱": 0, "其他": 0}
        self.strength: int | None = None
        self.done_nodes: set[tuple[int, int, int]] = set()  # (章, 行, 列)：本次已处理过事件的节点
        self.reset_at: float | None = None  # time.monotonic()：到这个时刻才能重置（客户端 MazeDungeonManager.resetTime）

    def ask(self, route: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        response = self.ctx.request(route, body or {})
        self.track(response)
        self.track_cooldown(response)
        return response

    def track_cooldown(self, response: Mapping[str, Any]) -> None:
        """重置冷却 = MazeDeploy.resetTime 秒 + 地图 createTime − serverTime（毫秒，客户端 resetTime getter）。读不到就不等。"""
        body = response.get("data")
        if not isinstance(body, Mapping):
            return
        created, now = state.num(body.get("createTime")), state.num(body.get("serverTime"))
        chapter = state.num(body.get("chapter"))
        wait = state.num((data.row("MazeDeploy", chapter) or {}).get("resetTime")) if chapter else None
        if created is not None and now is not None and wait is not None:
            self.reset_at = time.monotonic() + max(0.0, (wait * 1000 + created - now) / 1000)

    def wait_reset(self) -> None:
        """重置前等冷却结束。被用户停止抛 Halt。"""
        left = 0.0 if self.reset_at is None else self.reset_at - time.monotonic()
        if left > 0:
            self.ctx.log(f"星斗幻境：重置冷却中，等待 {left:.0f} 秒")
            if not self.ctx.sleep(left + COOLDOWN_GRACE):
                raise Halt("已被用户停止（重置冷却中）")

    def track(self, response: Mapping[str, Any]) -> None:
        """跟踪体力：客户端 Give/consume 只看响应顶层的 give / consume 列表。"""
        if self.strength is None:
            return
        for key, sign in (("give", 1), ("consume", -1)):
            for item in response.get(key) or []:
                if isinstance(item, Mapping) and item.get("type") == "mazeProp" and state.num(item.get("subType")) == STRENGTH:
                    self.strength += sign * (state.num(item.get("count")) or 0)

    def read(self, route: str = R_MAP) -> Maze:
        try:
            maze = parse(self.ask(route))
        except (ValueError, p.AllianceError) as exc:
            raise Halt(f"读取地图失败：{exc}") from exc
        if maze.strength is not None:
            self.strength = maze.strength  # 以服务器为准
        return maze

    def resolve(self, maze: Maze, x: int, y: int, event: int | None, where: str) -> None:
        """处理节点事件（goto 之后，或上次中断留下的）。"""
        try:
            if event == TEXT:
                self.ask(R_OPTION, {"index": 1})
                self.counts["答题"] += 1
            elif event in BATTLES:
                self.ask(R_ENTER)
                self.ask(R_FINISH, {"win": 1, "time": 0})
                self.counts["战斗"] += 1
            elif event == SHOP:
                self.ask(R_SHOP)  # 只读商店，客户端关掉面板也没有请求
                self.counts["商店"] += 1
            elif event in CHESTS:
                listing = self.ask(R_LIST)
                if not isinstance(listing.get("rewardList"), list) or not listing["rewardList"]:
                    raise Halt(f"{where} 宝箱没有可选奖励，未领取")
                self.ask(R_CHOOSE, {"index": 0})
                self.counts["宝箱"] += 1
            else:  # 401 温泉由 goto 自己结算；0 / 未知事件客户端也没有后续请求
                self.counts["其他"] += 1
        except p.AllianceError as exc:
            raise Halt(f"{where} 事件 {event} 未完成：{exc}") from exc
        self.done_nodes.add((maze.chapter, x, y))

    def step(self, maze: Maze) -> Maze:
        """走一步并处理事件，返回最新地图。"""
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise Halt(f"一次推进超过 {MAX_STEPS} 步，已停止")
        if self.ctx.stopped:
            raise Halt("已被用户停止")
        if maze.pos is None:
            row0 = len(maze.rows[0]) if maze.rows else 0
            candidates = [(0, n) for n in range(row0)]
        else:
            x, y, status = maze.pos
            if status != 1 and (maze.chapter, x, y) not in self.done_nodes:  # 上次停在这个节点：先把它的事件做完
                self.resolve(maze, x, y, event_of(maze, x, y), f"第 {maze.chapter} 层节点 {x},{y}")
                return self.read()
            try:
                candidates = next_nodes(maze, x, y)
            except ValueError as exc:
                raise Halt(str(exc)) from exc
        if not candidates:
            raise Halt(f"第 {maze.chapter} 层当前位置没有下一步，且本层未通关（pos={maze.pos}）")
        x, y = candidates[0]
        where = f"第 {maze.chapter} 层节点 {x},{y}"
        if self.strength is None:
            raise Halt(f"读不到星斗幻境体力，停在 {where} 之前；不继续探险")
        if self.strength < MIN_STRENGTH:
            raise Halt(f"体力不足（{self.strength}，每步需要至少 {MIN_STRENGTH}），停在 {where} 之前；不购买体力")
        try:
            moved = self.ask(R_GOTO, {"x": x, "y": y})
        except p.AllianceError as exc:
            raise Halt(f"进入 {where} 失败：{exc}") from exc
        self.resolve(maze, x, y, state.num(moved.get("event")), where)
        return self.read()


def all_passed(maze: Maze) -> bool:
    return maze.passed and maze.chapter >= chapter_max()


def advance(w: Walker, maze: Maze) -> None:
    while not all_passed(maze):
        if maze.passed:  # 本章通关，进下一章（响应本身就是新的地图）
            before = maze.chapter
            try:
                maze = parse(w.ask(R_NEXT))
            except (ValueError, p.AllianceError) as exc:
                raise Halt(f"第 {before} 层通关后进入下一层失败：{exc}") from exc
            if maze.chapter != before + 1:
                raise Halt(f"进入下一层后服务器返回第 {maze.chapter} 层，预期第 {before + 1} 层")
            w.ctx.log(f"进入第 {maze.chapter} 层")
            continue
        maze = w.step(maze)


def auto_runs(info: Any) -> int | None:
    """界面“自动配置”用：本周灵玉宝箱免费开启次数还没用完 → 1 轮，用完 → 0；读不到返回 None。"""
    week = state.weekly(info)
    cap = state.num((data.row("MazeDeploy", chapter_max()) or {}).get("RewardMax"))
    used = state.num(week.get("rewardMazeCnt", 0)) if week is not None else None
    if cap is None or used is None or used < 0:
        return None
    return 1 if cap - used > 0 else 0


def weekly_boxes(ctx: TaskContext) -> tuple[int, str]:
    """免费开灵玉宝箱。返回 (开了几个, 说明)。读不到次数或库存就不开。"""
    if ctx.stopped:
        return 0, "用户停止，未开宝箱"
    try:
        info = ctx.refresh()
    except p.AllianceError as exc:
        return 0, f"刷新每周状态失败，未开宝箱：{exc}"
    week = state.weekly(info)
    stock_map = week.get("mazeReward") if week else None
    top = data.row("MazeDeploy", chapter_max()) or {}
    cap = state.num(top.get("RewardMax"))
    if week is None or cap is None or not (stock_map is None or isinstance(stock_map, Mapping)):
        return 0, "读不到每周宝箱状态（everyWeekSetting），未开宝箱"
    used = state.num(week.get("rewardMazeCnt", 0))
    stock = state.num((stock_map or {}).get(str(JADE_BOX), 0))
    if used is None or used < 0 or stock is None or stock < 0:
        return 0, "每周宝箱次数或库存无效，未开宝箱"
    free = min(cap - used, stock)
    if free <= 0:
        return 0, f"没有可开的灵玉宝箱（本周已开 {used}/{cap}，库存 {stock}）"
    gains, opened = dungeon.Gains(), 0
    for _ in range(free):
        if ctx.stopped:
            return opened, f"用户停止，已开 {opened} 个宝箱，未继续"
        try:
            response = ctx.request(R_BOX, {"type": 0, "rewardId": JADE_BOX})
        except p.AllianceError as exc:
            return opened, f"开第 {opened + 1} 个灵玉宝箱失败：{exc}"
        gains.add(response, ctx.session.drain_pushes())
        opened += 1
    return opened, f"免费开灵玉宝箱 ×{opened}" + dungeon.gain_note(gains)


def reset(w: Walker, notes: list[str]) -> Maze | None:
    """等冷却后重置，返回重置后的地图。失败不致命（可能还在冷却）：记进 notes，返回 None。"""
    w.wait_reset()
    try:
        return parse(w.ask(R_RESET))
    except (ValueError, p.AllianceError) as exc:
        notes.append(f"重置未成功（{exc}）")
        return None


def one_round(w: Walker, notes: list[str]) -> bool:
    """推进一轮三层并重置。返回是否三层通关；停在半路抛 Halt。"""
    w.steps = 0  # MAX_STEPS 按每一轮算；先清零，这一轮读地图就失败时不会把上一轮的步数再算一遍
    w.counts.update({k: 0 for k in w.counts})
    maze = w.read()
    if all_passed(maze):  # 上次通关后没来得及重置：先重置再打
        maze = reset(w, notes) or maze
        if all_passed(maze):
            return False
    advance(w, maze)
    if reset(w, notes):
        notes.append("已重置回第一层")
    return True


def walked(w: Walker) -> str:
    return "、".join(f"{k}{v}" for k, v in w.counts.items() if v)


@task(ID, "星斗幻境", reads={R_MAP, R_SHOP, R_LIST},
      writes={R_GOTO, R_ENTER, R_FINISH, R_OPTION, R_CHOOSE, R_NEXT, R_RESET, R_BOX}, group="周常", times_max=MAX_RUNS, auto=lambda ctx: auto_runs(ctx.info))
def run(ctx: TaskContext) -> TaskResult:
    times = dungeon.read_times(ctx, ID, MAX_RUNS)
    if isinstance(times, TaskResult):
        return times
    if times == 0:
        return TaskResult(ID, SKIPPED, "次数留空，不执行")
    w, notes, why, rounds, total_steps = Walker(ctx), [], None, 0, 0
    try:
        for _ in range(times):
            if not one_round(w, notes):
                break
            rounds += 1
            total_steps += w.steps
            notes.insert(len(notes) - 1, f"第 {rounds} 轮三层通关，共 {w.steps} 步（{walked(w)}）")
            ctx.log(f"星斗幻境第 {rounds}/{times} 轮通关")
    except Halt as exc:
        why = str(exc)
        total_steps += w.steps
        notes.append(f"已推进 {total_steps} 步" + (f"（{walked(w)}）" if walked(w) else ""))
    opened, box_note = weekly_boxes(ctx)  # 没走完也要开：之前几次攒下的宝箱仍然有效
    notes.append(box_note)
    detail = "；".join(notes)
    if why:
        return TaskResult(ID, BLOCKED, f"{detail}；{why}")
    if not total_steps and not opened:
        return TaskResult(ID, SKIPPED, detail)
    return TaskResult(ID, SUBMITTED, detail)
