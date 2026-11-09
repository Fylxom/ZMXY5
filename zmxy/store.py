"""本机设置：账号、任务选择。密码/二级密码/Cookie 用 Windows DPAPI 加密落盘。

存档编号：界面显示 1-8，协议 record_index 为 0-7，只在 ``to_account`` 里换算一次。
账号条目 = 一个 4399 登录账号；各存档的任务勾选/选项/不拾取/角色信息分开存在 ``acc["saves"]["1".."8"]``。
"""

from __future__ import annotations

import base64
from copy import deepcopy
import json
import os
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import protocol as p, servertime
from .core import Account
from .license_store import dpapi

# 游戏服务器按 IP 直连、使用自签名证书：不走证书链校验，用该域名作为 SNI，并固定证书指纹。
# 只作用于游戏 TLS 连接；4399 平台 HTTPS 仍走系统证书校验（见 auth4399._auth_openers）。
GAME_TLS_SERVER_NAME = "g2-zm5.4399zmxy.com"
# 游戏证书是 2017 年签发的自签名证书（已过期），无法用证书链校验，改为固定指纹。
GAME_TLS_PINS = ("3bb7714dd2bdc2ad55e6548aae8d401cb66e68d6c8e062f2693fc1f293a2d5c9",)

PREFIX = "dpapi:"
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKLY_WIDE = frozenset({"alliance"})  # 可以开“多存档”的账号级任务：每周几各用哪个存档（用户指定：联盟贡献）


def parse_clock(text: Any) -> int | None:
    """“时:分”（如 20:30，分钟写两位）→ 当天的第几分钟；写得不对 = None。"""
    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", str(text or "").strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return int(m[1]) * 60 + int(m[2])


def seal(text: str) -> str:
    return PREFIX + base64.b64encode(dpapi(text.encode(), protect=True)).decode() if text else ""


def unseal(text: str) -> str:
    """解不开（换了 Windows 用户）或不是密文时返回空串，**不**覆盖原值。"""
    if not text.startswith(PREFIX):
        return ""
    try:
        return dpapi(base64.b64decode(text[len(PREFIX):]), protect=False).decode()
    except (OSError, ValueError, AttributeError):  # AttributeError：不是 Windows，没有 DPAPI
        return ""


def settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "Zmxy5Next" / "settings.json"


def to_client_config(mapping: dict[str, Any]) -> p.ClientConfig:
    """游戏连接的统一配置：自签名证书 + 域名 SNI + 证书指纹固定。"""
    return replace(
        p.ClientConfig.from_mapping({**mapping, "tls_verify": False, "tls_server_name": GAME_TLS_SERVER_NAME}),
        tls_pins=GAME_TLS_PINS,
    )


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self.data: dict[str, Any] = {"accounts": []}
        self.load()

    def load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return
        try:
            loaded = json.loads(raw.decode("utf-8"))
        except ValueError:
            loaded = None
        if isinstance(loaded, dict) and isinstance(loaded.get("accounts"), list):
            self.data = loaded
            self._clear_pvp_times()
        elif raw.strip():  # 文件在但读不懂：下一次保存会用空设置盖掉它，先留一份，账号和密文还能找回来
            try:
                self.path.with_suffix(".bad").write_bytes(raw)
            except OSError:
                pass

    def save(self) -> None:
        self._clear_pvp_times()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # 原子替换，写一半断电不会损坏旧文件

    def _clear_pvp_times(self) -> None:
        """斗神 / 灵宠乱斗按每日目标自动结束，清理各账号各存档遗留的手动次数。"""
        for acc in self.accounts:
            saves = acc.get("saves") if isinstance(acc, dict) else None
            if not isinstance(saves, dict):
                continue
            for cfg in saves.values():
                options = cfg.get("options") if isinstance(cfg, dict) else None
                if not isinstance(options, dict):
                    continue
                for tid in ("pvp_match", "pet_brawl"):
                    opt = options.get(tid)
                    if isinstance(opt, dict):
                        opt.pop("times", None)

    @property
    def accounts(self) -> list[dict[str, Any]]:
        return self.data["accounts"]

    def set_secret(self, acc: dict[str, Any], field: str, plain: str) -> None:
        acc[field] = seal(plain)

    def get_secret(self, acc: dict[str, Any], field: str) -> str:
        return unseal(str(acc.get(field, "")))

    # ---- 存档：同一登录账号的各存档设置互不共用（账号级任务除外，见下）
    @staticmethod
    def save_cfg(acc: dict[str, Any], slot: int) -> dict[str, Any]:
        """该存档的设置 {"tasks", "options", "no_pickup", "profile"}；没有就建一份空的。"""
        return acc.setdefault("saves", {}).setdefault(str(slot), {})

    @staticmethod
    def save_view(acc: dict[str, Any], slot: int) -> dict[str, Any]:
        """只读取：没有就返回空字典，不创建。"""
        return (acc.get("saves") or {}).get(str(slot)) or {}

    @staticmethod
    def reset_save(acc: dict[str, Any], slot: int) -> None:
        """该存档的勾选、选项、不拾取、断点回到默认，读取到的角色信息（profile：羽毛、时光之舟、幻兽等）也清掉（用户要求）。账号级任务设置不动。"""
        Store.save_view(acc, slot).clear()

    def reset_account(self, acc: dict[str, Any]) -> None:
        """整个账号回到默认：它的每个存档（同 reset_save，读取到的角色信息也清掉）+ 账号级任务的设置（开关、执行存档、选项）。登录信息和备注名不动。"""
        for key in list(acc.get("saves") or {}):
            self.reset_save(acc, int(key))
        (self.data.get("wide") or {}).pop(acc.get("username", ""), None)
        profiles = self.data.get("account_profiles")
        if isinstance(profiles, dict):
            profiles.pop(acc.get("username", ""), None)

    def account_profile(self, username: str) -> dict[str, Any]:
        """登录账号级状态；同一账号的多个存档共用，存档重置不会清掉。"""
        profiles = self.data.setdefault("account_profiles", {})
        if not isinstance(profiles, dict):
            profiles = self.data["account_profiles"] = {}
        profile = profiles.setdefault(username, {})
        if not isinstance(profile, dict):
            profile = profiles[username] = {}
        return profile

    @staticmethod
    def active_slots(acc: dict[str, Any]) -> list[int]:
        """勾选了任务的存档编号（升序）。"""
        return sorted(int(k) for k, v in (acc.get("saves") or {}).items() if v.get("tasks"))

    # ---- 断点：暂停（如背包满）时，每个存档各记自己还没做完的任务；点“继续”只跑这些
    @staticmethod
    def label(acc: dict[str, Any]) -> str:
        """界面和日志里显示的账号名：备注名，没有才退回 4399 账号。"""
        return acc.get("name") or acc.get("username", "?")

    @staticmethod
    def slot_name(label: str, slot: int) -> str:
        """普通执行项的名字（日志里的账号列；界面靠它找回是哪个存档）。"""
        return f"{label} · 存档 {slot}"

    @staticmethod
    def wide_name(label: str, record: int) -> str:
        return f"{label} · 账号级 · 存档 {record}"

    def _saves_of(self, only: tuple[dict[str, Any], int] | None) -> list[Any]:
        return [self.save_view(*only)] if only else [sv for acc in self.accounts for sv in (acc.get("saves") or {}).values()]

    def switch_off(self, task_ids: Iterable[str], only: tuple[dict[str, Any], int] | None = None) -> None:
        """把这些任务的开关关掉（活动页：活动已结束）；只改这一次，不锁定。
        默认所有账号、所有存档；only=(账号, 存档) 只关这一个存档（云梯走到终点是按存档算的）。"""
        off = set(task_ids)
        for sv in self._saves_of(only):
            if isinstance(sv, dict) and off.intersection(sv.get("tasks", ())):
                sv["tasks"] = [t for t in sv["tasks"] if t not in off]

    def switch_on(self, task_ids: Iterable[str], only: tuple[dict[str, Any], int] | None = None) -> None:
        """打开这些任务的开关；默认所有账号、所有存档，only 可限制到一个存档。"""
        on = list(dict.fromkeys(task_ids))
        for sv in self._saves_of(only):
            if not isinstance(sv, dict) or not isinstance(sv.get("tasks"), (list, tuple)):
                continue
            tasks = list(sv["tasks"])
            for tid in on:
                if tid not in tasks:
                    tasks.append(tid)
            sv["tasks"] = tasks

    def add_resume(self, acc: dict[str, Any], slot: int, tasks: list[str]) -> None:
        cfg = self.save_cfg(acc, slot)
        cfg["resume"] = list(dict.fromkeys([*cfg.get("resume", ()), *tasks]))

    def add_progress(self, acc: dict[str, Any], slot: int, task: str, progress: dict[str, int]) -> None:
        self.save_cfg(acc, slot).setdefault("resume_done", {})[task] = dict(progress)

    def clear_resumes(self) -> None:
        for acc in self.accounts:
            for sv in (acc.get("saves") or {}).values():
                sv.pop("resume", None)
                sv.pop("resume_done", None)

    def has_resume(self) -> bool:
        return any(sv.get("resume") for acc in self.accounts for sv in (acc.get("saves") or {}).values())

    def resume_accounts(self, wide: dict[str, bool],
                        saver: Callable[[dict[str, Any]], Callable[[str], None]]) -> list[Account]:
        """按各存档的断点生成执行项（账号级任务排在普通存档任务之前）。"""
        normal: list[Account] = []
        wide_items: list[Account] = []
        for acc in self.accounts:
            for key, sv in sorted((acc.get("saves") or {}).items(), key=lambda kv: int(kv[0])):
                left = sv.get("resume") or []
                if not left:
                    continue
                base = self.to_account(acc, saver(acc), int(key))
                done_of = sv.get("resume_done") or {}  # 暂停前已打完的次数：继续时从设置的次数里扣掉
                for tid, done in done_of.items():
                    base = replace(base, options={**base.options, tid: {**base.options.get(tid, {}), "resume_done": done}})
                if plain := [t for t in left if t not in wide]:
                    normal.append(replace(base, tasks=tuple(plain)))
                if account_wide := [t for t in left if t in wide]:
                    wide_items.append(self._wide_resume(acc, int(key), base, account_wide, done_of))
        return wide_items + normal

    def _wide_resume(self, acc: dict[str, Any], record: int, base: Account, tids: list[str], done_of: dict[str, Any]) -> Account:
        options = self.wide_options(acc.get("username", ""), tids)
        for tid in tids:
            if done := done_of.get(tid):
                options[tid] = {**options.get(tid, {}), "resume_done": done}
        return replace(base, name=self.wide_name(self.label(acc), record), tasks=tuple(tids), options=options, tidy=None)

    def run_accounts(self, acc: dict[str, Any], on_cookie: Callable[[str], None] | None = None,
                     slots: list[int] | None = None) -> list[Account]:
        """每个要执行的存档生成一个执行项（slots 缺省 = 勾选了任务的存档）。"""
        return [self.to_account(acc, on_cookie, slot) for slot in (self.active_slots(acc) if slots is None else slots)]

    # ---- 账号级任务（联盟贡献、VIP 礼包、师徒授课/学艺）：按登录账号保存，同账号各存档共用（用户明确要求），存档独立选择
    def wide_cfg(self, username: str, task_id: str, default_on: bool = False) -> dict[str, Any]:
        """{"on": 是否执行, "record": 存档 1-8}（多存档另有 "multi"、"week"，见 wide_record）。没有就按默认值建一份。"""
        return self.data.setdefault("wide", {}).setdefault(username, {}).setdefault(
            task_id, {"on": default_on, "record": 1})

    @staticmethod
    def wide_record(cfg: dict[str, Any], weekday: int | None = None) -> int:
        """账号级任务今天在哪个存档执行：开了“多存档”就按星期几取 ``cfg["week"]``（周一…周日各一个 1-8），否则 ``cfg["record"]``。
        weekday：0 = 周一；缺省 = 服务器时间的今天。"""
        week = cfg.get("week")
        if cfg.get("multi") and isinstance(week, list) and len(week) == 7:
            day = servertime.clock().weekday() if weekday is None else weekday
            if isinstance(week[day], int) and 1 <= week[day] <= 8:
                return week[day]
        return int(cfg.get("record") or 1)

    def wide_options(self, username: str, tids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        """账号级任务的选项：存在登录账号上，各存档通用。"""
        out: dict[str, Any] = {}
        for t in tids:
            cfg = self.wide_cfg(username, t)
            o = dict(cfg.get("options", {}).get(t, {}))
            if cfg.get("claim_record"):  # 护送经书的领奖存档
                o["claim_record"] = cfg["claim_record"]
            if o:
                out[t] = o
        return deepcopy(out)

    def reset_one_shot_toggles(self, acc: dict[str, Any], slot: int, task_id: str,
                               keys: Iterable[str], *, account_wide: bool = False) -> bool:
        """把任务本次执行用过的一次性开关重置为关闭；返回设置是否有变化。"""
        if account_wide:
            wide = self.data.get("wide")
            user_cfg = wide.get(str(acc.get("username", ""))) if isinstance(wide, dict) else None
            cfg = user_cfg.get(task_id) if isinstance(user_cfg, dict) else None
        else:
            cfg = self.save_view(acc, slot)
        options = cfg.get("options") if isinstance(cfg, dict) else None
        task_options = options.get(task_id) if isinstance(options, dict) else None
        if not isinstance(task_options, dict):
            return False
        changed = False
        for key in keys:
            if task_options.get(key):
                task_options[key] = False
                changed = True
        return changed

    def wide_accounts(self, entries: list[dict[str, Any]], wide: dict[str, bool],
                      saver: Callable[[dict[str, Any]], Callable[[str], None]], only: str | None = None) -> list[Account]:
        """从 entries 涉及的每个登录账号生成账号级任务的执行项：同一登录账号 + 同一存档的任务合成一项。
        only：只做这一个任务（忽略开关）。"""
        out: list[Account] = []
        done: set[str] = set()
        for acc in entries:
            user = acc.get("username", "")
            if user in done:
                continue
            done.add(user)
            by_record: dict[Any, list[str]] = {}
            for tid, default_on in wide.items():
                cfg = self.wide_cfg(user, tid, default_on)
                if tid == only or (only is None and cfg["on"]):
                    by_record.setdefault(self.wide_record(cfg), []).append(tid)
            for record, tids in by_record.items():
                base = self.to_account(acc, saver(acc), int(record))
                out.append(replace(base, name=self.wide_name(self.label(acc), int(record)), tasks=tuple(tids), options=self.wide_options(user, tids),
                                   tidy=None))
        return out

    def peer_resolver(self, saver: Callable[[dict[str, Any]], Callable[[str], None]]) -> Callable[[str, int], Account | None]:
        """按（登录名, 存档）找账号：师徒试炼要同时登录师傅和徒弟两个账号。saver(账号条目) 给出保存 Cookie 的回调。"""
        def find(username: str, slot: int) -> Account | None:
            for acc in self.accounts:
                if acc.get("username") == username and 1 <= int(slot) <= 8:
                    return self.to_account(acc, saver(acc), int(slot))
            return None
        return find

    @staticmethod
    def tidy_every(sv: dict[str, Any]) -> int | None:
        """存档的同物归库设置 {"on", "every"} → Account.tidy（关 = None）。"""
        cfg = sv.get("tidy")
        if not isinstance(cfg, dict) or not cfg.get("on"):
            return None
        every = cfg.get("every", 0)
        return every if isinstance(every, int) and not isinstance(every, bool) and every >= 0 else 0

    def to_account(self, acc: dict[str, Any], on_cookie: Callable[[str], None] | None = None, slot: int = 1) -> Account:
        """某个存档的执行项。slot：界面 1-8。"""
        sv = self.save_view(acc, slot)
        mapping = {
            "username": acc.get("username", ""),
            "password": self.get_secret(acc, "password"),
            "second_password": self.get_secret(acc, "second_password"),
            "cookie": self.get_secret(acc, "cookie"),
            "record_index": int(slot) - 1,
        }
        return Account(
            name=self.slot_name(self.label(acc), slot),
            config=to_client_config(mapping),
            tasks=tuple(sv.get("tasks", ())),
            options=deepcopy(sv.get("options", {})),
            on_cookie=on_cookie,
            no_pickup=frozenset(sv.get("no_pickup", ())),
            tidy=self.tidy_every(sv),
            account_profile=self.account_profile(str(acc.get("username", ""))),
        )
