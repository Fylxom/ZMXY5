"""通天河的另外两个周常页模块：报名雇佣、护送经书（客户端 TTRProxy）。

* 报名雇佣：``hireApply {}``，每天一次（``everyDaySetting.tongtianHireApply`` = 1 就是已报名）。只报名让别人雇我，**不雇别人**（雇人要消耗物品）。
* 护送经书：每天 ``tongtianRiverEscortNum``（3）条船，每天共 ``tongtianRiverEscortRefreshNum``（5）次免费刷新。
  每条船先 ``freshEscortGood {}`` 刷目标档次（刷到最高档 4 就停，不再浪费次数），再 ``escort {}`` 出发，
  等 ``escortData.endTime``（15 分钟）后 ``getReward {}`` 领经书，再出下一条。已经在运镖的船先等它结束并领奖。
  界面“选择”里给三条船各定一个刷新次数，默认 3,1,1（推荐）；总和超过今日剩余刷新次数的部分不会刷。
  付费的 ``freshEscortGoodWithTicket``、``finishNow``（立即完成）一律不碰；也不超过免费刷新次数去刷。
  发船后不空等：前置步骤发出第一条船，之后船到点的领奖、发下一条穿插在其他任务的请求之间（``Voyage``，挂在 ``Session.hook`` 上）；
  所有普通任务做完后的收尾把没跑完的等完。领奖存档不是发船存档时，领奖要断开重登，只能在收尾里领。
  最后一条船穿插着领完时，任务行当场显示最终结果（``Voyage.finish`` → ``ctx.report``），不等收尾。
  不白等：发完今天最后一条船，或者发完一条发现它回来时开放时段已经结束（发不了下一条），就不等它，直接结束，
  经书留给下次运行时领（``begin`` 会先领上次留下的船；用户确认隔天领不会清掉）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from .. import dungeon, api, protocol as p, state, timeplan
from ..core import BLOCKED, SKIPPED, SUBMITTED, Choices, Pick, TaskContext, TaskResult, UncertainOutcome, load_tasks, task
from . import tongtian

ID_HIRE = "tongtian_hire"
ID_ESCORT = "tongtian_escort"
R_APPLY = "scene.tongtianHandler.hireApply"
R_FRESH = "scene.tongtianHandler.freshEscortGood"
R_ESCORT = "scene.tongtianHandler.escort"
R_REWARD = "scene.tongtianHandler.getReward"
R_LIST = api.R_ESCORT_LIST  # 只用来取服务器时间
GOOD_NAMES = tongtian.SHIPS
BEST_GOOD = len(GOOD_NAMES)  # 目标档次 1–4（木舟/宝船/银帆/海舰），4 是最高
DEFAULT_REFRESH = (3, 1, 1)
REFRESH_KEYS = ("refresh1", "refresh2", "refresh3")
MAX_WAIT_SECONDS = 1200.0  # 一条船 15 分钟，多等 5 分钟余量
GRACE_SECONDS = 3.0


@task(ID_HIRE, "报名雇佣", writes={R_APPLY}, group="周常", account_wide=True)
def hire(ctx: TaskContext) -> TaskResult:
    if why := timeplan.closed(ID_HIRE, ctx.now):
        return TaskResult(ID_HIRE, SKIPPED, f"{why}，未报名")
    applied = state.used(ctx.info, "tongtianHireApply")
    if applied is None:
        return TaskResult(ID_HIRE, BLOCKED, "读不到今日报名状态（everyDaySetting），未报名")
    if applied >= 1:
        return TaskResult(ID_HIRE, SKIPPED, "今日已报名", day_done=True)
    try:
        ctx.request(R_APPLY, {})
    except p.AllianceError as exc:
        return TaskResult(ID_HIRE, BLOCKED, f"报名失败：{exc}")
    return TaskResult(ID_HIRE, SUBMITTED, "已报名雇佣", day_done=True)


def refresh_plan(options: Mapping[str, Any]) -> list[int]:
    plan = []
    for key, default in zip(REFRESH_KEYS, DEFAULT_REFRESH):
        n = state.num(options.get(key, default))
        plan.append(default if n is None or n < 0 else n)
    return plan


def escort_of(river: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = river.get("escortData")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) and raw else None


def river_of(response: Any, what: str) -> Mapping[str, Any]:
    river = state.section(response, "tongtianRiver")
    if river is None:
        raise dungeon.Stop(f"{what}的响应没有 tongtianRiver，无法确认结果；停止")
    return river


def good_name(good_id: Any) -> str:
    n = state.num(good_id)
    return f"{GOOD_NAMES[n - 1]}（档次 {n}）" if n is not None and 1 <= n <= len(GOOD_NAMES) else f"未知船型（goodId={good_id}）"


def refresh(ctx: TaskContext, river: Mapping[str, Any], budget: int) -> tuple[Mapping[str, Any], int]:
    """刷新目标档次，最多 budget 次，到最高档就停。返回（最新 tongtianRiver，实际刷新次数）。"""
    done = 0
    while done < budget and (state.num(river.get("goodId")) or 0) < BEST_GOOD and not ctx.stopped:
        river = river_of(ctx.request(R_FRESH, {}), "刷新目标")
        done += 1
        ctx.log(f"刷新 {done}/{budget}：{good_name(river.get('goodId'))}")
    return river, done


def read_end(ctx: TaskContext, river: Mapping[str, Any]) -> float:
    """这条船到点的时刻（time.monotonic 基准）。"""
    escort = escort_of(river)
    end = state.num(escort.get("endTime")) if escort else None
    now = state.num(api.escort_list(ctx.request).get("serverTime"))
    if end is None or now is None:
        raise dungeon.Stop("读不到这条船的结束时间或服务器时间，未领奖")
    wait = (end - now) / 1000
    if wait > MAX_WAIT_SECONDS:
        raise dungeon.Stop(f"这条船还要 {wait / 60:.0f} 分钟才结束，超过等待上限，未领奖")
    return time.monotonic() + wait


def claim(ctx: TaskContext, gains: dungeon.Gains) -> Mapping[str, Any]:
    """领当前这条（已到点的）船的经书。返回领奖后的 tongtianRiver。"""
    session = ctx.session
    record = state.num(ctx.options.get("claim_record"))
    elsewhere = record is not None and record != session.slot
    try:
        if elsewhere:  # 领奖存档不是发船存档：临时登录领奖存档领，领完回到发船存档
            with session.other_save(record) as other:
                response = other.request(R_REWARD, {})
                gains.add(response, other.drain_pushes())
            after = river_of(session.info, "领取经书") if state.section(session.info, "tongtianRiver") else None
            if after is None:
                raise dungeon.Stop(f"在存档 {record} 领取后，读不到发船存档的通天河数据；停止")
        else:
            response = ctx.request(R_REWARD, {})
            # 穿插在别的任务里时，推送里可能有那个任务的奖励，不能取走
            gains.add(response, [] if session.pumping else session.drain_pushes())
            after = river_of(response, "领取经书")
    except p.AllianceError as exc:
        raise dungeon.Stop(f"领取经书失败：{exc}") from exc
    if escort_of(after):
        raise dungeon.Stop("领取后这条船仍在运镖，没有领到；请登录游戏核对" + (f"（领奖存档 {record}）" if elsewhere else ""))
    return after


def start(ctx: TaskContext, river: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        after = river_of(ctx.request(R_ESCORT, {}), "出发护送")
    except p.AllianceError as exc:
        raise dungeon.Stop(f"出发护送失败：{exc}") from exc
    if not escort_of(after):
        raise dungeon.Stop("出发护送的响应里没有 escortData，无法确认；停止")
    return after


class Voyage:
    """一个连接上的护送进度。发船后不空等：挂在 ``Session.hook`` 上，其他任务每发一个请求（或 ``ctx.sleep``）时 ``tick`` 看一眼，
    船到点了就领经书、接着发下一条。所有普通任务做完后的收尾把没跑完的跑完。
    领奖存档不是发船存档时，领奖要断开重登，不能穿插在别的任务里：只在收尾里领。"""

    def __init__(self, ctx: TaskContext, river: Mapping[str, Any], trips: int, cap: int, refresh_cap: int, refreshed: int) -> None:
        self.ctx, self.session = ctx, ctx.session
        self.spec = load_tasks()[ID_ESCORT]
        self.river, self.trips, self.cap, self.refresh_cap, self.refreshed = river, trips, cap, refresh_cap, refreshed
        self.plan = refresh_plan(ctx.options)
        self.gains = dungeon.Gains()
        self.sailed = self.spent = 0
        self.end_at: float | None = None  # 当前这条船到点的时刻；None = 没有在跑的船
        self.why: str | None = None
        self.closed: str | None = None  # 开放时段结束、没发完的说明（不算失败）；发船后发现下一条来不及时，不等这条船，直接结束
        self.done = self.busy = self.deferred = False
        self.reported = False  # 最终结果已经作为任务结果交出去了
        record = state.num(ctx.options.get("claim_record"))
        self.elsewhere = record is not None and record != self.session.slot

    def run(self, step: Callable[[], None]) -> None:
        """执行一步。不嵌套（步骤里的请求又触发穿插点）；出错记成结束原因。"""
        if self.busy or self.done:
            return
        self.busy = True
        old, bag_full = self.session._spec, self.session.bag_full
        self.session.bind(self.spec)
        try:
            step()
        except dungeon.Stop as stop:
            self.finish(str(stop))
        except p.AllianceError as exc:
            self.finish(f"刷新目标失败：{exc}")
        except UncertainOutcome as exc:
            self.finish(show=False)  # 结果未知：不能显示成已完成
            raise UncertainOutcome(f"已出发 {self.sailed} 条船；{exc}") from exc
        finally:
            self.busy = False
            self.session.bind(old)
            self.session.bag_full = bag_full  # 领奖时背包满记在护送自己的结果里，不能算到当时正在跑的别的任务头上

    def finish(self, why: str | None = None, show: bool = True) -> None:
        self.why, self.done = why or self.why, True
        self.session.hook(ID_ESCORT, None)
        if show:
            self.ctx.report(self.result())  # 任务行马上显示最终状态，不等收尾

    def take(self) -> TaskResult | None:
        """还没交出的最终结果（交出后标记，不重复交）；还没跑完 = None。"""
        if not self.done or self.reported:
            return None
        self.reported = True
        return self.result()

    def launch(self) -> None:
        """出新的一条船：刷新目标档次再出发。"""
        ctx = self.ctx
        budget = min(self.plan[self.trips] if self.trips < len(self.plan) else 0, max(0, self.refresh_cap - self.refreshed))
        self.river, n = refresh(ctx, self.river, budget)
        self.refreshed, self.spent = self.refreshed + n, self.spent + n
        if ctx.stopped:
            raise dungeon.Stop("已被用户停止，未发船")
        self.river = start(ctx, self.river)
        self.trips, self.sailed = self.trips + 1, self.sailed + 1
        ctx.log(f"护送 {self.trips}/{self.cap} 已出发（刷新 {n} 次，船型：{good_name(self.river.get('goodId'))}）")
        self.end_at = read_end(ctx, self.river)
        if self.trips >= self.cap:  # 今天的船发完了：不等最后这条，经书下次运行时领（隔天也领得到）
            self.closed = "最后这条船的经书下次运行时领"
            self.finish()
        elif self.last_ship():  # 这条船回来时时段已经结束，发不了下一条：同样不等它
            self.closed = f"这条船回来时开放时段已结束，还有 {self.cap - self.trips} 条船没发；这条船的经书下次运行时领"
            self.finish()

    def last_ship(self) -> bool:
        """刚发的这条船到点时，开放时段是不是已经结束（读不到时段 = 当作结束）。"""
        escort = escort_of(self.river)
        end = state.num(escort.get("endTime")) if escort else None
        until = timeplan.open_until(ID_ESCORT, self.ctx.now)
        return end is None or until is None or end / 1000 >= until

    def advance(self) -> None:
        """领掉在跑的这条船（如果有），再出下一条；次数用完就结束。"""
        if self.end_at is not None:
            self.river, self.end_at = claim(self.ctx, self.gains), None
        if self.ctx.stopped:
            self.finish("已被用户停止")
        elif self.trips >= self.cap:
            self.finish()
        elif why := timeplan.closed(ID_ESCORT, self.ctx.now):  # 时段结束了（比如领的是上次留下的船）：剩下的不发（领奖不受时段限制）
            self.closed = f"{why}，还有 {self.cap - self.trips} 条船没发"
            self.finish()
        else:
            self.launch()

    def begin(self) -> None:
        if escort_of(self.river):  # 上次留下的船：先等它结束并领奖，才能出新的
            self.end_at = read_end(self.ctx, self.river)
        else:
            self.launch()

    def tick(self) -> None:
        """穿插点：船到点了就领奖、发下一条。"""
        if self.done or self.elsewhere or self.end_at is None or time.monotonic() < self.end_at or self.ctx.stopped:
            return
        self.run(self.advance)

    def drain(self, ctx: TaskContext) -> None:
        """把没跑完的跑完（收尾；这时已经没有别的任务可穿插了）。"""
        self.ctx = ctx
        while not self.done:
            ship = self.end_at
            wait = None if ship is None else ship - time.monotonic()
            if wait is not None and wait > 0:
                ctx.log(f"护送中，{wait / 60:.1f} 分钟后领奖")
                if not ctx.wait(wait + GRACE_SECONDS):  # 等的时候别的任务可以插进来做
                    self.finish("已被用户停止（这条船的经书还没领）")
                    break
                self.ctx = ctx  # 插进来的任务触发过穿插点的话，进度会显示到它那一行：换回来
                if self.end_at != ship:  # 等的时候插进来的任务已经把这条船领掉、发了下一条：重新等
                    continue
            self.run(self.advance)

    def result(self) -> TaskResult:
        note = f"护送 {self.sailed} 条船，刷新 {self.spent} 次" + dungeon.gain_note(self.gains) + (f"；{self.closed}" if self.closed else "")
        if self.why:
            return TaskResult(ID_ESCORT, BLOCKED, f"{note}；{self.why}" if self.sailed else self.why)
        return TaskResult(ID_ESCORT, SUBMITTED, note, day_done=self.trips >= self.cap)  # 船发满了（最后一条的经书下次运行时领，隔天也领得到）


def refresh_counts(acc: Mapping[str, Any]) -> list[tuple[int, str]]:
    return [(n, str(n)) for n in range((state.num(tongtian.common().get("tongtianRiverEscortRefreshNum")) or 5) + 1)]


def voyage(ctx: TaskContext) -> Voyage | TaskResult:
    """这个连接上的护送进度（第一次调用时读次数、发出第一条船）；读不到次数、今日已跑完则直接给结果。"""
    found = getattr(ctx.session, "_voyage", None)
    if found is not None:
        found.ctx = ctx  # 进度显示在自己的任务行
        return found
    common = tongtian.common()
    cap, refresh_cap = state.num(common.get("tongtianRiverEscortNum")), state.num(common.get("tongtianRiverEscortRefreshNum"))
    trips, refreshed = state.used(ctx.info, "tongtianRiverEscortNum"), state.used(ctx.info, "tongtianRiverEscortRefreshNum")
    river = state.section(ctx.info, "tongtianRiver")
    if not cap or not refresh_cap or trips is None or refreshed is None or river is None:
        return TaskResult(ID_ESCORT, BLOCKED, "读不到今日护送/刷新次数或通天河数据（进角信息），未护送")
    if not escort_of(river) and trips >= cap:
        return TaskResult(ID_ESCORT, SKIPPED, "今日护送次数已用完", day_done=True)
    if not escort_of(river) and (why := timeplan.closed(ID_ESCORT, ctx.now)):  # 有上次留下的船照样去领
        return TaskResult(ID_ESCORT, SKIPPED, f"{why}，未护送")
    found = ctx.session._voyage = Voyage(ctx, river, trips, cap, refresh_cap, refreshed)
    found.run(found.begin)
    if not found.done:
        ctx.session.hook(ID_ESCORT, found.tick)
    return found


def prelude(ctx: TaskContext) -> TaskResult | None:
    """本次所有任务之前：发出第一条船（或接上上次留下的船）；之后的领奖、发船穿插在其他任务里。"""
    found = voyage(ctx)
    if isinstance(found, TaskResult):
        return found
    found.deferred = True
    return found.take()  # 第一条船就没发出去：直接给结果，本体和收尾都不用再做


def finale(ctx: TaskContext) -> TaskResult | None:
    """所有普通任务做完后：等船、领奖、发完剩下的船，交出最终结果（已经交过就不再交）。"""
    found = voyage(ctx)
    if isinstance(found, TaskResult):
        return found
    found.drain(ctx)
    return found.take()


@task(ID_ESCORT, "护送经书", reads={R_LIST}, writes={R_FRESH, R_ESCORT, R_REWARD}, group="周常", account_wide=True,
      prelude=prelude, finale=finale, hosts=True,
      choices=Choices(None, "船", picks=tuple(Pick(key, f"第 {i} 条船", refresh_counts, default=d, blank=False)
                                  for i, (key, d) in enumerate(zip(REFRESH_KEYS, DEFAULT_REFRESH), start=1)),
                      pick_total=5, note="每条船出发前刷新几次档次，刷到最高档自动停；每天共 5 次免费，推荐 3 · 1 · 1。\n发船后其他任务照常做，船到点领奖再发下一条。"))
def escort(ctx: TaskContext) -> TaskResult:
    found = voyage(ctx)
    if isinstance(found, TaskResult):
        return found
    if found.deferred and not found.done:  # 经过前置步骤：船在后台跑，最终结果由收尾交出
        return TaskResult(ID_ESCORT, SKIPPED, f"已出发 {found.sailed} 条船，经书待领" if found.sailed else "有船在护送中，经书待领", pending=True)
    found.drain(ctx)  # 没经过前置步骤（单独运行）：全部跑完再返回；穿插着已经跑完的直接交结果
    return found.take() or found.result()
