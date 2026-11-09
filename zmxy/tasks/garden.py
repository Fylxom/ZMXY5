"""联盟药园（定时页）：采摘、铲除自己药园里成熟的植物，再把今天的仙露浇给盟友。

对照客户端 ``modules/garden``（``GardenProxy`` / ``GardenSoilView``）：

* ``getGardenInfo {}`` → ``garden {uid, serverTime, protectEndTime, soil{地块号: {plantId, finishTime, pickNum, wateringNum…}}}``，时间都是毫秒。
* 采摘 ``pick``：成熟（``finishTime`` ≤ ``serverTime``）且 ``pickNum == 0``（客户端“一键采摘” ``allPicks``），间隔 2 秒。
* 铲除 ``uproot``：成熟且 ``pickNum > 0``（客户端“一键清除” ``clearDatas`` 不勾“含种子”），间隔 0.5 秒；没成熟的不铲。
  **铲掉以后那棵上还没成熟的果子（要用琼浆催的）就没了**，要催的请先在游戏里催完。
* 浇灌 ``water``：只能浇别人的（自己药园的菜单里没有这一项）。今日剩余 = ``Garden[1].wateringFreeNum`` − ``everyDaySetting.wateringFreeNum``；
  盟友来自 ``getLeagueInfo`` 的 ``users``，``getGardenSimpleInfo {uids}`` 里有没成熟植物的才进去看（``getFriendGardenInfo {uid}``）；
  开着能量罩的不能浇（“有能量罩保护无法浇灌”），每棵最多浇到 ``GardenPlant.wateringNum`` 次。

三个写请求的请求体都是客户端的 ``removeCdParam`` + ``targetId``：``{plantId, soilIndex, ticket, targetId}``。``ticket`` 是“立即成熟”的点券数，
只有 ``matureWithTicket`` 用得上（那条路由不声明，守卫也会拦）；这里一律填 0。客户端浇灌时这个字段不是 0，服务器认不认 0 **没在真机上验证**。
每一步都以响应里的新 ``garden`` 为准：采摘后 ``pickNum`` 没变、铲除后地块还在、浇灌后 ``wateringNum`` 没加，就停止不重发。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .. import daycut, dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from .rewards import with_idle_error

ID = "garden"
R_INFO = "scene.gardenHandler.getGardenInfo"
R_PICK = "scene.gardenHandler.pick"
R_UPROOT = "scene.gardenHandler.uproot"
R_WATER = "scene.gardenHandler.water"
R_LIST = "scene.gardenHandler.getGardenSimpleInfo"
R_FRIEND = "scene.gardenHandler.getFriendGardenInfo"
WATER_KEY = "wateringFreeNum"
MAX_VISITS = 30   # 一次最多进几个盟友的药园
MAX_REFUSED = 3   # 浇灌被拒绝几次就不再试


class _Halt(Exception):
    """服务器状态或等待结果对不上；记录已确认浇水次数后停止后续步骤。"""

    def __init__(self, message: str, *, watered: int = 0) -> None:
        super().__init__(message)
        self.watered = watered


def _wait(ctx: TaskContext, seconds: float, action: str) -> str | None:
    """停止或连接中断时不再发送下一步请求。"""
    if ctx.sleep(seconds):
        return None
    if ctx.stopped:
        return "已被用户停止"
    return with_idle_error(ctx, f"{action}后等待时连接中断")


def garden_of(response: Any) -> Mapping[str, Any] | None:
    g = response.get("garden") if isinstance(response, Mapping) else None
    return g if isinstance(g, Mapping) and state.num(g.get("serverTime")) is not None else None


def soils(garden: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """地块号 → 地块；没种东西时 soil 可能整个没有。"""
    raw = garden.get("soil")
    pairs = raw.items() if isinstance(raw, Mapping) else enumerate(raw) if isinstance(raw, list) else ()
    return {i: s for k, s in pairs if isinstance(s, Mapping) and (i := state.num(k)) is not None and state.num(s.get("plantId"))}


def ripe(garden: Mapping[str, Any], soil: Mapping[str, Any]) -> bool | None:
    finish = state.num(soil.get("finishTime"))
    return None if finish is None else finish <= state.num(garden["serverTime"])


def plant_name(soil: Mapping[str, Any]) -> str:
    return str((state.row("GardenPlant", soil.get("plantId")) or {}).get("name") or soil.get("plantId"))


def _body(garden: Mapping[str, Any], index: int, soil: Mapping[str, Any]) -> dict[str, Any]:
    return {"plantId": soil.get("plantId"), "soilIndex": index, "ticket": 0, "targetId": garden.get("uid")}


def _act(ctx: TaskContext, route: str, garden: Mapping[str, Any], index: int, soil: Mapping[str, Any], gains: dungeon.Gains | None = None) -> Mapping[str, Any]:
    """发一次采摘/铲除/浇灌，返回响应（里面必须带新的 garden）。服务器拒绝照常抛 AllianceError。"""
    response = ctx.request(route, _body(garden, index, soil))
    if gains is not None:
        gains.add(response, ctx.session.drain_pushes())
    if garden_of(response) is None:
        raise _Halt(f"{plant_name(soil)}：响应里没有药园数据，无法确认结果")
    return response


def _tend_ripe(ctx: TaskContext, garden: Mapping[str, Any], route: str, verb: str, pause: float, wanted: Callable[[Mapping[str, Any]], bool],
               problem: Callable[[Mapping[str, Any], int], str | None], done: list[str], gains: dungeon.Gains | None = None) -> Mapping[str, Any]:
    """按地块号处理成熟且 wanted(地块) 的植物，返回最新的药园；problem(新药园, 地块号) 给出结果对不上的说明。"""
    for index in sorted(soils(garden)):
        soil = soils(garden).get(index)
        if soil is None or not ripe(garden, soil) or not wanted(soil):
            continue
        if ctx.stopped:
            raise _Halt("已被用户停止")
        name = plant_name(soil)
        try:
            garden = garden_of(_act(ctx, route, garden, index, soil, gains))
        except p.AllianceError as exc:
            raise _Halt(f"{verb}{name}被拒绝：{exc}") from exc
        if issue := problem(garden, index):
            raise _Halt(f"{verb}{name}后{issue}")
        done.append(name)
        if reason := _wait(ctx, pause, f"{verb}{name}"):
            raise _Halt(reason)
    return garden


def tend(ctx: TaskContext, garden: Mapping[str, Any], gains: dungeon.Gains, picked: list[str], removed: list[str]) -> None:
    """采摘再铲除自己药园里成熟的；做成的植物名记进 picked / removed（中途停下也留着）。"""
    def not_picked(g: Mapping[str, Any], index: int) -> str | None:
        return None if (state.num(soils(g).get(index, {}).get("pickNum")) or 0) > 0 else "服务器没有记成已采摘"

    def still_there(g: Mapping[str, Any], index: int) -> str | None:
        return "地块上还有植物" if index in soils(g) else None

    garden = _tend_ripe(ctx, garden, R_PICK, "采摘", 2, lambda s: state.num(s.get("pickNum")) == 0, not_picked, picked, gains)
    _tend_ripe(ctx, garden, R_UPROOT, "铲除", 0.5, lambda s: (state.num(s.get("pickNum")) or 0) > 0, still_there, removed)


def water_left(ctx: TaskContext, info: Any = None) -> int | None:
    """今天还能浇几次；读不到 → None。"""
    limit = state.num((state.row("Garden", 1) or {}).get("wateringFreeNum"))
    used = state.used(ctx.info if info is None else info, WATER_KEY)
    if limit is None or used is None:
        return None
    return limit - used - (0 if info is not None else ctx.session.spent[(ID,)])


def members(ctx: TaskContext, me: Any) -> list[int] | None:
    """盟友的 uid（不含自己）；读不到 → None。"""
    league = state.league(ctx.info)
    lid = state.league_id(league) if league else None
    if not lid:
        return None
    users = (ctx.request(p.ROUTE_LEAGUE_INFO, {"leagueId": lid}).get("league") or {}).get("users")
    if isinstance(users, Mapping):
        users = list(users.values())
    if not isinstance(users, list):
        return None
    uids = [state.num(u.get("uid")) for u in users if isinstance(u, Mapping)]
    return [u for u in uids if u is not None and u != state.num(me)]


def growing(rows: Any, now: int) -> list[int]:
    """药园列表里有没成熟植物的盟友，最快成熟的排前面（times 每项是 [成熟时间, 成长总时间]）。"""
    out = []
    for r in rows if isinstance(rows, list) else ():
        uid = state.num(r.get("uid")) if isinstance(r, Mapping) else None
        ends = [state.num(t[0]) for t in r.get("times") or () if isinstance(t, list) and t] if uid is not None else []
        later = [e for e in ends if e is not None and e > now]
        if later:
            out.append((min(later), uid))
    return [uid for _, uid in sorted(out)]


class _Watering:
    """一次浇灌：left 今天还剩几次，done 浇成了几次，refused 被拒绝几次。"""

    def __init__(self, ctx: TaskContext, left: int) -> None:
        self.ctx, self.left, self.done, self.refused = ctx, left, 0, 0
        self.failed_reads: list[str] = []

    def soil(self, garden: Mapping[str, Any], index: int) -> Mapping[str, Any]:
        """把一块地浇到次数用完、浇满或被拒绝，返回最新的药园（下一块地要用）。"""
        ctx = self.ctx
        while self.left > 0 and not ctx.stopped:
            soil = soils(garden).get(index)
            cap = state.num((state.row("GardenPlant", soil.get("plantId")) or {}).get("wateringNum")) if soil else None
            before = state.num(soil.get("wateringNum", 0)) if soil else None
            if cap is None or before is None or before >= cap or ripe(garden, soil) is not False:
                return garden
            if why := daycut.reason(ctx.session):
                raise _Halt(why, watered=self.done)
            ctx.session.spent[(ID,)] += 1  # 先占用：请求发出后可能已经用掉，不退回
            try:
                response = _act(ctx, R_WATER, garden, index, soil)
            except p.AllianceError as exc:
                ctx.session.spent[(ID,)] -= 1  # 服务器明确拒绝 = 没用掉
                self.refused += 1
                ctx.log(f"浇灌{plant_name(soil)}被拒绝：{exc}")
                return garden
            except _Halt as exc:
                raise _Halt(str(exc), watered=self.done) from exc
            garden = garden_of(response)
            if (state.num(soils(garden).get(index, {}).get("wateringNum")) or 0) <= before:
                raise _Halt(f"浇灌{plant_name(soil)}后浇灌次数没有增加", watered=self.done)
            self.done += 1
            fresh = water_left(ctx, response) if state.daily(response) is not None else None
            # 客户端只覆盖回执中出现的每日字段；局部回执可能带有
            # everyDaySetting 却不含 wateringFreeNum，不能让默认 0 把剩余次数重置。
            # 每次已确认浇灌至少扣 1；服务端快照只可进一步减少剩余次数。
            self.left = self.left - 1 if fresh is None else max(0, min(self.left - 1, fresh))
            if reason := _wait(ctx, 1, f"浇灌{plant_name(soil)}"):
                raise _Halt(reason, watered=self.done)
        return garden

    def visit(self, uid: int) -> None:
        ctx = self.ctx
        try:
            garden = garden_of(ctx.request(R_FRIEND, {"uid": uid}))
        except p.AllianceError as exc:
            if ctx.stopped:
                return
            if getattr(ctx.session, "live", True) is False:
                raise _Halt(f"读取盟友药园时连接中断（{exc}），停止后续浇灌", watered=self.done) from exc
            self.failed_reads.append(f"{uid}（{exc}）")
            return
        if garden is None:
            self.failed_reads.append(f"{uid}（未返回有效药园数据）")
            return
        if (state.num(garden.get("protectEndTime")) or 0) > state.num(garden["serverTime"]):
            return  # 开着能量罩的不能浇
        for index in sorted(soils(garden)):
            garden = self.soil(garden, index)
            if self.refused >= MAX_REFUSED:
                return

    def finish(self) -> tuple[int, str]:
        done = self.done
        if self.ctx.stopped:
            return done, f"用户停止；已浇灌 {done} 次" if done else "用户停止，未浇灌"
        if self.failed_reads:
            preview = "、".join(self.failed_reads[:3])
            more = f"等 {len(self.failed_reads)} 个" if len(self.failed_reads) > 3 else ""
            raise _Halt(f"读取盟友药园失败{more}：{preview}；停止后续浇灌", watered=done)
        if done:
            return done, f"浇灌 {done} 次"
        return 0, "浇灌多次被拒绝，未浇灌" if self.refused else "盟友的药园里没有可浇灌的植物"


def water(ctx: TaskContext, me: Any, now: int) -> tuple[int, str]:
    """把今天剩下的仙露浇给盟友。返回 (浇了几次, 说明)。"""
    if ctx.stopped:
        return 0, "用户停止，未浇灌"
    left = water_left(ctx)
    if left is None:
        raise _Halt("读不到今日仙露次数，未浇灌")
    if left <= 0:
        return 0, "今日仙露已用完"
    try:
        uids = members(ctx, me)
    except p.AllianceError as exc:
        if ctx.stopped:
            return 0, "用户停止，未浇灌"
        raise _Halt(f"读不到联盟成员（{exc}），未浇灌") from exc
    if uids is None:
        raise _Halt("读不到联盟成员，未浇灌")
    if not uids:
        return 0, "联盟里没有其他成员"
    try:
        listed = ctx.request(R_LIST, {"uids": uids}).get("data")
    except p.AllianceError as exc:
        if ctx.stopped:
            return 0, "用户停止，未浇灌"
        raise _Halt(f"读不到盟友药园列表（{exc}），未浇灌") from exc
    if not isinstance(listed, list):
        raise _Halt("服务器未返回盟友药园列表，未浇灌")
    job = _Watering(ctx, left)
    for uid in growing(listed, now)[:MAX_VISITS]:
        if job.left <= 0 or job.refused >= MAX_REFUSED or ctx.stopped:
            break
        job.visit(uid)
    return job.finish()


@task(ID, "联盟药园", reads={R_INFO, R_LIST, R_FRIEND, p.ROUTE_LEAGUE_INFO}, writes={R_PICK, R_UPROOT, R_WATER}, group="周常")
def run(ctx: TaskContext) -> TaskResult:
    if ctx.stopped:
        return TaskResult(ID, SKIPPED, "用户停止，未处理药园")
    league = state.league(ctx.info)
    if league is None or not state.league_id(league):
        return TaskResult(ID, SKIPPED, "当前角色没有联盟")
    try:
        garden = garden_of(ctx.request(R_INFO, {}))
    except p.AllianceError as exc:
        return TaskResult(ID, BLOCKED, f"读不到药园：{exc}")
    if garden is None:
        return TaskResult(ID, BLOCKED, "药园响应里没有 garden / serverTime，未执行")
    gains, parts, stop, note = dungeon.Gains(), [], None, ""
    picked: list[str] = []
    removed: list[str] = []
    watered = 0
    try:
        tend(ctx, garden, gains, picked, removed)
        watered, note = water(ctx, garden.get("uid"), state.num(garden["serverTime"]))
    except _Halt as exc:
        stop = str(exc)
        watered = exc.watered
        if watered:
            note = f"已浇灌 {watered} 次"
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"药园操作结果未知，请登录游戏核对；{exc}") from exc
    if picked:
        parts.append("采摘 " + "、".join(picked) + (f"（{gains.text()}）" if gains.items else ""))
    if removed:
        parts.append("铲除 " + "、".join(removed))
    if note:
        parts.append(note)
    summary, did = "；".join(parts), picked or removed or watered
    if stop:
        status = BLOCKED if did or stop != "已被用户停止" else SKIPPED
        return TaskResult(ID, status, f"{summary + '；' if summary else ''}停止于 {stop}")
    if ctx.stopped:
        return TaskResult(ID, BLOCKED if did else SKIPPED, summary or "用户停止，未处理药园")
    if not (picked or removed):
        summary = "没有成熟的植物；" + summary
    return TaskResult(ID, SUBMITTED if did else SKIPPED, summary)
