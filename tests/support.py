"""测试共用的假连接和帮手函数（各测试文件不再互相 import）。

* ``FakeConn``：按“路由 → 应答”的表回话，适合一问一答的任务；``run_daily`` 配它用。
* ``Fake``：按 handler(route, body) 回话，适合要看请求内容、有状态的任务；``run`` / ``run_dungeon`` 配它用。
"""

from datetime import datetime, timedelta, timezone

from zmxy import dungeon, login, protocol as p
from zmxy.core import Session, TaskContext, load_tasks

CN = timezone(timedelta(hours=8))


def ts(y, m, d, h=12):
    return int(datetime(y, m, d, h, tzinfo=CN).timestamp())


class FakeConn:
    """responses: 路由 -> 响应 dict，或可调用 (body)->dict，或 Exception 实例（作为服务器拒绝）。"""

    def __init__(self, responses=None, exc=None):
        self.responses, self.exc, self.sent = responses or {}, exc, []

    def request(self, route, body):
        self.sent.append((route, body))
        if self.exc:
            raise self.exc
        r = self.responses.get(route, {})
        r = r(body) if callable(r) else r
        if isinstance(r, str):
            return {"code": 500, "msg": r}
        return {"code": 200, **r}

    def routes(self):
        return [r for r, _ in self.sent]

    def bodies(self, route):
        return [b for r, b in self.sent if r == route]


class Fake:
    """按路由脚本化应答的假连接。handler(route, body) 返回：
    带 code 的 dict = 原样作为应答；不带 code 的 dict = 成功应答（补 code 200）；str = 服务器拒绝；None = 默认应答；也可以直接抛异常。"""

    def __init__(self, handler=None):
        self.handler, self.sent, self.pushes = handler, [], []

    def request(self, route, body):
        self.sent.append((route, body))
        out = self.handler(route, body) if self.handler else None
        if isinstance(out, str):
            return {"code": 500, "error": out}
        if isinstance(out, dict) and "code" not in out:
            return {"code": 200, **out}
        if out is not None:
            return out
        return {"code": 200, "rnd": 7} if route == dungeon.R_CREATE else {"code": 200}

    def drain_pushes(self):
        out, self.pushes = self.pushes, []
        return out

    def routes(self):
        return [r.rsplit(".", 1)[1] for r, _ in self.sent]

    def bodies(self, name):
        return [b for r, b in self.sent if r.endswith(name)]


def run(task_id, conn, info, options=None, now=ts(2026, 10, 1), stop=None, tasks=(), all_options=None, session=None, log=None, account_profile=None,
        stage=False):
    """stage=True：只跑该任务的在线阶段（TaskSpec.stage）。"""
    s = session or Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s.info = conn, info
    s._link = login.Link(conn, s.config, [{"index": 0}], {"time": now} if now is not None else {})
    s.bind(load_tasks()[task_id])
    spec = load_tasks()[task_id]
    return (spec.stage if stage else spec.run)(TaskContext(s, options or {}, stop=stop, tasks=tuple(tasks), all_options=all_options or {},
                                                 account_profile=account_profile if account_profile is not None else {},
                                                 **({"log": log} if log else {})))


def run_daily(task_id, conn, info, options=None, now=1_000_000):
    s = Session(p.ClientConfig(uid=1, username="u"))
    s._conn, s.info = conn, info
    s._link = login.Link(conn, s.config, [], {"time": now})
    s.bind(load_tasks()[task_id])
    return load_tasks()[task_id].run(TaskContext(s, options or {}))


def day(**kw):
    return {"everyDaySetting": kw}


LEVEL = {"dungeon_main": 51, "dungeon_hidden": 1002, "dungeon_elite": 30600, "dungeon_nightmare": 40170}


def run_dungeon(task_id, conn, info, options=None, no_pickup=()):
    """options 里没写 levels 时补上该任务的测试关卡；产品里没有默认关卡。"""
    session = Session(p.ClientConfig(uid=1, username="u"))
    session._conn, session.info = conn, info
    spec = load_tasks()[task_id]
    session.bind(spec)
    options = ({"levels": [LEVEL[task_id]]} if task_id in LEVEL else {}) | (options or {})
    return spec.run(TaskContext(session, options, no_pickup=frozenset(no_pickup)))


def elite_info(used=0, vip=0):
    return {"vip": {"level": vip}, "dungeon": {"30600": {"id": 30600, "dailyCount": used}}}
