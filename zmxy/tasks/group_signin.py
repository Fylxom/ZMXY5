"""4399 群组签到：读指定群组页面，当天没签就签一次。走社区网页，不碰游戏连接。

社区登录态的恢复顺序：Cookie 读页 → 跳登录就同步一次 → 还跳就账密登录一次。
签到请求只发一次；结果读不懂一律 blocked，不重试。Cookie 只在本次任务的 jar 里，不写回设置。
"""

from __future__ import annotations

import html
import http.cookiejar
import http.cookies
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .. import auth4399, state
from ..core import BLOCKED, SKIPPED, SUBMITTED, TaskContext, TaskResult, task

ID = "group_signin"
GROUP_ID = 83193
GROUP_URL = f"https://my.4399.com/forums/mtag-{GROUP_ID}"
SIGN_URL = "https://my.4399.com/forums/grade-signIn"
SYNC_URL = "https://ptlogin.4399.com/ptlogin/onlineStat.do?appGame=my"  # 官方 ucenter.js 的 onlineStat
LOGIN_FRAME = "https://ptlogin.4399.com/ptlogin/loginFrame.do?appId=my&displayMode=embed&layout=vertical"
LOGIN_URL = "https://ptlogin.4399.com/ptlogin/login.do?v=1"
_CLASS_ATTR = re.compile(r"class\s*=\s*['\"]([^'\"]*)['\"]", re.I)
_LOGIN_PATH = "/account/login"
_MAX_BODY = 2 * 1024 * 1024


class _CommunityError(RuntimeError):
    """群组请求失败：只算本任务 blocked，不升级成游戏会话异常。"""


class _Stopped(Exception):
    pass


def _community_cookies(cookie: str) -> http.cookiejar.CookieJar:
    if "\r" in cookie or "\n" in cookie:
        raise _CommunityError("没有可用的 4399 群组登录态")
    values = http.cookies.SimpleCookie()
    try:
        values.load(cookie)
    except http.cookies.CookieError as exc:
        raise _CommunityError("4399 群组登录态格式异常") from exc
    if not values:
        raise _CommunityError("4399 群组登录态格式异常")
    jar = http.cookiejar.CookieJar()
    for name, item in values.items():
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=item.value, port=None, port_specified=False,
            domain=".4399.com", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    return jar


def _login_request(request, jar, timeout):
    """复用平台的证书和直连配置，只发一次。"""
    send = auth4399._auth_openers(request, cookie_jar=jar)[0][1]
    try:
        with send(request, timeout=timeout) as response:
            raw, status = response.read(_MAX_BODY + 1), response.status
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _CommunityError("4399 社区账密登录请求失败，未重试") from exc
    if len(raw) > _MAX_BODY:
        raise _CommunityError("4399 社区登录响应过大，未继续")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    return status, re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), html.unescape(text))


def _password_login(ctx, timeout):
    """账密建立社区会话，返回新 jar；看登录页时被停止返回 None，不提交账密。"""
    jar = http.cookiejar.CookieJar()
    headers = {"User-Agent": ctx.config.auth_user_agent, "Referer": LOGIN_FRAME}
    status, _ = _login_request(urllib.request.Request(LOGIN_FRAME, headers=headers), jar, timeout)
    if status >= 400:
        raise _CommunityError("4399 社区登录页请求被拒绝，未继续")
    if ctx.stopped or ctx.winding:
        return None
    # 与官方社区表单相同；mainDivId 必须是合法的内部容器名
    payload = auth4399._login_payload(ctx.config, None)
    payload.update(appId="my", bizId="", level="0", regLevel="0", displayMode="embed",
                   mainDivId="embed_login_div", postLoginHandler="refreshParent")
    request = urllib.request.Request(LOGIN_URL, data=urllib.parse.urlencode(payload).encode("utf-8"), method="POST",
                                     headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    status, text = _login_request(request, jar, timeout)
    try:
        auth4399._check_login_reply(status, text, jar)
    except auth4399.CaptchaRequired as exc:
        raise _CommunityError("4399 社区账密登录需要验证码，未签到；请先在社区网页登录") from exc
    except auth4399.AllianceError as exc:
        raise _CommunityError(str(exc)) from exc
    if not any(c.name in ("Pauth", "Uauth") and c.value for c in jar):
        raise _CommunityError("账密登录响应未建立社区登录态，未签到；未重试")
    return jar


def _request(url: str, *, timeout: float, cookie_jar: http.cookiejar.CookieJar, method: str = "GET",
             form: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """返回 (最终地址, 正文)。Cookie 由 jar 按域和路径发送，同步到的新 Uauth 才能盖掉旧的。"""
    headers = {"Accept": "application/json, text/html, */*", "Referer": GROUP_URL,
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers.update({"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"})
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        with opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=timeout) as response:
            text = response.read(_MAX_BODY + 1)
            if len(text) > _MAX_BODY:
                raise _CommunityError("4399 群组返回内容过大，未继续处理")
            return response.geturl(), text.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):  # opener 不跟的跳转（如 POST 的 307）：地址够判断登录失效了
            return str(exc.headers.get("Location") or ""), ""
        raise _CommunityError(f"4399 群组请求被拒绝（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _CommunityError("4399 群组请求失败，请稍后重试") from exc


def _login_redirect(final_url: str) -> bool:
    """只认最终地址的路径；页面里的登录链接、query 里的地址都不算失效。"""
    path = urllib.parse.urlsplit(final_url).path.rstrip("/")
    return path == _LOGIN_PATH or path.startswith(_LOGIN_PATH + "/")


def _page_state(page: str, final_url: str) -> str:
    if _login_redirect(final_url):
        return "login"
    entries = [names for found in _CLASS_ATTR.finditer(page) if "j-showCal" in (names := found.group(1).split())]
    if not entries:
        return "unavailable"
    return "signed" if any("hassign" in names for names in entries) else "ready"


def _sign_reply(text: str) -> tuple[int | None, Mapping[str, Any]]:
    try:
        value = json.loads(text)
    except ValueError:
        return None, {}
    if not isinstance(value, Mapping):
        return None, {}
    result = value.get("result")
    return state.num(value.get("code")), result if isinstance(result, Mapping) else {}


def _sign(ctx: TaskContext, cookie: str, timeout: float) -> TaskResult:
    def check_stop() -> None:
        if ctx.stopped or ctx.winding:
            raise _Stopped

    def login():
        jar = _password_login(ctx, timeout)
        if jar is None:
            raise _Stopped
        return jar

    def read_group(jar) -> str:
        check_stop()
        final_url, page = _request(GROUP_URL, timeout=timeout, cookie_jar=jar)
        return _page_state(page, final_url)

    check_stop()
    jar = _community_cookies(cookie) if cookie else login()
    page_state = read_group(jar)
    if page_state == "login":  # 平台 Pauth 可能有效而社区 Uauth 还没建立
        check_stop()
        ctx.log("群组页跳转登录，正在同步社区登录态")
        _request(SYNC_URL, timeout=timeout, cookie_jar=jar)
        page_state = read_group(jar)
    if page_state == "login" and cookie and ctx.config.password:
        check_stop()
        ctx.log("社区登录态同步未恢复，使用该账号账密登录社区")
        jar = login()
        page_state = read_group(jar)
    if page_state == "login":
        return TaskResult(ID, BLOCKED, "4399 社区登录态同步后仍跳转登录页，未签到；游戏登录态可能仍有效")
    if page_state == "unavailable":
        return TaskResult(ID, BLOCKED, "账号未加入该群组或页面未返回签到入口，未签到")
    if page_state == "signed":
        return TaskResult(ID, SKIPPED, "今天已经完成群组签到")
    check_stop()

    final_url, text = _request(SIGN_URL, timeout=timeout, method="POST", cookie_jar=jar,
                               form={"sign": 1, "tagid": GROUP_ID, "_AJAX_": 1})
    if _login_redirect(final_url):
        return TaskResult(ID, BLOCKED, "4399 群组登录态已失效，签到结果未确认")
    code, result = _sign_reply(text)
    if code == 100:
        days = state.find(result, "continueDay")
        return TaskResult(ID, SUBMITTED, "群组签到成功" + (f"，连续签到 {days} 天" if days is not None else ""))
    if result.get("isSigned") or "已签到" in text:
        return TaskResult(ID, SKIPPED, "今天已经完成群组签到")
    if code is None:
        return TaskResult(ID, BLOCKED, "群组签到返回格式无法识别，结果未确认；未重试")
    return TaskResult(ID, BLOCKED, f"群组签到被服务器拒绝（code={code}），未重试")


@task(ID, "群组签到", group="日常", account_wide=True)
def run(ctx: TaskContext) -> TaskResult:
    cookie = ctx.config.cookie
    if not cookie and not ctx.config.password:
        return TaskResult(ID, BLOCKED, "没有可用的 4399 群组登录态，未签到")
    try:
        return _sign(ctx, cookie, min(max(float(ctx.config.timeout), 5.0), 30.0))
    except _Stopped:
        return TaskResult(ID, SKIPPED, "已停止，未签到")
    except _CommunityError as exc:
        return TaskResult(ID, BLOCKED, f"群组签到未完成：{exc}")
