"""离线测试。导入这个包时装上“禁止联网”的闸门：哪个用例漏了 patch、真去连外网，会在这里被拦下并记下来。

本机回环（socketpair、127.0.0.1）不受影响。被拦的连接抛 OSError——和断网时一样，代码会走“读不到就沿用本地”的路；
``BLOCKED`` 里记着被拦的地址，``tests/test_zz_offline.py`` 最后检查它是空的。
"""

import socket

BLOCKED: list[object] = []
_connect = socket.socket.connect


def _offline(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host in ("127.0.0.1", "::1", "localhost"):
        return _connect(self, address)
    BLOCKED.append(address)
    raise OSError(f"测试不许联网：{address}")


socket.socket.connect = _offline

# 测试只读自己的数据表快照 tests/tables/（tools/test_tables.py 生成，不进 git），不读 basic/：
# 用例里写着某一版表的内容（当周开放的 Boss、表对应的养成周……），而 basic/ 里的表每周跟着游戏更新换。
# 新表还合不合用由 tests/test_new_tables.py 单独查。
import os  # noqa: E402
from pathlib import Path  # noqa: E402

from zmxy import data  # noqa: E402

_named = os.environ.get("ZMXY_TEST_TABLES")  # 只给 tools/test_tables.py 生成快照时用
_snapshots = sorted((Path(__file__).parent / "tables").glob("cg1_*_extracted"))
if not _named and not _snapshots:
    raise RuntimeError("没有测试用的数据表快照：先运行 python tools/test_tables.py（从 basic/ 里的数据表生成 tests/tables/）")
TABLES = Path(_named) if _named else _snapshots[-1]
data.use(TABLES)

# 世界 Boss 的同场玩家记录写进临时目录：跑测试不往真的设置目录里添记录。
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

from zmxy import bosslog  # noqa: E402

BOSS_LOG = Path(tempfile.mkdtemp(prefix="zmxy_test_")) / "world_boss_log"  # 一场一个文件
atexit.register(shutil.rmtree, BOSS_LOG.parent, True)
bosslog.folder = lambda: BOSS_LOG

# 服务器时钟的差值同样写进临时目录（进角时会学一次），不碰真的设置目录。
from zmxy import servertime  # noqa: E402

servertime.path_override = BOSS_LOG.parent / "server_clock.json"
