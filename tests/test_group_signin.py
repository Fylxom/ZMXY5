"""群组签到登录链接误判与单次提交保护的离线回归。"""

import unittest
import io
from email.message import Message
import urllib.request
import urllib.response
import threading
import urllib.parse
from unittest import mock

from zmxy import core, protocol as p
from zmxy.tasks import group_signin as gs


READY = '<a class="j-showCal" href="javascript:void(0)">签到</a>'
SIGNED = '<a class="j-showCal hassign">已签到</a>'
LOGIN_LINK = '<a href="/account/login?refer=group">登录</a>'


class GroupSigninTests(unittest.TestCase):
    def run_signin(self, responses, cookie='Pauth=offline-cookie', password=''):
        session = core.Session(p.ClientConfig(uid=1, username='offline', cookie=cookie, password=password))
        with mock.patch.object(gs, '_request', side_effect=responses) as request:
            result = gs.run(core.TaskContext(session, {}))
        return result, request

    def test_login_link_does_not_hide_ready_or_signed_entry(self):
        self.assertEqual(gs._page_state(LOGIN_LINK + READY, gs.GROUP_URL), 'ready')
        self.assertEqual(gs._page_state(LOGIN_LINK + SIGNED, gs.GROUP_URL), 'signed')

    def test_login_string_in_script_is_not_session_expiry(self):
        html = '<script>var loginUrl="/account/login";</script>' + READY
        self.assertEqual(gs._page_state(html, gs.GROUP_URL), 'ready')

    def test_login_url_in_query_is_not_redirect(self):
        self.assertEqual(gs._page_state(READY, gs.GROUP_URL + '?next=/account/login'), 'ready')

    def test_real_login_redirect_wins_over_entry(self):
        for url in ('https://my.4399.com/account/login', '/account/login?next=group',
                    'https://my.4399.com/account/login/', '/account/login/index'):
            with self.subTest(url=url):
                self.assertEqual(gs._page_state(READY, url), 'login')

    def test_no_signin_entry_does_not_submit(self):
        result, request = self.run_signin([(gs.GROUP_URL, LOGIN_LINK)])
        self.assertEqual(result.status, core.BLOCKED)
        self.assertIn('未返回签到入口', result.detail)
        self.assertEqual(request.call_count, 1)

    def test_ready_with_login_link_submits_exactly_once(self):
        result, request = self.run_signin([(gs.GROUP_URL, LOGIN_LINK + READY),
                                          (gs.SIGN_URL, '{"code":100,"result":{"userGrade":{"continueDay":3}}}')])
        self.assertEqual(result.status, core.SUBMITTED)
        self.assertIn('连续签到 3 天', result.detail)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args, (gs.SIGN_URL,))
        self.assertEqual(request.call_args.kwargs['method'], 'POST')
        self.assertEqual(request.call_args.kwargs['form'], {'sign': 1, 'tagid': gs.GROUP_ID, '_AJAX_': 1})

    def test_signed_with_login_link_never_posts(self):
        result, request = self.run_signin([(gs.GROUP_URL, LOGIN_LINK + SIGNED)])
        self.assertEqual(result.status, core.SKIPPED)
        self.assertEqual(request.call_count, 1)

    def test_expired_session_syncs_once_and_never_posts(self):
        login = ('https://my.4399.com/account/login', READY)
        result, request = self.run_signin([login, (gs.SYNC_URL, ''), login])
        self.assertEqual(result.status, core.BLOCKED)
        self.assertIn('同步后仍跳转', result.detail)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([c.args[0] for c in request.call_args_list], [gs.GROUP_URL, gs.SYNC_URL, gs.GROUP_URL])

    def test_sync_establishes_community_session_before_single_post(self):
        result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''),
                                          (gs.GROUP_URL, READY), (gs.SIGN_URL, '{"code":100}')])
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(sum(c.kwargs.get('method') == 'POST' for c in request.call_args_list), 1)
        jars = [c.kwargs['cookie_jar'] for c in request.call_args_list]
        self.assertTrue(all(jar is jars[0] for jar in jars))

    def test_sync_can_discover_already_signed_without_post(self):
        result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''),
                                          (gs.GROUP_URL, SIGNED)])
        self.assertEqual(result.status, core.SKIPPED)
        self.assertEqual(request.call_count, 3)

    def test_failed_sync_does_not_retry_or_post(self):
        result, request = self.run_signin([('/account/login', ''), gs._CommunityError('offline sync failure')])
        self.assertEqual(result.status, core.BLOCKED)
        self.assertEqual(request.call_count, 2)

    def test_sync_without_signin_entry_does_not_post(self):
        result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''), (gs.GROUP_URL, '')])
        self.assertEqual(result.status, core.BLOCKED)
        self.assertEqual(request.call_count, 3)

    def test_post_failure_after_sync_never_repeats_post_or_sync(self):
        result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''),
                                          (gs.GROUP_URL, READY), gs._CommunityError('offline post timeout')])
        self.assertEqual(result.status, core.BLOCKED)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(sum(c.kwargs.get('method') == 'POST' for c in request.call_args_list), 1)

    def test_stop_after_sync_never_rereads_group_or_posts(self):
        stop = threading.Event()
        session = core.Session(p.ClientConfig(uid=1, username='offline', cookie='Pauth=offline'))

        def respond(url, **kwargs):
            if url == gs.SYNC_URL:
                stop.set()
                return gs.SYNC_URL, ''
            return '/account/login', ''

        with mock.patch.object(gs, '_request', side_effect=respond) as request:
            result = gs.run(core.TaskContext(session, {}, stop=stop))
        self.assertEqual(result.status, core.SKIPPED)
        self.assertEqual(request.call_count, 2)

    def test_invalid_cookie_never_requests(self):
        for cookie in ('not-a-cookie', 'Pauth=offline\r\nInjected: invalid'):
            with self.subTest(cookie_kind='invalid'):
                result, request = self.run_signin([], cookie=cookie)
                self.assertEqual(result.status, core.BLOCKED)
                request.assert_not_called()

    def test_missing_cookie_never_requests(self):
        result, request = self.run_signin([], cookie='')
        self.assertEqual(result.status, core.BLOCKED)
        request.assert_not_called()

    def test_password_fallback_logs_in_then_posts_once(self):
        jar = gs._community_cookies('Pauth=offline-new')
        with mock.patch.object(gs, '_password_login', return_value=jar) as login:
            result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''),
                                              ('/account/login', ''), (gs.GROUP_URL, READY),
                                              (gs.SIGN_URL, '{"code":100}')], password='offline-pass')
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        login.assert_called_once()
        self.assertIs(request.call_args.kwargs['cookie_jar'], jar)
        self.assertEqual(sum(c.kwargs.get('method') == 'POST' for c in request.call_args_list), 1)

    def test_password_fallback_already_signed_skips_post(self):
        with mock.patch.object(gs, '_password_login', return_value=gs._community_cookies('Pauth=offline-new')):
            result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''),
                                              ('/account/login', ''), (gs.GROUP_URL, SIGNED)], password='offline-pass')
        self.assertEqual(result.status, core.SKIPPED)
        self.assertFalse(any(c.kwargs.get('method') == 'POST' for c in request.call_args_list))

    def test_password_login_without_saved_cookie(self):
        with mock.patch.object(gs, '_password_login', return_value=gs._community_cookies('Pauth=offline-new')) as login:
            result, request = self.run_signin([(gs.GROUP_URL, SIGNED)], cookie='', password='offline-pass')
        self.assertEqual(result.status, core.SKIPPED)
        login.assert_called_once()
        self.assertEqual(request.call_count, 1)

    def test_password_failure_never_retries_or_signs(self):
        with mock.patch.object(gs, '_password_login', side_effect=gs._CommunityError('offline login failed')) as login:
            result, request = self.run_signin([('/account/login', ''), (gs.SYNC_URL, ''),
                                              ('/account/login', '')], password='offline-pass')
        self.assertEqual(result.status, core.BLOCKED)
        login.assert_called_once()
        self.assertEqual(request.call_count, 3)

    def test_password_form_matches_community_contract(self):
        session = core.Session(p.ClientConfig(uid=1, username='offline', password='offline-pass'))
        sent = []

        def login_request(request, jar, timeout):
            sent.append(request)
            if request.get_method() == 'POST':
                jar.set_cookie(next(iter(gs._community_cookies('Pauth=offline-new'))))
            return 200, ''

        with mock.patch.object(gs, '_login_request', side_effect=login_request):
            jar = gs._password_login(core.TaskContext(session, {}), 15)
        self.assertTrue(any(c.name == 'Pauth' for c in jar))
        self.assertEqual([r.get_method() for r in sent], ['GET', 'POST'])
        self.assertEqual(sent[1].full_url, gs.LOGIN_URL)
        form = urllib.parse.parse_qs(sent[1].data.decode(), keep_blank_values=True)
        self.assertEqual(form['appId'], ['my'])
        self.assertRegex(form['mainDivId'][0], r'^\w+$')
        self.assertNotEqual(form['password'], ['offline-pass'])
        self.assertEqual(session.config.cookie, '')

    def test_http_200_login_error_is_not_success_without_auth_cookie(self):
        session = core.Session(p.ClientConfig(uid=1, username='offline', password='offline-pass'))
        with mock.patch.object(gs, '_login_request', return_value=(200, 'wrong mainDivId format')) as send:
            with self.assertRaises(gs._CommunityError):
                gs._password_login(core.TaskContext(session, {}), 15)
        self.assertEqual(send.call_count, 2)

    def test_login_captcha_and_wrong_password_stop_without_retry(self):
        session = core.Session(p.ClientConfig(uid=1, username='offline', password='offline-pass'))
        for body in ('密码错误', 'ptlogin/captcha.do?captchaId=offline-id'):
            with self.subTest(body_kind='login_denied'):
                with mock.patch.object(gs, '_login_request', side_effect=[(200, ''), (200, body)]) as send:
                    with self.assertRaises(gs._CommunityError):
                        gs._password_login(core.TaskContext(session, {}), 15)
                self.assertEqual(send.call_count, 2)

    def test_stop_on_login_frame_never_posts_credentials(self):
        stop = threading.Event()
        session = core.Session(p.ClientConfig(uid=1, username='offline', password='offline-pass'))

        def frame(request, jar, timeout):
            stop.set()
            return 200, ''

        with mock.patch.object(gs, '_login_request', side_effect=frame) as send:
            self.assertIsNone(gs._password_login(core.TaskContext(session, {}, stop=stop), 15))
        self.assertEqual(send.call_count, 1)

    def test_post_failure_is_never_retried(self):
        for response in ((gs.SIGN_URL, '<html>unknown</html>'),
                         (gs.SIGN_URL, '{"code":500}'),
                         ('https://my.4399.com/account/login', ''),
                         gs._CommunityError('offline timeout')):
            with self.subTest(response=type(response).__name__):
                result, request = self.run_signin([(gs.GROUP_URL, READY), response])
                self.assertEqual(result.status, core.BLOCKED)
                self.assertEqual(request.call_count, 2)

    def test_cookie_jar_respects_domain_and_secure_flag(self):
        jar = gs._community_cookies('Pauth=offline-platform; Uauth=offline-stale')
        for url in ('https://example.com/', 'http://my.4399.com/'):
            request = urllib.request.Request(url)
            jar.add_cookie_header(request)
            self.assertIsNone(request.get_header('Cookie'))

    def test_http_set_cookie_survives_redirect_and_replaces_stale_cookie(self):
        calls = []
        build = urllib.request.build_opener

        class Transport(urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                calls.append((request.full_url, request.get_header('Cookie', ''), request.get_method()))
                headers, code, body = Message(), 200, ''
                if len(calls) == 1:
                    code = 302
                    headers['Location'] = 'https://my.4399.com/account/login'
                    headers['Set-Cookie'] = 'community_seen=1; Domain=.4399.com; Path=/; Secure'
                elif request.full_url == gs.SYNC_URL:
                    headers['Set-Cookie'] = 'Uauth=offline-fresh; Domain=.4399.com; Path=/; Secure'
                elif request.full_url == gs.GROUP_URL:
                    body = READY
                elif request.full_url == gs.SIGN_URL:
                    body = '{"code":100}'
                response = urllib.response.addinfourl(io.BytesIO(body.encode()), headers, request.full_url, code)
                response.msg = 'OK'
                return response

        session = core.Session(p.ClientConfig(uid=1, username='offline',
                                             cookie='Pauth=offline-platform; Uauth=offline-stale'))
        with mock.patch.object(gs.urllib.request, 'build_opener', side_effect=lambda *handlers: build(Transport(), *handlers)):
            result = gs.run(core.TaskContext(session, {}))
        self.assertEqual(result.status, core.SUBMITTED, result.detail)
        self.assertEqual(len(calls), 5)
        self.assertIn('community_seen=1', calls[2][1])
        for url, cookie, method in calls[3:]:
            self.assertIn('Uauth=offline-fresh', cookie)
            self.assertNotIn('Uauth=offline-stale', cookie)
        self.assertEqual(sum(method == 'POST' for _, _, method in calls), 1)
        self.assertEqual(session.config.cookie, 'Pauth=offline-platform; Uauth=offline-stale')


if __name__ == '__main__':
    unittest.main()
