"""程序自动更新（zmxy/appupdate.py）：版本比较、读 Release、下载校验、替换失败时换回。不联网。"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zmxy import appupdate


class Resp(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def release_json(tag: str, assets: tuple[str, ...] = (appupdate.EXE, appupdate.EXE + ".sha256")) -> bytes:
    return json.dumps({"tag_name": tag, "body": "修了几个问题",
                       "assets": [{"name": n, "browser_download_url": f"https://dl/{n}", "size": 3} for n in assets]}).encode()


class VersionTest(unittest.TestCase):
    def test_order(self) -> None:
        self.assertEqual(appupdate.version_key("V1.10.2"), (1, 10, 2))
        self.assertTrue(appupdate.newer("V1.10", "V1.9"))
        self.assertTrue(appupdate.newer("v1.4", "V1.3"))
        self.assertFalse(appupdate.newer("V1.3", "V1.3"))
        self.assertFalse(appupdate.newer("V1.2", "V1.3"))
        self.assertFalse(appupdate.newer("beta", "V1.3"))
        self.assertFalse(appupdate.newer("V2.0", None))  # 源码运行不更新


class UpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.exe = Path(tmp.name) / appupdate.EXE
        self.exe.write_bytes(b"old")
        def offline(*extra):  # 测试里一律不联网：没单独模拟的请求都当成连不上
            def open_(request, timeout=0):
                raise OSError("测试里不联网")
            return open_
        for name, value in (("exe_path", lambda: self.exe), ("current_version", lambda: "V1.3"), ("REPO", "someone/zmxy5"),
                            ("_opener", offline)):
            patch = mock.patch.object(appupdate, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, pages: dict[str, bytes]) -> None:
        self.fetched: list[str] = []
        def get(url: str, accept: str = "", mirror: str = appupdate.DIRECT) -> Resp:
            url = appupdate.MIRRORS[mirror][1] + url
            self.fetched.append(url)
            if url not in pages:
                raise appupdate.UpdateError("连不上")
            return Resp(pages[url])
        patch = mock.patch.object(appupdate, "_get", get)
        patch.start()
        self.addCleanup(patch.stop)

    def test_check(self) -> None:
        api = appupdate.API.format(repo="someone/zmxy5")
        self.serve({api: release_json("V1.4")})
        rel = appupdate.check()
        self.assertIsNotNone(rel)
        self.assertEqual((rel.tag, rel.notes), ("V1.4", "修了几个问题"))
        self.assertTrue(rel.exe_url.endswith(appupdate.EXE) and rel.sha_url.endswith(".sha256"))
        self.serve({api: release_json("V1.3")})
        self.assertIsNone(appupdate.check())
        self.serve({api: release_json("V1.5", (appupdate.EXE,))})
        with self.assertRaisesRegex(appupdate.UpdateError, "缺少"):
            appupdate.check()

    def test_preview(self) -> None:
        """接收预览版：从发布列表里取版本最大的（含预览版，不看草稿）；不接收只读正式版接口。"""
        def rel(tag: str, **kw) -> dict:
            return {**json.loads(release_json(tag)), **kw}
        listing = json.dumps([rel("V1.4", prerelease=True), rel("V1.5", draft=True), rel("V1.3.9")]).encode()
        api = appupdate.API.format(repo="someone/zmxy5")
        self.serve({appupdate.RELEASES.format(repo="someone/zmxy5"): listing, api: release_json("V1.3.9")})
        got = appupdate.check(preview=True)
        self.assertEqual((got.tag, got.preview), ("V1.4", True))
        self.assertEqual(appupdate.check().tag, "V1.3.9")  # 正式版
        self.assertFalse(appupdate.check().preview)
        self.assertEqual(appupdate.version_key("V1.4-beta2"), (1, 4))  # 后缀里的数字不算

    def test_redirect_fallback(self) -> None:
        """接口直连、镜像都查不到：经镜像读 releases/latest 的跳转拿正式版版本号；跳到发布列表页 = 还没发布；直连不退回。"""
        import urllib.error

        def opener(location: str):
            def open_(request, timeout=0):
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": location}, None)
            return lambda *extra: open_
        with mock.patch.object(appupdate, "_api", side_effect=appupdate.UpdateError("查不到")):
            with mock.patch.object(appupdate, "_opener", opener("https://ghfast.top/https://github.com/someone/zmxy5/releases/tag/V1.4")):
                rel = appupdate.check("ghfast")
            self.assertEqual((rel.tag, rel.sha256), ("V1.4", ""))  # 没有 GitHub 校验值：下载时读 .sha256
            self.assertEqual(rel.exe_url, "https://github.com/someone/zmxy5/releases/download/V1.4/ZMXY5.exe")
            with mock.patch.object(appupdate, "_opener", opener("https://github.com/someone/zmxy5/releases/tag/V1.3")):
                self.assertIsNone(appupdate.check("ghfast"))  # 不比现在新
            with mock.patch.object(appupdate, "_opener", opener("https://github.com/someone/zmxy5/releases")):
                self.assertIsNone(appupdate.check("ghfast"))  # 还没发布
            with self.assertRaisesRegex(appupdate.UpdateError, "查不到"):
                appupdate.check(appupdate.DIRECT)

    def test_mirror(self) -> None:
        """选了镜像：查版本先直连接口，连不上再走镜像；下载（含校验文件）走镜像。"""
        api = appupdate.API.format(repo="someone/zmxy5")
        pre = appupdate.MIRRORS["ghfast"][1]
        self.serve({pre + api: release_json("V1.4")})
        self.assertEqual(appupdate.check("ghfast").tag, "V1.4")
        self.assertEqual(self.fetched, [api, pre + api])
        other = appupdate.MIRRORS["ghproxy"][1]  # 选中的镜像不转发接口：换别的镜像查
        self.serve({other + api: release_json("V1.4")})
        self.assertEqual(appupdate.check("ghfast").tag, "V1.4")
        self.assertEqual(self.fetched, [api, pre + api, other + api])
        self.serve({})
        with self.assertRaisesRegex(appupdate.UpdateError, "所有镜像"):
            appupdate.check("ghfast")
        with self.assertRaisesRegex(appupdate.UpdateError, "连不上"):
            appupdate.check(appupdate.DIRECT)
        good = hashlib.sha256(b"new").hexdigest()
        rel = appupdate.Release("V1.4", "", "https://dl/exe", "https://dl/sha", 3)
        self.serve({pre + "https://dl/exe": b"new", pre + "https://dl/sha": good.encode()})
        self.assertEqual(appupdate.download(rel, mirror="ghfast").read_bytes(), b"new")
        self.assertTrue(all(u.startswith(pre) for u in self.fetched))
        self.assertEqual(appupdate.page_url(), "https://github.com/someone/zmxy5")

    def test_github_digest_beats_mirror(self) -> None:
        """GitHub 接口给了附件校验值：用它核对，不读镜像的 .sha256；镜像把程序和校验文件一起换了也不替换。"""
        api = appupdate.API.format(repo="someone/zmxy5")
        good = hashlib.sha256(b"new").hexdigest()
        info = json.loads(release_json("V1.4"))
        info["assets"][0]["digest"] = "sha256:" + good.upper()
        self.serve({api: json.dumps(info).encode()})
        rel = appupdate.check("ghfast")
        self.assertEqual(rel.sha256, good)
        pre = appupdate.MIRRORS["ghfast"][1]
        evil = hashlib.sha256(b"evil").hexdigest()
        self.serve({pre + rel.exe_url: b"evil", pre + rel.sha_url: evil.encode()})
        with self.assertRaisesRegex(appupdate.UpdateError, "对不上"):
            appupdate.download(rel, mirror="ghfast")
        self.assertNotIn(pre + rel.sha_url, self.fetched)
        self.assertEqual(self.exe.read_bytes(), b"old")
        self.serve({pre + rel.exe_url: b"new"})
        self.assertEqual(appupdate.download(rel, mirror="ghfast").read_bytes(), b"new")
        for bad in ("", "md5:abc", "sha256:xyz"):
            self.assertEqual(appupdate._digest({"digest": bad}), "")

    def test_probe(self) -> None:
        """测线路：下到了 / GitHub 回 404（还没发布）= 通；镜像拒绝、超时 = 不通。地址带上镜像前缀。"""
        import urllib.error
        seen: list[str] = []

        def opener(outcome):
            def open_(request, timeout=0):
                seen.append(request.full_url)
                if isinstance(outcome, Exception):
                    raise outcome
                return Resp(b"ab" * 32)
            return lambda: open_
        cases = [(None, (True, True, "")),  # 下到了：有速度
                 (urllib.error.HTTPError("u", 404, "nf", {}, None), (True, False, "还没有发布")),
                 (urllib.error.HTTPError("u", 403, "no", {}, None), (False, False, "HTTP 403")),
                 (TimeoutError("timed out"), (False, False, "超时"))]
        for outcome, want in cases:
            with mock.patch.object(appupdate, "_opener", opener(outcome)):
                r = appupdate.probe("ghproxy")
            self.assertEqual((r.ok, r.speed is not None, r.note), want)
            self.assertGreaterEqual(r.seconds, 0)
        self.assertEqual(seen[0], appupdate.MIRRORS["ghproxy"][1]
                         + "https://github.com/someone/zmxy5/releases/latest/download/ZMXY5.exe")
        self.assertEqual(appupdate.speed_text(5 * 1024), "5 KB/秒")
        self.assertEqual(appupdate.speed_text(3 * 1048576), "3.0 MB/秒")

    def test_cancel_download(self) -> None:
        """下载中取消：停下、删掉没下完的文件、不替换。"""
        import threading
        rel = appupdate.Release("V1.4", "", "https://dl/exe", "https://dl/sha", 0, hashlib.sha256(b"x" * 300000).hexdigest())
        self.serve({"https://dl/exe": b"x" * 300000})
        cancel = threading.Event()
        with self.assertRaisesRegex(appupdate.UpdateError, "已取消"):
            appupdate.download(rel, lambda d, t: cancel.set(), cancel=cancel)
        self.assertEqual(list(self.exe.parent.glob("*.download")) + list(self.exe.parent.glob("*.new")), [])
        self.assertEqual(self.exe.read_bytes(), b"old")

    def test_download_checks_hash(self) -> None:
        rel = appupdate.Release("V1.4", "", "https://dl/exe", "https://dl/sha", 3)
        good = hashlib.sha256(b"new").hexdigest()
        self.serve({"https://dl/exe": b"new", "https://dl/sha": f"{good}  ZMXY5.exe\n".encode()})
        seen: list[tuple[int, int]] = []
        new = appupdate.download(rel, lambda d, t: seen.append((d, t)))
        self.assertEqual(new.read_bytes(), b"new")
        self.assertEqual(seen[-1], (3, 3))
        self.serve({"https://dl/exe": b"bad", "https://dl/sha": good.encode()})
        with self.assertRaisesRegex(appupdate.UpdateError, "对不上"):
            appupdate.download(rel)
        self.assertFalse(self.exe.with_name(appupdate.EXE + ".download").exists())
        self.serve({"https://dl/exe": b"new", "https://dl/sha": b"<html>"})
        with self.assertRaisesRegex(appupdate.UpdateError, "格式"):
            appupdate.download(rel)

    def test_install_and_cleanup(self) -> None:
        new = self.exe.with_name(appupdate.EXE + ".new")
        new.write_bytes(b"new")
        appupdate.install(new)
        self.assertEqual(self.exe.read_bytes(), b"new")
        old = self.exe.with_name(appupdate.EXE + ".old")
        self.assertEqual(old.read_bytes(), b"old")
        appupdate.cleanup()
        self.assertFalse(old.exists())
        self.assertTrue(self.exe.exists())

    def test_install_restores_on_failure(self) -> None:
        missing = self.exe.with_name(appupdate.EXE + ".new")  # 新文件不在：放不进去 → 换回原来的
        with self.assertRaisesRegex(appupdate.UpdateError, "恢复"):
            appupdate.install(missing)
        self.assertEqual(self.exe.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
