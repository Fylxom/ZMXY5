"""灵宠采集：领取已完成的采集位（不加倍、不用点券），并按原配置重新派遣。

领取响应带 ``photoId`` 时输出获得的相片；每种材料（``Collect.photoId``）对应 6 张相片，集齐后在汇总里提示切换采集材料，
并带上 ``todo``：界面在这次执行的“结束”前另记一行“待处理”（照常重派，不暂停）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import dungeon, protocol as p, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "pet_collection"
ROUTE_COLLECT = "scene.petHandler.collect"
ROUTE_REWARD = "scene.petHandler.getCollectReward"
SLOTS = 3


def _slots(value: Any) -> Mapping[str, Any] | None:
    data = state.find(value, "collectData", "collect_data", depth=8)
    return data if isinstance(data, Mapping) else None


def _slot(data: Mapping[str, Any] | None, pos: int) -> Mapping[str, Any] | None:
    rec = data.get(str(pos), data.get(pos)) if data else None
    return rec if isinstance(rec, Mapping) else None


def _album(*sources: Any) -> dict[str, Any]:
    """已拥有的相片：先取登录数据里的 petAlbum，再用领取响应里更新的 petAlbum 覆盖。键是相片 id（字符串），另有 ``reward`` 键不算相片。"""
    out: dict[str, Any] = {}
    for src in sources:
        got = state.find(src, "petAlbum", depth=8)
        if isinstance(got, Mapping):
            out.update({str(k): v for k, v in got.items() if str(k).isdigit()})
    return out


def _photo_ids(collect_id: int | None) -> list[str]:
    row = state.row("Collect", collect_id) if collect_id else None
    return [x for x in str((row or {}).get("photoId") or "").split(";") if x.isdigit()]


def _photo_name(photo_id: Any) -> str:
    return str((state.row("CollectPhoto", photo_id) or {}).get("name") or f"相片{photo_id}")


class _Round:
    """一次灵宠采集：逐个采集位领取、重派，攒着备注和收获。"""

    def __init__(self, ctx: TaskContext, data: Mapping[str, Any], now: int) -> None:
        self.ctx, self.data, self.now = ctx, data, now
        self.notes: list[str] = []
        self.done = 0
        self.gains, self.claimed, self.photos = dungeon.Gains(), 0, 0  # 本次领取的材料、领取次数、获得的相片数
        self.album = _album(ctx.info)

    def full_notes(self) -> list[str]:
        out, seen = [], set()
        for pos in range(SLOTS):
            rec = _slot(self.data, pos)
            cid = state.num(rec.get("collectId")) if rec else None
            ids = _photo_ids(cid)
            if ids and cid not in seen and all(i in self.album for i in ids):
                seen.add(cid)
                name = (state.row("Collect", cid) or {}).get("name") or cid
                out.append(f"【{name}】的 {len(ids)} 张相片已集齐，请切换采集材料（采集位 {pos + 1}）")
        return out

    def summary(self) -> list[str]:
        """领取结果：采集获得材料×数量；获得 N 张相片 / 未获得相片。一次都没领取就不写。"""
        if not self.claimed:
            return []
        out = [f"采集获得{self.gains.text()}"] if self.gains.items else []
        out.append(f"获得 {self.photos} 张相片" if self.photos else "未获得相片")
        return out

    def finish(self, extra: str = "") -> TaskResult:
        full = self.full_notes()
        self.notes.extend(full)
        text = "；".join(self.summary() + self.notes + ([extra] if extra else []))
        if self.ctx.stopped:
            status = SUBMITTED if self.done or self.claimed else SKIPPED
        else:
            status = SUBMITTED if self.done else (BLOCKED if extra else SKIPPED)
        return TaskResult(ID, status, text or "当前没有可领取的采集位", todo="；".join(full))

    def slot(self, pos: int) -> str | None:
        """处理一个采集位：到时间的先领取，再原样重派。返回必须停止的原因；None = 继续下一个。"""
        rec = _slot(self.data, pos)
        if not rec:
            return None
        reward, cid, gear = state.num(rec.get("reward")), state.num(rec.get("collectId")), state.num(rec.get("gear"))
        pet, finish_at = str(rec.get("petEntityId") or ""), state.seconds(rec.get("finishTime"))
        if not cid or cid <= 0 or gear not in (0, 1, 2) or not pet or reward is None:
            return f"采集位 {pos + 1} 数据不完整，已停止"
        if reward == 0:
            if finish_at is None:
                return f"采集位 {pos + 1} 缺少结束时间，已停止"
            if finish_at > self.now:
                self.notes.append(f"采集位 {pos + 1} 仍在进行")
                return None
            stop = self.claim(pos, cid)
            if stop:
                return stop
            if self.ctx.stopped:
                return "已领取，但用户停止，未重派"
        if self.ctx.stopped:
            return "用户停止，未重派"
        return self.dispatch(pos, cid, pet, gear)

    def claim(self, pos: int, cid: int) -> str | None:
        """领取一个采集位；返回必须停止的原因。"""
        ctx = self.ctx
        try:
            resp = ctx.request(ROUTE_REWARD, {"pos": pos, "double": 0, "must": 0})
        except p.AllianceError as exc:
            return f"采集位 {pos + 1} 领取失败：{exc}"
        pushes = ctx.session.drain_pushes()
        self.claimed += 1
        self.gains.add(resp, pushes)
        self.album.update(_album(resp))
        photo = state.num(resp.get("photoId")) if isinstance(resp, Mapping) else None
        if photo and photo > 0:
            self.album.setdefault(str(photo), True)
            ids = _photo_ids(cid)
            self.photos += 1
            ctx.log(f"采集位 {pos + 1} 获得相片「{_photo_name(photo)}」（本材料 {sum(i in self.album for i in ids)}/{len(ids) or 6}）")
        updated = _slots(resp) or _slots(pushes)
        after = _slot(updated, pos)
        if not after or state.num(after.get("reward")) == 0:
            return f"采集位 {pos + 1} 领取响应未确认已领取，不重复领取或派遣"
        self.data = updated
        return None

    def dispatch(self, pos: int, cid: int, pet: str, gear: int) -> str | None:
        """同一只宠物、同一档位重新派出去；返回必须停止的原因。"""
        data = self.data
        busy = any(
            q != pos and (o := _slot(data, q)) and state.num(o.get("reward")) == 0 and str(o.get("petEntityId") or "") == pet
            for q in range(SLOTS)
        )
        if busy:
            self.done += 1
            self.notes.append(f"采集位 {pos + 1} 宠物被其他采集位占用，未重派")
            return None
        try:
            resp = self.ctx.request(ROUTE_COLLECT, {"collectId": cid, "entityId": pet, "gear": gear, "pos": pos})
        except p.AllianceError as exc:
            return f"采集位 {pos + 1} 重派失败：{exc}"
        updated = _slots(resp)
        after = _slot(updated, pos)
        if not after or state.num(after.get("reward")) != 0 or str(after.get("petEntityId") or "") != pet:
            return f"采集位 {pos + 1} 派遣响应未确认开始采集，已停止"
        self.data = updated
        self.done += 1
        return None


@task(ID, "灵宠采集", writes={ROUTE_COLLECT, ROUTE_REWARD})
def run(ctx: TaskContext) -> TaskResult:
    data, now = _slots(ctx.info), ctx.now
    if data is None:
        return TaskResult(ID, BLOCKED, "服务器没有返回 collectData，未领取或派遣")
    if now is None:
        return TaskResult(ID, BLOCKED, "没有取得服务器时间，无法判断采集是否完成")
    r = _Round(ctx, data, now)
    for pos in range(SLOTS):
        if ctx.stopped:
            return r.finish("已被用户停止")
        stop = r.slot(pos)
        if stop:
            return r.finish(stop)
    return r.finish()
