"""程序自动更新：从 GitHub 公开发布仓库的 Releases 检查、下载新版 ZMXY5.exe，替换后重启。

发布仓库的 Releases 放打包好的程序；仓库里另有部分源码（不含登录、通讯、激活，不能单独运行）。每次发布一个 Release：
* tag = 程序版本（和打包时填的一样，如 ``V1.4``）；正文 = 更新说明（检测到新版时显示给用户）；
* 附件 ``ZMXY5.exe`` 和 ``ZMXY5.exe.sha256``（打包时 tools/build.py 生成，下载后核对，对不上不替换）。
见 tools/publish.py。

只在打包后的程序里工作（源码运行没有程序版本）。替换的做法：Windows 不能覆盖正在运行的 exe，但能改名：
正在运行的 ``ZMXY5.exe`` 改名成 ``ZMXY5.exe.old``，新的改名成 ``ZMXY5.exe``；程序退出后再启动新的（``restart_after_exit``），
下次启动时删掉 ``.old``（``cleanup``）。下载到一半的是 ``.download``，不会被当成程序。

国内直连 GitHub 下载常常很慢或连不上：设置里可以选镜像（MIRRORS，在 GitHub 地址前面加镜像前缀）。
选了镜像时，查版本先直连 GitHub 接口（国内一般能连上），连不上再走选中的镜像、再试其他镜像（不是每个镜像都转发接口）；下载程序走选中的镜像。
校验值优先用 GitHub 接口里附件自带的 SHA-256（digest）：直连接口拿到的这个值镜像改不了，镜像换了程序文件也对不上、不会替换；
接口里没有（老版本的附件）才读 .sha256 附件，走镜像时它也从同一个镜像读。接口本身也是走镜像读到的，就防不住镜像造假。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = "Fylxom/ZMXY5"  # 发布仓库 "用户名/仓库名"（公开）；空 = 不检查更新。发出去以后别改名
EXE = "ZMXY5.exe"
API = "https://api.github.com/repos/{repo}/releases/latest"  # 最新正式版（跳过预览版和草稿）
RELEASES = "https://api.github.com/repos/{repo}/releases?per_page=20"  # 最近的发布（含预览版）
PAGE = "https://github.com/{repo}"
TIMEOUT = 20
PROBE_TIMEOUT = 10  # 设置里“测试线路”每条线路等回应最多几秒
PROBE_BYTES = 1024 * 1024  # 测速下载程序文件的开头这么多
PROBE_READ = 5  # 测速最多下几秒
SLOW = 100 * 1024  # 下载速度低于这个（字节/秒）算慢：提示可以取消后换线路
DIRECT = "direct"
MIRRORS = {  # 下载线路：值 → (显示文字, 加在 GitHub 地址前面的前缀)；镜像失效了就在这里换
    DIRECT: ("直连", ""),
    "ghfast": ("镜像 1", "https://ghfast.top/"),
    "ghproxy": ("镜像 2", "https://gh-proxy.com/"),
}


class UpdateError(Exception):
    """检查、下载、替换失败：说明直接给用户看。"""


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    exe_url: str
    sha_url: str
    size: int
    sha256: str = ""  # GitHub 接口里附件自带的校验值（小写十六进制）；没有 = 空，下载时读 .sha256 附件
    preview: bool = False  # 预览版（GitHub 上标了 prerelease）


def _digest(asset: dict) -> str:
    """附件的 "digest": "sha256:<64 位十六进制>" → 十六进制；没有或格式不对 = 空。"""
    kind, _, value = str(asset.get("digest") or "").partition(":")
    return value.lower() if kind == "sha256" and re.fullmatch(r"[0-9a-fA-F]{64}", value) else ""


def page_url() -> str:
    """发布仓库的网页（设置里“打开”）。"""
    return PAGE.format(repo=REPO)


@dataclass(frozen=True)
class Probe:
    ok: bool               # 通不通
    seconds: float         # 收到回应用了几秒
    speed: float | None    # 下载速度（字节/秒）；没测到（还没有发布、不通）= None
    note: str = ""         # 还没有发布 / HTTP 403 / 超时……


def probe(mirror: str) -> Probe:
    """测一条线路：经这条线路下载最新发布的程序文件开头一段（PROBE_BYTES，最多 PROBE_READ 秒），和真正更新走同一条下载路。
    GitHub 回 404（还没有发布）也算通：请求已经到了 GitHub，只是没有文件可测速度。"""
    url = _prefix(mirror) + PAGE.format(repo=REPO) + f"/releases/latest/download/{EXE}"
    request = urllib.request.Request(url, headers={"User-Agent": "ZMXY5-updater", "Range": f"bytes=0-{PROBE_BYTES - 1}"})
    start = time.monotonic()
    try:
        with _opener()(request, timeout=PROBE_TIMEOUT) as response:
            first = time.monotonic()
            got, stop = 0, first + PROBE_READ
            while got < PROBE_BYTES and time.monotonic() < stop and (chunk := _read(response, 64 * 1024)):
                got += len(chunk)
            spent = time.monotonic() - first
        return Probe(True, first - start, got / spent if got and spent > 0 else None)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return Probe(True, time.monotonic() - start, None, "还没有发布")
        return Probe(False, time.monotonic() - start, None, f"HTTP {exc.code}")
    except Exception as exc:
        timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc)
        return Probe(False, time.monotonic() - start, None, "超时" if timeout else type(exc).__name__)


def _read(response, size: int) -> bytes:
    """有多少先拿多少（read1），慢的时候也能及时报进度、及时响应取消；没有 read1 的退回 read。"""
    return response.read1(size) if hasattr(response, "read1") else response.read(size)


def speed_text(bps: float) -> str:
    return f"{bps / 1048576:.1f} MB/秒" if bps >= 1048576 else f"{bps / 1024:.0f} KB/秒"


def _prefix(mirror: str) -> str:
    return MIRRORS.get(mirror, MIRRORS[DIRECT])[1]


def current_version() -> str | None:
    """打包时写进去的程序版本（如 V1.3）；源码运行 = None。"""
    try:
        from . import _build_info
    except ImportError:
        return None
    return getattr(_build_info, "APP_VERSION", None) or None


def enabled() -> bool:
    return bool(REPO) and current_version() is not None


def exe_path() -> Path:
    """正在运行的程序文件（Nuitka 单文件版的 argv[0] 是用户双击的那个 exe）。"""
    return Path(sys.argv[0]).resolve()


def version_key(tag: str) -> tuple[int, ...]:
    """"V1.4" / "v1.10.2" → (1, 4) / (1, 10, 2)；读不出数字 = ()。
    只认开头的版本号："V1.4-beta2" 当成 (1, 4)，不把 beta 后面的 2 当成更新的版本。"""
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", tag or "")
    return tuple(int(n) for n in m.group(1).split(".")) if m else ()


def newer(tag: str, current: str | None) -> bool:
    return bool(version_key(tag)) and current is not None and version_key(tag) > version_key(current)


def _opener(*extra: Any) -> Callable:
    """走系统代理、用 Windows 证书库（和登录 4399 一样），国内有代理时也能连 GitHub。extra：再加的处理器（如不跟跳转）。"""
    handlers: list = [urllib.request.ProxyHandler(urllib.request.getproxies()), *extra]
    if os.name == "nt":
        from .auth4399 import platform_ssl_context
        handlers.append(urllib.request.HTTPSHandler(context=platform_ssl_context()))
    return urllib.request.build_opener(*handlers).open


def _get(url: str, accept: str = "application/octet-stream", mirror: str = DIRECT):
    """mirror：走哪条线路（MIRRORS 的键）。"""
    request = urllib.request.Request(_prefix(mirror) + url, headers={"User-Agent": "ZMXY5-updater", "Accept": accept})
    try:
        return _opener()(request, timeout=TIMEOUT)
    except Exception as exc:
        if mirror not in MIRRORS or mirror == DIRECT:
            raise UpdateError(f"连不上 GitHub（{type(exc).__name__}）；在国内可以在设置里把下载线路换成镜像再试") from exc
        raise UpdateError(f"连不上{MIRRORS[mirror][0]}（{type(exc).__name__}），可以换一条线路再试") from exc


def check(mirror: str = DIRECT, preview: bool = False) -> Release | None:
    """读发布仓库的最新 Release：比现在的新 → Release，不新 → None。先直连 GitHub 接口，连不上且选了镜像再走镜像。
    preview：也看预览版（GitHub 上标了 prerelease 的发布）——读最近的发布列表，取版本号最大的（不看草稿）；
    不开只看正式版（latest 接口本来就跳过预览版）。"""
    if not REPO:
        raise UpdateError("还没有设置更新地址")
    try:
        info = _api(RELEASES.format(repo=REPO) if preview else API.format(repo=REPO), mirror)
    except UpdateError:
        if mirror not in MIRRORS or mirror == DIRECT:
            raise
        return _latest_by_redirect(mirror)  # 接口直连、镜像都不行（镜像共用的接口额度常被用光）：退回读网页跳转，只能查正式版
    if preview:
        if not isinstance(info, list):
            raise UpdateError("GitHub 返回的发布列表读不懂")
        found = [r for r in info if isinstance(r, dict) and not r.get("draft") and version_key(str(r.get("tag_name") or ""))]
        if not found:
            return None
        info = max(found, key=lambda r: version_key(str(r.get("tag_name"))))
    elif not isinstance(info, dict):
        raise UpdateError("GitHub 返回的内容读不懂")
    tag = str(info.get("tag_name") or "")
    assets = {a.get("name"): a for a in info.get("assets") or [] if isinstance(a, dict)}
    exe, sha = assets.get(EXE), assets.get(EXE + ".sha256")
    if not newer(tag, current_version()):
        return None
    if not exe or not sha:
        raise UpdateError(f"新版本 {tag} 的发布里缺少 {EXE} 或 {EXE}.sha256，暂时不能自动更新")
    return Release(tag, str(info.get("body") or "").strip(), str(exe.get("browser_download_url")), str(sha.get("browser_download_url")),
                   int(exe.get("size") or 0), _digest(exe), bool(info.get("prerelease")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟着跳转：跳转本身就是要的答案（Location 里有版本号）。"""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _latest_by_redirect(mirror: str) -> Release | None:
    """备用查法：经镜像打开 github.com/<仓库>/releases/latest，它会跳到 .../releases/tag/<版本>，从跳转地址读出最新正式版。
    读不到更新说明和 GitHub 的校验值：下载后用 .sha256 附件核对（同一个镜像）。先试选中的镜像，再试其他镜像。"""
    page = PAGE.format(repo=REPO) + "/releases/latest"
    errors: list[Exception] = []
    for m in [mirror, *(k for k in MIRRORS if k not in (DIRECT, mirror))]:
        request = urllib.request.Request(_prefix(m) + page, headers={"User-Agent": "ZMXY5-updater"})
        try:
            with _opener(_NoRedirect())(request, timeout=TIMEOUT):
                pass
            errors.append(UpdateError("没有跳转"))  # 镜像没把跳转原样转过来：换下一个
            continue
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # 还没有发布
            where = (exc.headers.get("Location") or "") if 300 <= exc.code < 400 else ""
            if where.rstrip("/").endswith("/releases"):
                return None  # 还没有正式版：GitHub 跳到发布列表页
            found = re.search(r"/releases/tag/([^/?#]+)$", where)
            if not found:
                errors.append(exc)
                continue
            tag = urllib.parse.unquote(found.group(1))
        except Exception as exc:
            errors.append(exc)
            continue
        if not version_key(tag) or not newer(tag, current_version()):
            return None
        base = PAGE.format(repo=REPO) + f"/releases/download/{tag}/{EXE}"
        return Release(tag, "（这次是用备用方式查到的，读不到更新说明，可以在设置里点“GitHub 发布仓库”查看）", base, base + ".sha256", 0)
    raise UpdateError("直连 GitHub 和所有镜像都查不到新版本，请稍后再试") from errors[0]


def _api(api: str, mirror: str) -> Any:
    """读 GitHub 接口（JSON）。先直连，连不上且选了镜像再走镜像。"""
    try:
        response = _get(api, "application/vnd.github+json")
    except UpdateError:
        if mirror not in MIRRORS or mirror == DIRECT:
            raise
        # 不是每个镜像都转发接口（实测 ghfast.top 不转发、gh-proxy.com 转发）：先试选中的，再试其他镜像；下载仍走选中的
        response, errors = None, []
        for m in [mirror, *(k for k in MIRRORS if k not in (DIRECT, mirror))]:
            try:
                response = _get(api, "application/vnd.github+json", m)
                break
            except UpdateError as exc:
                errors.append(exc)
        if response is None:
            raise UpdateError("直连 GitHub 和所有镜像都查不到新版本，请稍后再试") from errors[0]
    with response:
        try:
            return json.loads(response.read().decode("utf-8"))
        except ValueError as exc:
            raise UpdateError("GitHub 返回的内容读不懂") from exc


def download(release: Release, progress: Callable[[int, int], None] | None = None, mirror: str = DIRECT,
             cancel: threading.Event | None = None) -> Path:
    """下载新版到程序旁边的 ``ZMXY5.exe.new``，核对 SHA-256。mirror：走哪条线路；cancel 设上 = 用户取消。返回下载好的文件。"""
    mirror = mirror if mirror in MIRRORS else DIRECT
    target = exe_path()
    part, new = target.with_name(EXE + ".download"), target.with_name(EXE + ".new")
    want = [release.sha256] if release.sha256 else None  # 有 GitHub 接口给的校验值就用它，镜像改不了
    if want is None:
        with _get(release.sha_url, mirror=mirror) as response:
            want = response.read().decode("utf-8", "replace").split()
    if not want or not re.fullmatch(r"[0-9a-fA-F]{64}", want[0]):
        raise UpdateError("校验文件格式不对，没有更新")
    digest = hashlib.sha256()
    done = 0
    try:
        with _get(release.exe_url, mirror=mirror) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or release.size or 0)
            while chunk := _read(response, 64 * 1024):
                if cancel is not None and cancel.is_set():
                    raise UpdateError("已取消下载")
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except PermissionError as exc:
        raise UpdateError(f"程序所在的文件夹不能写入（{target.parent}），请把程序放到桌面或其他普通文件夹再更新") from exc
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"下载中断（{type(exc).__name__}），请再试一次") from exc
    if digest.hexdigest().lower() != want[0].lower():
        part.unlink(missing_ok=True)
        raise UpdateError("下载的文件和发布的校验值对不上，没有更新，请再试一次")
    new.unlink(missing_ok=True)
    part.replace(new)
    return new


def install(new: Path) -> None:
    """把下载好的换成正式程序：正在运行的改名成 .old，新的改名成 ZMXY5.exe。出错就换回去。"""
    target = exe_path()
    old = target.with_name(EXE + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass  # 上次的旧程序还在被占用：换个名字放
    if old.exists():
        old = target.with_name(f"{EXE}.{os.getpid()}.old")
    try:
        target.replace(old)
    except OSError as exc:
        raise UpdateError(f"程序文件改不了名（{type(exc).__name__}），没有更新") from exc
    try:
        new.replace(target)
    except OSError as exc:
        old.replace(target)  # 换回去
        raise UpdateError(f"新程序放不进去（{type(exc).__name__}），已恢复原来的程序") from exc


def restart_after_exit() -> None:
    """本程序退出（释放单实例锁）以后，启动新的程序。"""
    target = exe_path()

    def launch() -> None:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(target)], cwd=str(target.parent), creationflags=flags, close_fds=True)
    atexit.register(launch)


def cleanup() -> None:
    """启动时删掉上次更新留下的旧程序、没下完的文件（删不掉就下次再删）。"""
    if current_version() is None:
        return
    folder = exe_path().parent
    for pattern in (EXE + ".old", EXE + ".*.old", EXE + ".download"):
        for path in folder.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass
