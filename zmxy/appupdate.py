"""程序自动更新：从 GitHub 公开发布仓库的 Releases 检查、下载新版 ZMXY5.exe，替换后重启。

发布仓库只放打包好的程序（源码不公开）。每次发布一个 Release：
* tag = 程序版本（和打包时填的一样，如 ``V1.4``）；正文 = 更新说明（检测到新版时显示给用户）；
* 附件 ``ZMXY5.exe`` 和 ``ZMXY5.exe.sha256``（打包时 tools/build.py 生成，下载后核对，对不上不替换）。
见 tools/publish.py。

只在打包后的程序里工作（源码运行没有程序版本）。替换的做法：Windows 不能覆盖正在运行的 exe，但能改名：
正在运行的 ``ZMXY5.exe`` 改名成 ``ZMXY5.exe.old``，新的改名成 ``ZMXY5.exe``；程序退出后再启动新的（``restart_after_exit``），
下次启动时删掉 ``.old``（``cleanup``）。下载到一半的是 ``.download``，不会被当成程序。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = "Fylxom/ZMXY5"  # 发布仓库 "用户名/仓库名"（公开，只放打包好的程序）；空 = 不检查更新。发出去以后别改名
EXE = "ZMXY5.exe"
API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 20


class UpdateError(Exception):
    """检查、下载、替换失败：说明直接给用户看。"""


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    exe_url: str
    sha_url: str
    size: int


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
    """"V1.4" / "v1.10.2" → (1, 4) / (1, 10, 2)；读不出数字 = ()。"""
    return tuple(int(n) for n in re.findall(r"\d+", tag or ""))


def newer(tag: str, current: str | None) -> bool:
    return bool(version_key(tag)) and current is not None and version_key(tag) > version_key(current)


def _opener() -> Callable:
    """走系统代理、用 Windows 证书库（和登录 4399 一样），国内有代理时也能连 GitHub。"""
    handlers: list = [urllib.request.ProxyHandler(urllib.request.getproxies())]
    if os.name == "nt":
        from .auth4399 import platform_ssl_context
        handlers.append(urllib.request.HTTPSHandler(context=platform_ssl_context()))
    return urllib.request.build_opener(*handlers).open


def _get(url: str, accept: str = "application/octet-stream"):
    request = urllib.request.Request(url, headers={"User-Agent": "ZMXY5-updater", "Accept": accept})
    try:
        return _opener()(request, timeout=TIMEOUT)
    except Exception as exc:
        raise UpdateError(f"连不上 GitHub（{type(exc).__name__}），请稍后再试，或者开着代理再试") from exc


def check() -> Release | None:
    """读发布仓库的最新 Release：比现在的新 → Release，不新 → None。"""
    if not REPO:
        raise UpdateError("还没有设置更新地址")
    with _get(API.format(repo=REPO), "application/vnd.github+json") as response:
        try:
            info = json.loads(response.read().decode("utf-8"))
        except ValueError as exc:
            raise UpdateError("GitHub 返回的内容读不懂") from exc
    tag = str(info.get("tag_name") or "")
    assets = {a.get("name"): a for a in info.get("assets") or [] if isinstance(a, dict)}
    exe, sha = assets.get(EXE), assets.get(EXE + ".sha256")
    if not newer(tag, current_version()):
        return None
    if not exe or not sha:
        raise UpdateError(f"新版本 {tag} 的发布里缺少 {EXE} 或 {EXE}.sha256，暂时不能自动更新")
    return Release(tag, str(info.get("body") or "").strip(), str(exe.get("browser_download_url")), str(sha.get("browser_download_url")),
                   int(exe.get("size") or 0))


def download(release: Release, progress: Callable[[int, int], None] | None = None) -> Path:
    """下载新版到程序旁边的 ``ZMXY5.exe.new``，核对 SHA-256。返回下载好的文件。"""
    target = exe_path()
    part, new = target.with_name(EXE + ".download"), target.with_name(EXE + ".new")
    with _get(release.sha_url) as response:
        want = response.read().decode("utf-8", "replace").split()
    if not want or not re.fullmatch(r"[0-9a-fA-F]{64}", want[0]):
        raise UpdateError("校验文件格式不对，没有更新")
    digest = hashlib.sha256()
    done = 0
    try:
        with _get(release.exe_url) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or release.size or 0)
            while chunk := response.read(256 * 1024):
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
