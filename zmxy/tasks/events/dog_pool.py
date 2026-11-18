"""狗卡轮转（双十一抽狗卡的多号模式）：选几个号一起抽，号与号之间互送狗卡，多拿“每日首次赠送”的那次抽卡，并把多余的卡集中给集卡号。

规则（客户端 modules/double11Dog、数据表 Double11Common / Double11Dog / Tips 521）：
* 每天免费抽 ``dailyDrawNum`` 次；当天第一次赠送再加 ``dailyPresentDrawNum`` 次（everyDaySetting.addDouble11LotteryCnt）。
* 某一种神汪要先激活（``activeDouble11Dog {id, type: 0}``，消耗 1 张）才能赠送（``double11Dog2Code {id, type: 1}``，消耗 1 张，返回兑换码）。
  2026-11-10 账号8 实测：没激活的种类赠送，服务器回“该位置未激活”，卡不扣。
* 兑换码由别的号兑换（``double11Code2Dog {cdkey}``）；同一个登录账号不能自己用，所以参加的号每个账号只选一个存档。
* 领奖期（rewardGetActivityId）里 ``bisectDouble11Reward {}`` 领瓜分的元宝（激活过、还没领）。

一轮（``run_round``）：同时登录全部参加的号 → 处理上次没走完的兑换码 → 各号用掉剩余抽卡次数、新种类一律激活 →
按下面的先后送卡（``next_gift``），每送一张重新找，直到没有可做的：
  1. 集卡号缺的种类：有这种多余卡的号送给它，它收到就激活；
  2. 今天还没送过卡、手里又没有可送的卡的号：送它一张它已激活过的种类，它就能送了（选还没送过的号里激活过的最多的种类，
     即常见卡，能接着往下传）；
  3. 今天还没送过卡、手里有可送的卡的号：送出去，按价值挑送法（见下）；
  4. 前面都没得做了：各号多余的卡汇总到集卡号（只汇总它激活过的种类：卡不消耗，第二天它还能拿去接力；它没激活过的种类留在原号，
     送过去它会激活掉、花掉价值）。
  多余卡 = 激活过的种类手里的每一张（一种只能激活一次，留着只能送人）。
  送过卡的号如果是当天首次赠送就补抽；补抽到的卡还能用在第 1、2、4 步（已经送过的号再送不加抽卡，不参加第 3 步）。
  价值（``card_values``）：每种卡 = 1/抽中概率；只算活动结束时集卡号手里、它能再转送的卡（别的号没激活的种类送不到集卡号，
  所以号自己抽到新种类就激活：那张卡本来到不了集卡号，激活了以后再抽到才送得过去）。送给激活过的号不花价值，优先；
  送给没激活的非集卡号会被激活掉（``worth_activation``）：成本 = 价值 ×（1 − 抽中概率）^收卡号剩余抽卡次数（它以后自己抽到这种，
  第一张本来也要激活，送过去只是提前；只有到结束都抽不到才真亏），好处 = 送卡号多抽一次、能送到集卡号的期望 = 它激活过的种类数。
  成本低于好处才送。最后一天剩余次数 0，成本就是整张卡。集卡号缺的不受限制（它每种都要激活一次，早激活早能收）。
  留卡过夜：抽卡期最后一天以前，送去激活会让哪个号第二天没有接力卡可接的，不送（``covered``）。
  第 2、4 步靠“号能兑换自己已激活的种类”：2026-11-12 实测可以。兑换被服务器拒绝的码记进 failed，提示用户。

各号游戏里“我的兑换码”（codeLog）中程序不认识的，每轮开始先收进账本当 made 处理（``adopt_own_codes``）。
  codeLog 里**有时还留着兑换掉的码**（2026-11-15 12:00 真机：前一天程序兑换过的两张还在，被收进来、兑换失败、整轮停了；
  2026-11-16 用户在客户端看“我的兑换码”已经是空的，所以不是一直留着，什么时候会留不清楚，猜是兑换时送卡号也在线）：所以兑换成功的码记进账本的
  "used"，源码运行时再加上留底文件里“已兑换”的码，这些不收；收进来的码兑换被拒只记 failed、接着往下做，不停整轮。
兑换码账本（设置目录的 dog_pool.json，"codes"）：每张码一条，状态
  sending（赠送请求发出、还没拿到码）→ made（拿到码，先存盘再核验扣卡）→ redeeming（兑换请求发出，记下收卡号兑换前的卡数）→ 删掉；
  failed = 兑换被拒；unknown = 结果说不清，交给用户到游戏里核对，不自动重发。
  下一轮先按状态处理：redeeming 的比对收卡号现在的卡数认定成没成，说不清的转 unknown，绝不重兑。
  账本读不了 / 格式坏了：整轮停下，原文件不动（不当成空账本覆盖）。
源码运行时另外把每个码的生成、兑换、失败追加进项目里的 ``狗卡/兑换码记录.txt``（只追加不删，留底查账；打包版不写）。

每个新的写操作（抽、激活、送、兑换）之前：检查用户停止、服务器日期没变、活动阶段没变（``halt``），并重读状态；
写完核对：抽卡已抽次数 +1、其余不变；激活 / 赠送 / 兑换后全部卡数和激活记录与预期完全一致。对不上就停，不重发。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from ... import data, protocol as p, state
from ...core import BLOCKED, SKIPPED, SUBMITTED, TaskResult, UncertainOutcome

R_ACTIVE = "scene.activityHandler.activeDouble11Dog"
R_TO_CODE = "scene.activityHandler.double11Dog2Code"
R_TO_DOG = "scene.activityHandler.double11Code2Dog"
R_MONEY = "scene.activityHandler.bisectDouble11Reward"
TAB = chr(9)  # 兑换码记录一行里各列用制表符隔开（贴进表格就分好列）
MAX_GIFTS = 200  # 一轮里最多送这么多次；到了上限算这一轮没做完，不记“今天做过”

_lock = threading.Lock()  # 一次只跑一轮（定时里几个参加的号同时开始时，只有第一个做）
_file_lock = threading.RLock()


class Bad(Exception):
    """服务器返回和预期对不上 / 账本出问题：停下这一轮，不重发。"""


class Stopped(Exception):
    """用户停止、跨过零点或活动阶段变了：在交易边界停下（已发出的请求核对记账后才停）。"""


# ---- 设置和记录
def key_of(user: str, slot: int) -> str:
    return f"{user}#{int(slot)}"


def members_of(pool: Any) -> list[dict[str, Any]]:
    """设置里的参加者 [{"user", "slot", "hub"}]；格式不对的丢掉，同一个账号只留第一个存档。集卡号（hub）最多一个。"""
    rows = pool.get("members") if isinstance(pool, Mapping) else None
    out, seen = [], set()
    for m in rows if isinstance(rows, list) else []:
        if not isinstance(m, Mapping):
            continue
        user, slot = m.get("user"), m.get("slot")
        if not isinstance(user, str) or not user or not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 8:
            continue
        if user in seen:
            continue
        seen.add(user)
        hub = m.get("hub") is True and not any(o["hub"] for o in out)
        out.append({"user": user, "slot": slot, "hub": hub})
    return out


def record_path() -> Path:
    from ...store import settings_path
    return settings_path().with_name("dog_pool.json")


def load_record(path: Path | None = None) -> dict[str, Any]:
    """读账本。文件不存在 = 空账本；读不了、格式坏了 → Bad（原文件不动，不能当成空账本再覆盖掉里面的兑换码）。"""
    target = path or record_path()
    try:
        got = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Bad(f"狗卡账本 {target.name} 读不了或格式坏了（{exc}），先不送卡；文件原样保留") from exc
    if not isinstance(got, dict) or not all(isinstance(got.get(k, []), list) for k in ("codes", "failed", "unknown", "used")) \
            or not all(isinstance(e, dict) for e in got.get("codes", [])):
        raise Bad(f"狗卡账本 {target.name} 格式不对，先不送卡；文件原样保留")
    return got


def peek_record(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """界面用：读不了时返回 ({}, 原因)，不抛。"""
    try:
        return load_record(path), ""
    except Bad as exc:
        return {}, str(exc)


def _save_record(record: Mapping[str, Any], path: Path | None = None) -> None:
    target = path or record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(target)


def update_record(change: Callable[[dict[str, Any]], Any], path: Path | None = None) -> dict[str, Any]:
    """读-改-写账本。写不进去 → Bad（调用方要停下，不能当成记上了）。"""
    with _file_lock:
        record = load_record(path)
        change(record)
        try:
            _save_record(record, path)
        except OSError as exc:
            raise Bad(f"狗卡账本写不进去（{exc}）") from exc
        return record


def code_log() -> Path | None:
    """源码运行时的兑换码留底文件；打包后的程序 = None。"""
    from ...tablesync import frozen
    return None if frozen() else Path(__file__).resolve().parents[3] / "狗卡" / "兑换码记录.txt"


def note_code(event: str, code: str, kind_name: str, giver: str, taker: str, error: str = "", path: Path | None = None) -> None:
    """追加一行：时间  事件  种类  送的号 → 收的号  兑换码（失败另带原因）。写不进去不影响送卡（账本才是依据）。"""
    target = path or code_log()
    if target is None:
        return
    line = TAB.join([datetime.now(state._CN).strftime("%Y-%m-%d %H:%M:%S"), event, kind_name, f"{giver} → {taker}", code] + ([error] if error else []))
    try:
        with _file_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            new = not target.exists()
            with target.open("a", encoding="utf-8") as fp:
                if new:
                    fp.write(TAB.join(["时间", "事件", "卡", "送的号 → 收的号", "兑换码", "失败原因"]) + chr(10))
                fp.write(line + chr(10))
    except OSError:
        pass


class Book:
    """一轮里用的账本：path（测试换成临时文件）、log_path（兑换码留底）。每张码的状态见模块说明。"""

    def __init__(self, path: Path | None = None, log_path: Path | None = None) -> None:
        self.path, self.log_path = path, log_path

    def load(self) -> dict[str, Any]:
        return load_record(self.path)

    def entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self.load().get("codes", [])]

    def add(self, entry: dict[str, Any]) -> None:
        update_record(lambda rec: rec.setdefault("codes", []).append(entry), self.path)

    def set(self, eid: str, **fields: Any) -> None:
        def change(rec: dict[str, Any]) -> None:
            for e in rec.get("codes", []):
                if e.get("id") == eid:
                    e.update(fields)
        update_record(change, self.path)

    def move(self, eid: str, to: str | None, **fields: Any) -> None:
        """从 codes 拿掉；to = "failed" / "unknown" 时连同 fields 移过去，None = 走完了（兑换成功）：只把码记进 used。"""
        def change(rec: dict[str, Any]) -> None:
            keep = []
            for e in rec.get("codes", []):
                if e.get("id") == eid:
                    if to:
                        rec.setdefault(to, []).append({**e, **fields})
                    elif isinstance(e.get("code"), str) and e["code"] not in rec.get("used", []):
                        rec.setdefault("used", []).append(e["code"])  # 游戏里“我的兑换码”还会列着它：记下来，下次不当成新码收
                else:
                    keep.append(e)
            rec["codes"] = keep
        update_record(change, self.path)

    def note(self, *args: Any, **kw: Any) -> None:
        note_code(*args, path=self.log_path, **kw)

    def redeemed(self) -> set[str]:
        """兑换成功过的码：账本的 used，加上留底文件里“已兑换”的（源码运行时才有）。"""
        rec = self.load()
        codes = {c for c in rec.get("used", []) if isinstance(c, str)}
        target = self.log_path if self.log_path is not None else code_log()
        try:
            lines = target.read_text(encoding="utf-8").splitlines() if target is not None and target.exists() else []
        except OSError:
            lines = []
        for line in lines:
            cols = line.split(TAB)
            if len(cols) >= 5 and cols[1].startswith("已兑换"):
                codes.add(cols[4])
        return codes


# ---- 一个参加的号
class Member:
    """一个参加的号（已登录的会话）。cards：各种卡的张数；act：已激活的种类；drawn / gave / got：这一轮的抽、送、收。"""

    def __init__(self, session: Any, user: str, slot: int, label: str, collect: bool, kinds: list[Mapping[str, Any]], present: int, base: int,
                 hub: bool = False) -> None:
        self.session, self.user, self.slot, self.label, self.collect, self.hub = session, user, slot, label, collect, hub
        self.ids = {r["consume_subType"]: r["id"] for r in kinds}
        self.names = {r["consume_subType"]: str(r.get("name") or r["consume_subType"]) for r in kinds}
        self.present, self.base = present, base
        self.cards: dict[str, int] = {}
        self.act: set[str] = set()
        self.reward_time = 0
        self.own_codes: list[tuple[str, str]] = []
        self.used = self.extra = 0
        self.drawn = self.gave = self.got = self.activated = 0
        self.halt: Callable[[], None] = lambda: None  # 每个新的写操作前调用（run_round 装上停止 / 跨日 / 阶段检查）

    @property
    def key(self) -> str:
        return key_of(self.user, self.slot)

    @property
    def gifted(self) -> bool:
        """今天送过卡了：每日首次赠送的那次抽卡已经加上，或者这一轮送过（服务器没加抽卡次数也不再当成“没送过”一直送）。"""
        return self.extra >= self.present or self.gave > 0

    def spare(self, kind: str) -> bool:
        return kind in self.act and self.cards.get(kind, 0) > 0

    def spares(self) -> list[str]:
        return [k for k in self.ids if self.spare(k)]

    def missing(self) -> list[str]:
        return [k for k in self.ids if k not in self.act]

    # 读
    def read(self) -> None:
        from .double11_dog import R_DATA  # 读数据和抽卡的路由归 double11_dog
        reply = self.session.request(R_DATA, {})
        box = reply.get("data") if isinstance(reply, Mapping) else None
        if not isinstance(box, Mapping):
            raise Bad(f"{self.label}：服务器没有返回狗卡数据")
        cards = {k: state.num(box.get(k, 0)) for k in self.ids}
        if any(n is None or n < 0 for n in cards.values()):
            raise Bad(f"{self.label}：狗卡数量异常")
        pos = box.get("dogPos") or {}
        if not isinstance(pos, Mapping):
            raise Bad(f"{self.label}：激活记录格式变化")
        by_id = {str(i): k for k, i in self.ids.items()}
        self.cards = cards
        self.act = {by_id[str(i)] for i, on in pos.items() if on and str(i) in by_id}
        self.reward_time = state.num(box.get("getPoolRewardTime")) or 0
        self.own_codes = []  # 游戏里“我的兑换码”：这个号生成了、还没人兑换的码（兑换掉的服务器会拿掉）
        for row in box.get("codeLog") or []:  # 每条 [?, Double11Dog.id, 兑换码]
            if isinstance(row, (list, tuple)) and len(row) > 2 and str(row[1]) in by_id and isinstance(row[2], str) and row[2]:
                self.own_codes.append((by_id[str(row[1])], row[2]))

    def read_counts(self) -> None:
        info = self.session.refresh()
        used, extra = state.used(info, "double11LotteryCnt"), state.used(info, "addDouble11LotteryCnt")
        if used is None or extra is None or used > self.base + extra:
            raise Bad(f"{self.label}：每日抽卡次数缺失或异常")
        self.used, self.extra = used, extra

    @property
    def left(self) -> int:
        return max(0, self.base + self.extra - self.used)

    # 写：先 halt、重读，写完核对全部卡数和激活记录
    def _check(self, want: dict[str, int], act: set[str], what: str) -> None:
        self.read()
        if self.cards != want or self.act != act:
            raise Bad(f"{self.label}：{what}后狗卡数量或激活记录对不上")

    def draw(self) -> str:
        from .double11_dog import R_DRAW, drawn_card
        self.halt()
        self.read()
        self.read_counts()
        if self.left <= 0:
            raise Bad(f"{self.label}：抽卡次数已经没了")
        before, act, used, extra = dict(self.cards), set(self.act), self.used, self.extra
        reply = self.session.request(R_DRAW, {})
        kind = drawn_card(reply, tuple(self.ids))
        self._check({**before, kind: before[kind] + 1}, act, "抽卡")
        self.read_counts()
        if self.used != used + 1 or self.extra != extra:
            raise Bad(f"{self.label}：抽卡后已抽次数没有正好 +1")
        self.drawn += 1
        return kind

    def activate(self, kind: str) -> None:
        self.halt()
        self.read()
        if kind in self.act or self.cards.get(kind, 0) <= 0:
            return
        before, act = dict(self.cards), set(self.act)
        self.session.request(R_ACTIVE, {"id": self.ids[kind], "type": 0})
        self._check({**before, kind: before[kind] - 1}, act | {kind}, f"激活{self.names[kind]}")
        self.activated += 1

    def to_code(self, kind: str, on_code: Callable[[str], None]) -> str:
        """赠送一张。拿到码先交给 on_code 存盘，再核对扣卡（核对断了码也已经记下）。调用前已 halt、重读。"""
        before, act = dict(self.cards), set(self.act)
        if not self.spare(kind):
            raise Bad(f"{self.label}：{self.names[kind]}没有可送的（没激活或没卡）")
        reply = self.session.request(R_TO_CODE, {"id": self.ids[kind], "type": 1})
        code = reply.get("cdkey") if isinstance(reply, Mapping) else None
        if not isinstance(code, str) or not code.strip():
            raise UncertainOutcome(f"{self.label}：赠送{self.names[kind]}没有拿到兑换码；请到游戏里“我的兑换码”核对")
        code = code.strip()
        on_code(code)
        self.gave += 1
        self._check({**before, kind: before[kind] - 1}, act, f"赠送{self.names[kind]}")
        return code

    def redeem(self, code: str, kind: str | None, before: Mapping[str, int]) -> str:
        """兑换一个码（调用前已 halt、重读，before = 兑换前的卡数）；返回兑换到的种类。核对：只有那一种 +1，其余全部不变。"""
        act = set(self.act)
        self.session.request(R_TO_DOG, {"cdkey": code})
        self.read()
        got = grew_by_one(before, self.cards)
        if self.act != act or got is None or (kind and got != kind):
            raise Bad(f"{self.label}：兑换后狗卡数量对不上")
        self.got += 1
        return got

    def use_draws(self, log) -> None:
        """用掉剩余的抽卡次数，新种类激活。"""
        self.read_counts()
        while self.left > 0:
            kind = self.draw()
            log(f"{self.label}：抽到{self.names[kind]}")
        self.activate_new(log)

    def activate_new(self, log) -> None:
        for kind in self.ids:
            if kind not in self.act and self.cards.get(kind, 0) > 0:
                self.activate(kind)
                log(f"{self.label}：激活{self.names[kind]}（{len(self.act)}/{len(self.ids)}）")

    def status(self) -> dict[str, Any]:
        return {"label": self.label, "act": len(self.act), "total": len(self.ids),
                "missing": [self.names[k] for k in self.missing()],
                "cards": {self.names[k]: n for k, n in self.cards.items() if n},
                "drawn": self.drawn, "gave": self.gave, "got": self.got, "gifted": self.gifted}


def grew_by_one(before: Mapping[str, int], after: Mapping[str, int]) -> str | None:
    """after 比 before 正好只有一种 +1、其余全部一样 → 那一种；否则 None。"""
    diff = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)}
    grew = [k for k, d in diff.items() if d]
    return grew[0] if len(grew) == 1 and diff[grew[0]] == 1 else None


# ---- 送卡的先后（只看 Member 的状态，不发请求；测试用假的 Member）
def covered(members: list[Member], take: tuple[Member, Member, str] | None = None) -> set[int]:
    """哪些号第二天有接力卡可接：它激活过的某一种是接力卡（至少 2 个号激活过、有号手里有多的）。返回号在 members 里的序号。
    take：假设先把这张送给没激活它的号（收的号激活掉，多余的少一张）。"""
    gone = take[2] if take else None
    act = [m.act | ({gone} if take and m is take[1] else set()) for m in members]
    relay = set()
    for kind in members[0].ids if members else ():
        spare = sum(m.cards.get(kind, 0) for m, a in zip(members, act) if kind in m.act) - (kind == gone)
        if spare > 0 and sum(kind in a for a in act) >= 2:
            relay.add(kind)
    return {i for i, a in enumerate(act) if a & relay}


def card_values() -> dict[str, float]:
    """每种卡的价值 = 1 / 抽中概率（Double11Common 的 probability 与 subType 一一对应）。对不上 = {}（不按价值限制）。
    价值 = 1/概率，所以某个号每抽一次、抽到某一种的期望价值都是 1。"""
    row = state.row("Double11Common", 1) or {}
    keys, weights = str(row.get("subType") or "").split(";"), [state.num(w) for w in str(row.get("probability") or "").split(";")]
    if len(keys) != len(weights) or any(not w or w <= 0 for w in weights):
        return {}
    total = sum(weights)
    return {k: total / w for k, w in zip(keys, weights)}


def future_draws(m: Any, days: int) -> int:
    """这个号到抽卡期结束还能抽几次（今天剩的 + 以后每天的免费次数；首次赠送多给的不算，保守）。"""
    return max(0, int(getattr(m, "left", 0) or 0)) + max(0, days) * int(getattr(m, "base", 1) or 1)


def activation_cost(kind: str, values: Mapping[str, float], draws: int) -> float:
    """把这种卡送给没激活它的号激活，集卡号最终少拿到的期望价值：只有收卡号到结束都抽不到这种时才真少一张。"""
    v = float(values.get(kind, 0.0))
    return v * (1.0 - 1.0 / v) ** draws if v > 1 else v


def worth_activation(g: Member, r: Member, kind: str, values: Mapping[str, float] | None, days: int) -> tuple[bool, float]:
    """g 把 kind 送给没激活它的非集卡号 r 激活值不值：(值不值, 成本)。好处 = g 多抽一次能送到集卡号的期望 = g 激活过的种类数。"""
    if not values:
        return True, 0.0
    cost = activation_cost(kind, values, future_draws(r, days))
    return cost < len(g.act), cost


def next_gift(members: list[Member], keep: bool = False,
              values: Mapping[str, float] | None = None, days: int = 0) -> tuple[Member, Member, str] | None:
    """下一张该送的卡：(送的号, 收的号, 种类)；没有可做的 = None。两个号必须是不同的登录账号。
    顺序：2. 救活 → 3. 首次赠送 → 1. 集卡号缺的 → 4. 汇总（集卡号缺的那种卡先在已激活的号之间接力，各号拿到加抽，
    当天最后再送集卡号激活，第 3 步不把它给别的号激活）。
    values：各种卡的价值（card_values）；第 3 步按“集卡号最终少拿多少”挑，送去激活值不值见 worth_activation。None = 不限制。
    keep：留接力卡过夜：送去激活会让某个号第二天没有接力卡可接的，不送（见 covered）。days：今天以后抽卡期还剩几天。"""
    def blocked(g: Member, r: Member, kind: str) -> bool:
        return keep and kind not in r.act and not covered(members) <= covered(members, (g, r, kind))

    def givers(kind: str, to: Member) -> list[Member]:
        found = [g for g in members if g.user != to.user and g.spare(kind)]
        return sorted(found, key=lambda g: (g.gifted, -g.cards.get(kind, 0)))  # 没送过的先送（顺带拿到它的那次抽卡）

    def to_hub() -> tuple[Member, Member, str] | None:  # 集卡号缺的
        for c in members:
            if c.collect:
                for kind in c.missing():
                    found = [g for g in givers(kind, c) if not blocked(g, c, kind)]
                    if found:
                        return found[0], c, kind
        return None

    hub = next((m for m in members if getattr(m, "hub", False)), None)
    hub_wants = {k for c in members if c.collect for k in c.missing()}
    for r in members:  # 2. 救活：送它一张它已激活的，它就有可送的了
        if not r.gifted and not r.spares():
            idle = [m for m in members if not m.gifted and not m.spares()]
            # 用能接着往下传的种类：还没送过卡的号里激活过它的越多越好（常见卡），稀有卡留给缺它的号激活
            for kind in sorted(r.act, key=lambda k: (-sum(k in m.act for m in idle), -sum(k in m.act for m in members),
                                                     -sum(g.cards.get(k, 0) for g in members))):
                found = givers(kind, r)  # 送的号没送过：这就是它今天的首次赠送；送过了：多余的卡本来也闲着
                if found:
                    return found[0], r, kind
    for g in members:  # 3. 自己能送的（今天的首次赠送）：选花掉价值最少的送法
        if not g.gifted and g.spares():
            options = []
            for r in members:
                if r.user == g.user:
                    continue
                for kind in g.spares():
                    if kind in r.act:
                        cost = 0.0  # 接力：卡还在，换个号拿着
                    elif kind in hub_wants:
                        continue  # 集卡号缺的：先接力让各号拿到加抽，最后再送集卡号激活（第 1 步），不给别的号激活
                    elif blocked(g, r, kind):
                        continue
                    elif r.collect:
                        cost = 0.0
                    else:
                        ok, cost = worth_activation(g, r, kind, values, days)
                        if not ok:  # 集卡号最终少拿的比多抽一次换回来的还多：不送，留着
                            continue
                    idle = not r.gifted and not r.spares()
                    options.append((cost, not idle, kind in r.act, -g.cards[kind], kind, r))
            if options:
                best = min(options, key=lambda o: o[:4])
                return g, best[5], best[4]
    step = to_hub()  # 1. 接力做完了，集卡号缺的再送它激活（还在汇总之前，汇总时这种也能收）
    if step:
        return step
    if hub is not None:  # 4. 前面都没得做了：各号多余的卡汇总到集卡号（只汇总它激活过的种类：卡还在，它以后也能送）
        for g in members:
            if g.user != hub.user:
                for kind in g.spares():
                    if kind in hub.act:
                        return g, hub, kind
    return None


def takers_for(kind: str, giver_user: str | None, members: list[Member]) -> list[Member]:
    """上一轮的码原定的收卡号这一轮没登录：改给集卡号（缺这种就激活，激活过就收着），再其次激活过这种的号（卡不消耗）。
    都没有 = 等下一轮。"""
    hub = [m for m in members if m.collect and m.user != giver_user]
    return hub + [m for m in members if not m.collect and m.user != giver_user and kind in m.act]


# ---- 一轮
class Round:
    """一轮的送卡、兑换和补账。"""

    def __init__(self, members: list[Member], log, book: Book, *, keep: bool = True, values: Mapping[str, float] | None = None,
                 days: int = 0, halt: Callable[[], None] = lambda: None) -> None:
        self.members, self.log, self.book, self.keep, self.values, self.days, self.halt = members, log, book, keep, values, days, halt
        self.notes: list[str] = []
        for m in members:
            m.halt = halt

    def _member(self, key: Any) -> Member | None:
        return next((m for m in self.members if m.key == key), None)

    def deliver(self, entry: dict[str, Any], to: Member, names: Mapping[str, str]) -> Member | None:
        """把一张已拿到的码交给 to 兑换。成功返回 to；服务器明确拒绝 → 记进 failed，程序自己送的码整轮停下（Bad），
        从游戏里“我的兑换码”收来的码（own）只返回 None 接着做（多半是早就兑换掉的旧码）。结果说不清的抛出去（账本里是 redeeming，下一轮比对卡数）。"""
        eid, code, kind = entry["id"], entry["code"], entry.get("kind")
        self.halt()
        to.read()
        before = dict(to.cards)
        self.book.set(eid, state="redeeming", to=to.key, before=before)
        try:
            got = to.redeem(code, kind if kind in to.ids else None, before)
        except p.AllianceError as exc:  # 服务器明确拒绝：码没用掉，留在 failed 里给用户看；意料之外，这一轮停下不再送
            self.book.move(eid, "failed", error=str(exc))
            self.book.note("兑换失败", code, names.get(kind, "?"), str(entry.get("from")), to.label, str(exc))
            if entry.get("own"):
                self.log(f"{to.label}：游戏里“我的兑换码”收来的一张{names.get(kind, '?')}兑换不了（{exc}），多半早就用过了，跳过")
                return None
            raise Bad(f"{to.label}：一张{names.get(kind, '?')}兑换失败（{exc}），码记在 dog_pool.json 的 failed；这一轮先停下")
        self.book.move(eid, None)
        self.book.note("已兑换", code, names[got], str(entry.get("from")), to.label)
        return to

    def gift(self, g: Member, r: Member, kind: str) -> None:
        """送一张卡：记意图 → 赠送拿码（先存盘再核对）→ 兑换 → 收卡号激活新种类 → 送卡号首次赠送就补抽。"""
        self.halt()
        g.read()
        r.read()
        first = not g.gifted
        extra = g.extra
        eid = uuid.uuid4().hex
        entry = {"id": eid, "state": "sending", "from": g.key, "to": r.key, "kind": kind, "at": int(time.time())}
        self.book.add(entry)

        def on_code(code: str) -> None:
            self.book.note("生成", code, g.names[kind], g.label, r.label)  # 先留底（不会失败），再进账本
            self.book.set(eid, state="made", code=code)
            entry.update(state="made", code=code)

        g.to_code(kind, on_code)
        got_by = self.deliver(entry, r, g.names)
        if got_by is not None:
            self.log(f"{g.label} 送 {got_by.label}：{g.names[kind]}")
            got_by.activate_new(self.log)
        if first:
            g.use_draws(self.log)
            if g.extra <= extra:
                self.log(f"{g.label}：今天首次赠送后服务器没有加抽卡次数")

    def adopt_own_codes(self) -> None:
        """各号“我的兑换码”里程序不认识的码（用户在游戏里手动点出来的）记进账本，接下来和上一轮留下的码一样先兑换：
        给集卡号，或激活过这种的号（卡不消耗，之后照常汇总回集卡号）。账本里已有的、失败 / 结果不明的不再收。"""
        rec = self.book.load()
        used = self.book.redeemed()
        for e in rec.get("codes") or []:  # 以前误收进来的、其实兑换过的码：直接拿掉
            if e.get("own") and e.get("state") == "made" and e.get("code") in used:
                self.book.move(e["id"], None)
        known = {e.get("code") for k in ("codes", "failed", "unknown") for e in rec.get(k) or [] if isinstance(e, Mapping)} | used
        for m in self.members:
            for kind, code in m.own_codes:
                if code in known:
                    continue
                known.add(code)
                self.book.add({"id": uuid.uuid4().hex, "state": "made", "code": code, "from": m.key, "to": None, "kind": kind,
                               "at": int(time.time()), "own": True})
                self.book.note("收下游戏里的码", code, m.names.get(kind, "?"), m.label, "")
                self.log(f"{m.label}：“我的兑换码”里有一张{m.names.get(kind, '?')}还没兑换，先拿来用")

    def settle_left(self) -> None:
        """上一轮没走完的码：按状态处理，绝不重发说不清的。"""
        names = self.members[0].names if self.members else {}
        self.adopt_own_codes()
        for entry in self.book.entries():
            eid, st, code, kind = entry.get("id"), entry.get("state"), entry.get("code"), entry.get("kind")
            if not eid:
                continue
            if st == "sending" or not isinstance(code, str):  # 赠送请求发出去了，不知道有没有生成码
                self.book.move(eid, "unknown", why="赠送结果不明")
                self.notes.append(f"上次 {entry.get('from')} 赠送一张{names.get(kind, '?')}结果不明，请到游戏里“我的兑换码”核对")
                continue
            if st == "redeeming":
                to = self._member(entry.get("to"))
                if to is None:
                    continue  # 收卡号这一轮没登录：下次再认
                to.read()
                before = entry.get("before") if isinstance(entry.get("before"), dict) else None
                if before is not None and grew_by_one(before, to.cards) == kind:
                    self.book.move(eid, None)
                    self.book.note("已兑换（上次确认）", code, names.get(kind, "?"), str(entry.get("from")), to.label)
                    continue
                if before is not None and to.cards == before:
                    self.book.set(eid, state="made")
                    st = "made"
                else:
                    self.book.move(eid, "unknown", why="兑换结果不明")
                    self.notes.append(f"上次 {to.label} 兑换一张{names.get(kind, '?')}结果不明，没有再兑换；请到游戏里核对")
                    continue
            target = self._member(entry.get("to")) if st == "made" else None
            if target is None:
                found = takers_for(kind, str(entry.get("from") or "").rpartition("#")[0], self.members) if kind in names else []
                target = found[0] if found else None
            if target is None:
                continue
            got_by = self.deliver(entry, target, names)
            if got_by is not None:
                self.log(f"{got_by.label}：兑换上次留下的码，得到{names.get(kind, '?')}")
                got_by.activate_new(self.log)

    def play(self) -> None:
        """抽卡、激活、送卡，直到没有可做的。到了 MAX_GIFTS 还有可做的 → Bad（这一轮不算做完）。"""
        for m in self.members:
            m.read()
        self.settle_left()
        for m in self.members:
            m.use_draws(self.log)
        def step() -> tuple[Member, Member, str] | None:
            return next_gift(self.members, self.keep, self.values, self.days)

        for _ in range(MAX_GIFTS):
            nxt = step()
            if nxt is None:
                return
            self.gift(*nxt)
        if step() is not None:
            raise Bad(f"一轮送了 {MAX_GIFTS} 张还没送完，先停下，下次接着送")


def play(members: list[Member], log, path: Path | None = None, log_path: Path | None = None, keep: bool = True,
         values: Mapping[str, float] | None = None, days: int = 0, halt: Callable[[], None] = lambda: None) -> list[str]:
    """抽卡、激活、送卡，直到没有可做的。返回要告诉用户的话。"""
    rnd = Round(members, log, Book(path, log_path), keep=keep, values=values, days=days, halt=halt)
    rnd.play()
    return rnd.notes


def claim(members: list[Member], log, halt: Callable[[], None] = lambda: None) -> list[str]:
    """领奖期：激活过、还没领的号各领一次瓜分的元宝。"""
    notes = []
    for m in members:
        halt()
        m.read()
        if not m.act or m.reward_time:
            continue
        m.session.request(R_MONEY, {})
        m.read()
        if not m.reward_time:
            raise Bad(f"{m.label}：领元宝后服务器没有记下领取时间")
        log(f"{m.label}：已领瓜分元宝（激活 {len(m.act)}/{len(m.ids)}）")
        notes.append(f"{m.label} 已领元宝")
    return notes


def days_left(now: Any) -> int:
    """今天以后抽卡期还剩几天（最后一天 = 0）。读不到结束时间按 0（最保守：不留卡、送去激活按整张卡算）。"""
    from .double11_dog import config
    end = state._activity_end(state.row("Activity", config().start) or {}, None)
    if end is None or not now:
        return 0
    return max(0, (end.date() - datetime.fromtimestamp(now, state._CN).date()).days)


def _day(now: Any) -> str | None:
    return datetime.fromtimestamp(now, state._CN).strftime("%Y-%m-%d") if now else None


def run_round(ctx, pool: Mapping[str, Any], labels: Mapping[str, str], *, reward: bool, manual: bool = False) -> TaskResult:
    """在当前存档上做一整轮：同时登录全部参加的号。manual：用户单独点了这一行的“执行”（今天做过也再做一轮）。"""
    from .double11_dog import ID, config, opened, reward_open
    members_cfg = members_of(pool)
    me = key_of(ctx.config.username, ctx.config.record_index + 1)
    if me not in {key_of(m["user"], m["slot"]) for m in members_cfg}:
        return TaskResult(ID, SKIPPED, "这个存档没参加狗卡轮转")
    if not _lock.acquire(blocking=False):
        return TaskResult(ID, SKIPPED, "另一个号正在做狗卡轮转")
    log, members, notes, status, complete = ctx.log, [], [], BLOCKED, False
    today = _day(ctx.now)
    try:
        if today is None:
            return TaskResult(ID, BLOCKED, "读不到服务器时间，不做狗卡轮转")
        try:
            record = load_record()
        except Bad as exc:
            return TaskResult(ID, BLOCKED, str(exc))
        if not manual and record.get("done") == today:  # 拿到锁以后再看：前一个号刚做完的也算
            return TaskResult(ID, SKIPPED, "今天狗卡轮转已经做过一轮（单独点“执行”可以再做一轮）")
        phase = reward_open if reward else opened

        def halt() -> None:
            if ctx.stopped or ctx.winding:
                raise Stopped("用户停止")
            if _day(ctx.now) != today:
                raise Stopped("跨过了零点")
            if not phase(ctx.now):
                raise Stopped("活动阶段变了")

        try:
            cfg = config()
            row = state.row("Double11Common", 1) or {}
            present = state.num(row.get("dailyPresentDrawNum"))
            if present is None or present < 0:
                raise Bad("缺少每日赠送加抽次数（dailyPresentDrawNum）")
            kinds = [r for r in state.table_rows("Double11Dog") if r.get("consume_subType") in cfg.keys]
            with ExitStack() as stack:
                skipped, uids = [], {}
                for m in members_cfg:
                    halt()
                    label = labels.get(key_of(m["user"], m["slot"]), key_of(m["user"], m["slot"]))
                    if key_of(m["user"], m["slot"]) == me:
                        session = ctx.session
                    else:
                        try:
                            session = stack.enter_context(ctx.open_peer(m["user"], m["slot"]))
                        except Exception as exc:  # 登录失败（密码、网络、等太久）：这个号这一轮不做，其余照常
                            skipped.append(f"{label}（{exc}）")
                            continue
                    uid = state.num((session.info or {}).get("uid")) if isinstance(session.info, Mapping) else None
                    if uid is not None and uid in uids:  # 两个登录名其实是同一个账号：不能互送
                        raise Bad(f"{label} 和 {uids[uid]} 是同一个账号")
                    if uid is not None:
                        uids[uid] = label
                    members.append(Member(session, m["user"], m["slot"], label, m["hub"], kinds, present, cfg.base, m["hub"]))
                if skipped:
                    notes.append("这一轮没登录上：" + "、".join(skipped))
                log(f"狗卡轮转：{len(members)} 个号已登录")
                if reward:
                    notes += claim(members, log, halt)
                else:
                    days = days_left(ctx.now)
                    notes += play(members, log, log_path=code_log(), keep=days > 0, values=card_values(), days=days, halt=halt)
                halt()  # 做完时还是同一天、同一阶段，才算今天这一轮做完
            status, complete = SUBMITTED, not skipped
        except Stopped as exc:
            notes.append(f"已停下：{exc}（已发出的请求都核对记账了，下次接着做）")
            status = SKIPPED
        except (Bad, ValueError, data.DataError, p.AllianceError) as exc:
            notes.append(f"已停止：{exc}；没走完的兑换码记在 dog_pool.json，下一轮先处理")
        finally:
            stats = {m.key: m.status() for m in members}
            try:  # 完成日期和各号进度在释放锁之前写（不会被别的轮次盖掉）
                update_record(lambda rec: rec.update(status={**rec.get("status", {}), **stats}, at=int(time.time()),
                                                     **({"done": today} if complete else {})))
            except Bad as exc:
                notes.append(str(exc))
                status = BLOCKED
    finally:
        _lock.release()
    drawn, gave = sum(m.drawn for m in members), sum(m.gave for m in members)
    summary = f"{len(members)} 个号：抽 {drawn} 张，送 {gave} 张"
    collectors = [m for m in members if m.collect]
    if collectors:
        summary += "；" + "；".join(f"{m.label} {len(m.act)}/{len(m.ids)}" + (f" 缺{'、'.join(m.names[k] for k in m.missing())}" if m.missing() else " 已集齐")
                                   for m in collectors)
    if reward:
        summary = f"{len(members)} 个号领瓜分元宝"
    return TaskResult(ID, status, "；".join([summary, *notes]))
