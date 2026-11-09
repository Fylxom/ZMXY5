"""灵宠试炼：自动打完服务器当前周天里还没通关的关（和旧版一样固定一遍，没有次数设置）。

进度全部来自服务器 ``petDungeonSetting``（当前周天 ``layerId`` + ``dungeonHistory`` 里今日已通关的关）；
关卡表是 ``PetChallengDungeon`` / ``PetChallengeLayer``。每关：``beginChallengeDungeon {petDungeonId}`` →
创建副本（真正的战斗 mid 是表里的 dungeonId，不是 petDungeonId）→ 拾取 → 结算 → ``endChallengeDugeon {win:1}``，
结算后重新进角色确认这一关已记为通关才继续下一关。
6 关全通后只用**免费**重置（``resetNumber - everyDaySetting.petDungeonResetCnt``）；次数读不到或已用完就停，
绝不发会花 400 点券的重置。没有任何界面选项：周天取服务器当前周天，次数固定 1 遍。
"""

from __future__ import annotations

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, UncertainOutcome, task

ID = "pet_trial"
R_BEGIN = "scene.petHandler.beginChallengeDungeon"
R_END = "scene.petHandler.endChallengeDugeon"  # 服务器路由名就是这样拼的
R_RESET = "scene.petHandler.resetPetChallengeDungeon"
TIMES = 1  # 一遍：当前周天剩下的关；6 关今日已全通时用一次免费重置再打一遍（旧版固定 1）


def progress(info) -> tuple[int, list[dict], set[int]] | str:
    """(当前周天, 该周天 6 关的表记录, 今日已通关的 petDungeonId)；读不到返回原因。"""
    setting = state.section(info, "petDungeonSetting")
    layer_id = state.num(setting.get("layerId")) if setting else None
    history = setting.get("dungeonHistory") if setting else None
    if layer_id is None or layer_id <= 0 or history is None:
        return "服务器未返回 petDungeonSetting（周天或通关记录）"
    layer = state.row("PetChallengeLayer", layer_id)
    start, end = (state.num(layer.get(k)) for k in ("startId", "passId")) if layer else (None, None)
    configs = [state.row("PetChallengDungeon", i) for i in range(start, end + 1)] if start is not None and end is not None else []
    if len(configs) != 6 or any(c is None or state.num(c.get("layerId")) != layer_id or (state.num(c.get("dungeonId")) or 0) <= 0 for c in configs):
        return f"本地没有灵宠试炼第 {layer_id} 周天完整的 6 关配置"
    rows = history.items() if isinstance(history, dict) else [(r.get("id"), r) for r in history if isinstance(r, dict)]
    done = set()
    for key, child in rows:
        count = state.num(next((child[k] for k in ("dailyCnt", "dailyCount", "count") if isinstance(child, dict) and k in child), child))
        if (i := state.num(key)) and i > 0 and count and count > 0:
            done.add(i)
    return layer_id, [dict(c) for c in configs], done


class _Trial:
    """一次灵宠试炼：攒着通关数、收获和免费重置的状态，任何一步停下都用 ``result`` 出结果。"""

    def __init__(self, ctx: TaskContext) -> None:
        self.ctx = ctx
        self.stages, self.gains, self.label = 0, dungeon.Gains(), ""
        self.reset_submitted = self.reset_confirmed = False

    def result(self, status: str, why: str = "") -> TaskResult:
        text = f"{self.label}已通关 {self.stages} 关" if self.label else f"已通关 {self.stages} 关"
        if self.reset_submitted:
            text += "；已确认免费重置" if self.reset_confirmed else "；免费重置请求已发送，结果未确认"
        if self.gains.items:
            text += f"；获得 {self.gains.text()}"
        if status == SKIPPED and self.reset_submitted:
            status = SUBMITTED
        return TaskResult(ID, status if self.stages or self.reset_submitted or status != SUBMITTED else SKIPPED,
                          f"{text}；{why}" if why else text)

    def free_reset(self, layer_id: int) -> list[dict] | TaskResult:
        """6 关全通：只用免费重置回到起点。返回重置后要打的关；不能重置返回 TaskResult。"""
        ctx = self.ctx
        layer = state.row("PetChallengeLayer", layer_id)
        # EveryDaySetting.petDungeonResetCnt 默认 0，setData 仅覆盖已返回的键。
        # 容器缺失或显式值损坏仍为未知，不允许进入可能扣点券的重置分支。
        used = state.used(ctx.info, "petDungeonResetCnt")
        cap = state.num(layer.get("resetNumber"))
        if used is None or used < 0 or cap is None or cap < 0:
            return self.result(BLOCKED, "6 关已全通，但无法确认免费重置次数，未重置（避免花 400 点券）")
        if cap - used <= 0:
            return self.result(SUBMITTED if self.stages else SKIPPED, "6 关已全通，今日免费重置已用完")
        # 真正发送前重新进角，避免沿用之前任务留下的旧计数。
        try:
            fresh = progress(ctx.refresh())
        except p.AllianceError as exc:
            return self.result(BLOCKED, f"免费重置前刷新失败：{exc}，未重置")
        if (isinstance(fresh, str) or fresh[0] != layer_id
                or any(c["id"] not in fresh[2] for c in fresh[1])):
            return self.result(BLOCKED, "免费重置前周天或通关记录发生变化，未重置")
        used = state.used(ctx.info, "petDungeonResetCnt")
        if used is None:
            return self.result(BLOCKED, "免费重置前无法确认最新次数，未重置（避免花 400 点券）")
        if used >= cap:
            return self.result(SKIPPED, "6 关已全通，今日免费重置已用完")
        if ctx.stopped:
            return self.result(SUBMITTED if self.stages else SKIPPED, "用户停止，未重置")
        try:
            ctx.request(R_RESET, {"petDungeonId": state.num(layer.get("startId"))})
        except p.AllianceError as exc:
            return self.result(BLOCKED, f"免费重置失败：{exc}，未继续挑战")
        self.reset_submitted = True
        try:
            after = progress(ctx.refresh())
        except p.AllianceError as exc:
            return self.result(BLOCKED, f"免费重置请求已发送，但刷新角色状态失败：{exc}，未继续挑战")
        if isinstance(after, str) or all(c["id"] in after[2] for c in after[1]):
            return self.result(BLOCKED, "重置后没有确认回到起点，未继续挑战")
        self.reset_confirmed = True
        return [c for c in after[1] if c["id"] not in after[2]]

    def clear(self, config: dict) -> TaskResult | None:
        """打一关并确认记为通关；打不下去返回 TaskResult。"""
        ctx = self.ctx
        name, pet_id = config.get("name") or f"第 {config['id']} 关", config["id"]
        ctx.log(f"灵宠试炼 第 {TIMES}/{TIMES} 遍 · {name}")
        try:
            ctx.request(R_BEGIN, {"petDungeonId": pet_id})
            dungeon.play(ctx, dungeon.Level(config["dungeonId"], "pet", name, (config["dungeonId"],)), self.gains)
            ctx.request(R_END, {"win": 1})
            after = progress(ctx.refresh())
        except (dungeon.Stop, p.AllianceError) as exc:
            return self.result(BLOCKED, f"{name} 未完成：{exc}；未继续下一关")
        if isinstance(after, str) or pet_id not in after[2]:
            return self.result(BLOCKED, f"{name} 结算后没有确认通关，未继续下一关")
        self.stages += 1
        return None

    def play(self) -> TaskResult:
        now = progress(self.ctx.info)
        if isinstance(now, str):
            return self.result(BLOCKED, f"{now}，未发送挑战")
        layer_id, configs, done = now
        self.label = f"第 {layer_id} 周天"
        pending = [c for c in configs if c["id"] not in done]
        if not pending:
            pending = self.free_reset(layer_id)
            if isinstance(pending, TaskResult):
                return pending
        for config in pending:
            if self.ctx.stopped:
                return self.result(SUBMITTED, "已被用户停止")
            if stop := self.clear(config):
                return stop
        return self.result(SUBMITTED)


@task(ID, "灵宠试炼", writes={R_BEGIN, R_END, R_RESET} | dungeon.ROUTES[dungeon.QUICK])
def run(ctx: TaskContext) -> TaskResult:
    trial = _Trial(ctx)
    try:
        return trial.play()
    except UncertainOutcome as exc:
        prefix = f"灵宠试炼已通关 {trial.stages} 关"
        if trial.reset_submitted:
            prefix += "；免费重置请求已发送，结果未确认"
        raise UncertainOutcome(f"{prefix}；{exc}") from exc
