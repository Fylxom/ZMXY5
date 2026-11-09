"""玄女战记：有战记能量就打最新关卡，打完领取战记任务和星级奖励。

对照客户端 ``modules/chapterRole``（ChapterRoleModule / CRoleProxy / GetRewardCommand）、``CreateBattleServerCmd``、
``MissonCompleteCmd``、``EnterGatewayCmd``：

* 配置：``ChapterRoleHelper`` 第 1 行给活动 id（Activity 档期）、关卡顺序（``dungeon``）、每次消耗的战记能量（``energyConsume``）。
* 状态：进角信息的 ``chapter`` = ``{energy, role{level}, dungeon{关卡: {star…}}, starReward{已领的奖励 id}, levelType{已开通的奖励档}}``。
* 最新关卡：按顺序第一个还没有记录的关卡（前一关有记录就解锁，同 BossItem.setData）；战记角色等级不到它的 ``Dungeon.needLevel``
  时退回最后一个打得了的关卡。
* 打完：最后一关有了记录（= 全部打完）就不再挑战、不用战记水晶，只把还能领的奖励领掉。
* 结束：全部打完，而且本期的战记任务全部领过（status==3）、没有还能领的星级奖励，才算这个存档的玄女战记结束了；
  结果里带 ``profile[chapter_end]``，活动页据此把它放进“已结束”并关掉这个存档的开关（同云梯走到终点）。
* 挑战：``beginChapterDungeon {dungeonId}`` → ``endChapterDungeon {win:1, star, starInfo}``。不做本地战斗；星级按
  ``DungeonGrade`` 里这一关设了的条件全部算达成（通关 1、限时 2、血量 4、连击 8，star = 条件个数）。
  结算响应带回新的 ``chapter``，下一次用它；能量没有减少就停，不靠自己推算。
* 奖励：``ChapterRoleTask`` 里本期活动的任务不用接取，info.quest 里 status==2 的 ``commitQuest``（同 CRTaskMediator）；
  星级奖励 ``getStarReward {id}``：总星数够、所属的档已开通（``levelType``）、还没领过的（同 GetRewardCommand）。
* 战记水晶：能量不够打一次、背包里有战记水晶（``Prop`` 101054001，+100 战记能量）时用 1 个
  （``itemHandler.useItem {index:0, count:1, id}``，同客户端道具面板 → UseItemCmd），重新进角色确认能量涨了再接着打。
  只领不买：扫荡（要扫荡体力）、开通至尊奖励（点券）都不碰。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from .. import dungeon, api, protocol as p, quest, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task
from .rewards import record

ID = "chapter_role"
R_BEGIN = "scene.chapterRoleHandler.beginChapterDungeon"
R_END = "scene.chapterRoleHandler.endChapterDungeon"
R_STAR = "scene.chapterRoleHandler.getStarReward"
R_USE = api.R_USE_ITEM
END = "chapter_end"  # 存档 profile 里的标记：这个存档的玄女战记已经结束（打完最后一关、奖励领完）
CRYSTAL = 101054001  # 战记水晶（Prop 表 attributeV1=chapterEnergy / attributeV2=100）
PACE = 1.0  # 开始和结算之间的间隔（秒）
STAR_BITS = (("star_complete", 1), ("star_time", 2), ("star_hpPercent", 4), ("star_hitCombo", 8))  # DungeonGrade 条件 → starInfo 的位


def helper() -> Mapping[str, Any]:
    return state.row("ChapterRoleHelper", 1) or {}


def is_open(now: int | None) -> bool:
    return state.activity_open(helper().get("activityId"), now)


def levels() -> list[int]:
    return [int(x) for x in str(helper().get("dungeon") or "").split(";") if x.strip().isdigit()]


def _has(records: Any, key: Any) -> bool:
    return isinstance(records, Mapping) and str(key) in {str(k) for k in records}


def _name(mid: int) -> str:
    from ..dungeon_levels import display_name
    return display_name((state.row("Dungeon", mid) or {}).get("name") or mid)


def cleared(chapter: Any) -> bool | None:
    """是不是已经打完最后一关；读不到关卡记录 → None（不确定）。"""
    order, records = levels(), chapter.get("dungeon") if isinstance(chapter, Mapping) else None
    if not order or not isinstance(records, Mapping):
        return None
    return _has(records, order[-1])


def unclaimed(info) -> int | None:
    """本期活动里还没领过的战记任务个数（没完成的也算）；服务器没给任务列表 → None。"""
    status = quest.status(info)
    if status is None:
        return None
    aid = state.num(helper().get("activityId"))
    return sum(1 for r in state.table_rows("ChapterRoleTask")
               if state.num(r.get("activityId")) == aid and status.get(state.num(r.get("id"))) != quest.DONE)


def chapter_end(info) -> bool | None:
    """这个存档的玄女战记是不是结束了：打完最后一关，战记任务全部领过，也没有还能领的星级奖励。
    info 是登录信息；关卡、任务或星级奖励状态读不到 → None（不确定，不改存档里的标记）。"""
    chapter = state.section(info, "chapter")
    done, left = cleared(chapter), unclaimed(info)
    if done is None or left is None:
        return None
    if not done or left:
        return False
    stars = ready_stars(chapter)
    return None if stars is None else not stars


def crystal_gain() -> int | None:
    """一个战记水晶加多少战记能量；表里它不再是加战记能量的道具 → None（不用）。"""
    row = state.row("Prop", CRYSTAL) or {}
    gain = state.num(row.get("attributeV2"))
    return gain if row.get("attributeV1") == "chapterEnergy" and gain and gain > 0 else None


def latest(chapter: Mapping[str, Any]) -> tuple[int | None, str]:
    """（现在该打的关卡, 读不出来时的原因）。"""
    order, records = levels(), chapter.get("dungeon")
    role = chapter.get("role")
    level = state.num(role.get("level")) if isinstance(role, Mapping) else None
    if not order or not isinstance(records, Mapping) or level is None:
        return None, "读不到战记关卡记录或战记角色等级"
    at = next((i for i, mid in enumerate(order) if not _has(records, mid)), len(order) - 1)
    for mid in reversed(order[:at + 1]):
        need = state.num((state.row("Dungeon", mid) or {}).get("needLevel"))
        if need is None:
            return None, f"数据表里没有关卡 {mid} 的等级要求"
        if level >= need:
            return mid, ""
    return None, f"战记角色等级 {level} 不够打第一关"


def grade(mid: int) -> tuple[int, int] | None:
    """这一关满星时提交的（star, starInfo）；数据表里没有这一关 → None。"""
    row = state.row("DungeonGrade", mid)
    if not row:
        return None
    info = 0
    for key, bit in STAR_BITS:
        try:
            info |= bit if float(row.get(key) or 0) > 0 else 0
        except (TypeError, ValueError):
            return None
    return bin(info).count("1"), info


def total_stars(chapter: Mapping[str, Any]) -> int:
    records = chapter.get("dungeon")
    if not isinstance(records, Mapping):
        return 0
    by_id = {str(k): v for k, v in records.items()}
    return sum(state.num(r.get("star")) or 0 for mid in levels() if isinstance(r := by_id.get(str(mid)), Mapping))


def ready_tasks(info) -> dict[int, str] | None:
    """本期活动里可领取的战记任务 {任务 id: 说明}；服务器没给任务列表 → None。"""
    status = quest.status(info)
    if status is None:
        return None
    aid = state.num(helper().get("activityId"))
    rows = sorted(state.table_rows("ChapterRoleTask"), key=lambda r: state.num(r.get("order")) or 0)
    return {int(r["id"]): str(r.get("describe") or r.get("name") or r["id"]) for r in rows
            if state.num(r.get("activityId")) == aid and status.get(state.num(r.get("id"))) == quest.READY}


def ready_stars(chapter: Mapping[str, Any]) -> dict[int, str] | None:
    """可领取的星级奖励 {奖励 id: 说明}；已领或已开档位状态缺失时返回 None。"""
    got, owned = chapter.get("starReward"), chapter.get("levelType")
    if not isinstance(got, Mapping) or not isinstance(owned, Mapping):
        return None
    stars = total_stars(chapter)
    rows = sorted(state.table_rows("ChapterRoleReward"), key=lambda r: state.num(r.get("id")) or 0)
    return {int(r["id"]): f"{r.get('stars')} 星·{r.get('describe') or r['id']}" for r in rows
            if (state.num(r.get("stars")) or 0) <= stars and _has(owned, r.get("levelType")) and not _has(got, r.get("id"))}


class _Run:
    """玄女战记的一次执行：先打到能量用完或打完，再领任务和星级奖励。"""

    def __init__(self, ctx: TaskContext, cost: int) -> None:
        self.ctx, self.cost = ctx, cost
        self.gains, self.fought, self.claimed = dungeon.Gains(), Counter(), []
        self.refused: list[str] = []  # 领取被拒绝的：只记下来
        self.stop = self.low = ""
        self.end: bool | None = None  # 这个存档的玄女战记结束了没有（读不到 = None，不改存档里的标记）
        self.used = 0  # 用掉的战记水晶
        self.info: Any = None
        self.chapter: Mapping[str, Any] | None = None
        self.fresh = True  # info 是不是刚读的（背包里的水晶数以它为准）

    def summary(self) -> str:
        parts = [x for x in (
            f"使用战记水晶 ×{self.used}" if self.used else "",
            "挑战 " + "、".join(f"{n}×{c}" for n, c in self.fought.items()) if self.fought else "",
            f"已领取：{'、'.join(self.claimed)}" if self.claimed else "",
        ) if x]
        return "；".join(parts) + dungeon.gain_note(self.gains)

    def claim(self, route: str, body: dict, name: str) -> Mapping[str, Any] | None:
        self.ctx.log(f"领取「{name}」")
        try:
            response = self.ctx.request(route, body)
        except p.AllianceError as exc:
            self.refused.append(f"{name}({exc})")
            self.ctx.log(f"  领取被拒绝：{self.refused[-1]}")
            return None
        self.ctx.log(f"  已领取{record(self.ctx, self.gains, response)}")
        self.claimed.append(name)
        return response

    def use_crystal(self, energy: int) -> bool:
        """能量不够打一次：用 1 个战记水晶。返回能不能接着打。"""
        ctx = self.ctx
        self.low = f"战记能量不足（{energy}/{self.cost}）"
        have = state.bag_count(self.info if self.fresh else ctx.refresh(), CRYSTAL) if crystal_gain() else None
        if not have or have < 0:
            ctx.log(f"{self.low}，{'背包里没有战记水晶' if have == 0 else '读不到战记水晶数量，不使用'}")
            return False
        ctx.log(f"{self.low}，使用 1 个战记水晶（背包 ×{have}，每个 +{crystal_gain()}）")
        try:
            record(ctx, self.gains, api.use_item(ctx.request, CRYSTAL))
        except p.AllianceError as exc:
            self.stop = f"使用战记水晶被拒绝（{exc}）"
            return False
        self.used += 1
        self.info, self.fresh = ctx.refresh(), True
        self.chapter = state.section(self.info, "chapter")
        now = state.num(self.chapter.get("energy")) if self.chapter else None
        if now is None or now <= energy:
            self.stop = "用了战记水晶后战记能量没有增加，结果不确定，未继续"
            return False
        self.low = ""
        return True

    def fight(self, energy: int) -> bool:
        """打一次最新关卡。返回能不能接着打。"""
        ctx = self.ctx
        mid, why = latest(self.chapter)
        stars = grade(mid) if mid is not None else None
        if mid is None or stars is None:
            self.stop = why or f"数据表里没有关卡 {mid} 的星级条件，未挑战"
            return False
        name = _name(mid)
        ctx.log(f"战记能量 {energy}，挑战最新关卡「{name}」")
        try:
            ctx.request(R_BEGIN, {"dungeonId": mid})
        except p.AllianceError as exc:
            self.stop = f"开始「{name}」被拒绝（{exc}）"
            return False
        ctx.sleep(PACE)  # 已经开打：先结算完，再理会“停止”
        try:
            response = ctx.request(R_END, {"win": 1, "star": stars[0], "starInfo": stars[1]})
        except p.AllianceError as exc:
            self.stop = f"「{name}」结算被拒绝（{exc}），请登录游戏核对"
            return False
        self.fought[name] += 1
        self.fresh = False
        ctx.log(f"  已结算（{stars[0]} 星）{record(ctx, self.gains, response)}")
        self.chapter = state.section(response, "chapter") or state.section(ctx.refresh(), "chapter")
        left = state.num(self.chapter.get("energy")) if self.chapter else None
        if left is None or left >= energy:
            self.stop = "结算后战记能量没有减少，结果不确定，未继续挑战"
            return False
        return True

    def fight_all(self) -> None:
        ctx = self.ctx
        ctx.log("读取角色状态（战记能量、关卡记录）")
        self.info = ctx.refresh()
        self.chapter = state.section(self.info, "chapter")
        while not ctx.stopped:
            if self.chapter is None:
                self.stop = "服务器未返回玄女战记数据，未挑战"
                return
            if cleared(self.chapter):
                self.low = "最后一关已通关，不再挑战"
                return
            energy = state.num(self.chapter.get("energy"))
            if energy is None:
                self.stop = "读不到战记能量，未挑战"
                return
            if not (self.use_crystal(energy) if energy < self.cost else self.fight(energy)):
                return

    def claim_rewards(self) -> None:
        ctx = self.ctx
        ctx.log("读取角色状态（战记任务、星级奖励）")
        info = ctx.refresh()  # 任务进度、总星数以服务器为准
        ready = ready_tasks(info)
        if ready is None:
            self.stop = self.stop or "服务器未返回任务列表，无法领取战记任务"
        for qid, name in (ready or {}).items():
            if ctx.stopped:
                break
            quest.commit(lambda route, body: self.claim(route, body, name), qid)
        if ctx.stopped:
            return
        chapter = state.section(info, "chapter")
        stars = ready_stars(chapter) if chapter else None
        if stars is None:
            self.stop = self.stop or "星级奖励状态读取不全，无法确认可领取奖励"
        for rid, name in (stars or {}).items():
            if ctx.stopped:
                break
            if self.claim(R_STAR, {"id": rid}, name) is None:
                break  # 被拒绝说明条件和客户端脚本推出的不一样，后面的不再试
        if self.claimed and cleared(chapter):
            info = ctx.refresh()  # 打完了：领完以后再读一次，看是不是全部领过了
        self.end = chapter_end(info)
        if cleared(state.section(info, "chapter")):
            self.low = self.end_note(info)

    def end_note(self, info) -> str:
        if self.end:
            return "玄女战记已结束（最后一关已通关，奖励已领完）"
        if self.end is None:
            return "最后一关已通关，但结束所需的奖励状态读取不全，暂不确认结束"
        if left := unclaimed(info):
            return f"最后一关已通关，还有 {left} 个战记任务没领完，暂不算结束"
        return "最后一关已通关，还有未领取的星级奖励，暂不算结束"

    def result(self) -> TaskResult:
        text = "；".join(x for x in (self.summary(), self.stop, "已被用户停止" if self.ctx.stopped else "",
                                      f"领取被拒绝：{'、'.join(self.refused)}" if self.refused else "") if x)
        mark = {END: self.end} if self.end is not None else None
        if self.stop or self.refused:
            return TaskResult(ID, BLOCKED, text, profile=mark)
        if self.fought or self.claimed or self.used:
            note = f"；{self.low}" if self.low.startswith(("玄女战记已结束", "最后一关")) else ""
            return TaskResult(ID, SUBMITTED, text + note, profile=mark)
        return TaskResult(ID, SKIPPED, f"{self.low or '已停止'}，也没有可领取的战记奖励", profile=mark)


@task(ID, "玄女战记", writes={R_BEGIN, R_END, R_STAR, R_USE, quest.R_COMMIT}, group="活动", window=is_open)
def run(ctx: TaskContext) -> TaskResult:
    cost = state.num(helper().get("energyConsume"))
    if not levels() or not cost or cost < 0 or ctx.now is None:
        return TaskResult(ID, BLOCKED, "缺少活动配置或服务器时间，未发送请求")
    if not is_open(ctx.now):
        return TaskResult(ID, SKIPPED, "活动未开放")
    job = _Run(ctx, cost)
    try:
        job.fight_all()
        if not ctx.stopped:
            job.claim_rewards()
    except UncertainOutcome as exc:
        raise UncertainOutcome(f"{job.summary() or '尚未挑战'}；{exc}") from exc
    return job.result()
