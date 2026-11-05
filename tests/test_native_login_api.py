"""Offline native-login API/auth/persistence checks; never sends an OTP."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix='muse-native-login-api-')
API_KEY = 'test-only-native-login-key'
OTP = '834927'
SESSION_COOKIE = 'native-session-secret-fixture'
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY=API_KEY, MUSE2API_CONCURRENCY='1')
with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network during import')):
    import app


async def request(method, path, payload=None, bearer=API_KEY):
    """Exercise the actual ASGI app without httpx, lifespan, sockets or HTTP."""
    body = json.dumps(payload).encode() if payload is not None else b''
    headers = {'content-type': 'application/json', 'content-length': str(len(body))}
    if bearer is not None:
        headers['authorization'] = 'Bearer ' + bearer
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'https', 'path': path,
             'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
             'headers': [(key.encode(), value.encode()) for key, value in headers.items()],
             'server': ('test', 443), 'client': ('test', 1)}
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    try:
        await app.app(scope, receive, send)
    except Exception:
        # ServerErrorMiddleware emits the safe 500 body, then rethrows for logging.
        if not any(message.get('status') == 500 for message in messages):
            raise
    status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
    data = b''.join(message.get('body', b'') for message in messages)
    return status, data.decode()


class NativeLoginApiTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts.clear()
        app.store.tasks.clear()
        app.engine._last_http_renew.clear()
        self.service = SimpleNamespace(**{
            method: Mock(return_value={'login_id': 'login-fixture', 'state': 'awaiting_code'})
            for method in ('start', 'verify', 'resend', 'status', 'cancel', 'current')
        })
        self.addCleanup(patch.stopall)
        patch.object(app, 'native_logins', self.service).start()
        patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network')).start()

    def response(self, method, path, payload=None, bearer=API_KEY):
        return asyncio.run(request(method, path, payload, bearer))

    def assert_no_secrets(self, payload):
        serialized = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        for secret in (API_KEY, OTP, SESSION_COOKIE, 'native-vml-secret-fixture'):
            self.assertNotIn(secret, serialized)

    def test_all_login_routes_require_valid_bearer(self):
        routes = [
            ('POST', '/admin/login/start', {'identifier': 'fixture@example.test'}),
            ('POST', '/admin/login/login-fixture/verify', {'code': OTP}),
            ('POST', '/admin/login/login-fixture/resend', None),
            ('GET', '/admin/login/current', None),
            ('GET', '/admin/login/login-fixture', None),
            ('DELETE', '/admin/login/login-fixture', None),
        ]
        for bearer in (None, 'incorrect-fixture-key'):
            for method, path, payload in routes:
                with self.subTest(bearer=bool(bearer), method=method, path=path):
                    status, body = self.response(method, path, payload, bearer)
                    self.assertEqual(status, 401)
                    self.assert_no_secrets(body)
        for method in vars(self.service).values():
            method.assert_not_called()

    def test_authenticated_routes_pass_only_expected_arguments(self):
        cases = [
            ('POST', '/admin/login/start', {'identifier': ' fixture@example.test ', 'label': ' Account A '},
             'start', ('fixture@example.test', 'Account A')),
            ('POST', '/admin/login/login-fixture/verify', {'code': OTP},
             'verify', ('login-fixture', OTP)),
            ('POST', '/admin/login/login-fixture/resend', None, 'resend', ('login-fixture',)),
            ('GET', '/admin/login/login-fixture', None, 'status', ('login-fixture',)),
            ('DELETE', '/admin/login/login-fixture', None, 'cancel', ('login-fixture',)),
        ]
        for verb, path, payload, method, arguments in cases:
            with self.subTest(method=method):
                status, body = self.response(verb, path, payload)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {'login_id': 'login-fixture', 'state': 'awaiting_code'})
                getattr(self.service, method).assert_called_once_with(*arguments)
                self.assert_no_secrets(body)

    def test_safe_service_error_preserves_status_without_echoing_otp(self):
        self.service.verify.side_effect = app.NativeLoginError(400, '验证码未通过，请重新输入')
        status, body = self.response('POST', '/admin/login/login-fixture/verify', {'code': OTP})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {'detail': '验证码未通过，请重新输入'})
        self.assert_no_secrets(body)

    def test_current_login_route_restores_flow_without_matching_dynamic_status(self):
        for public_state in ({'state': 'idle'},
                             {'login_id': 'login-fixture', 'state': 'awaiting_code'}):
            with self.subTest(state=public_state['state']):
                self.service.current.reset_mock()
                self.service.current.return_value = public_state
                status, body = self.response('GET', '/admin/login/current')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), public_state)
                self.service.current.assert_called_once_with()
                self.service.status.assert_not_called()
                self.assert_no_secrets(body)

    def test_rate_limit_error_is_not_converted_to_success(self):
        self.service.resend.side_effect = app.NativeLoginError(429, '请稍后再发送验证码')
        status, body = self.response('POST', '/admin/login/login-fixture/resend')
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body), {'detail': '请稍后再发送验证码'})
        self.assert_no_secrets(body)

    def test_unexpected_error_response_does_not_expose_secret_exception_text(self):
        self.service.verify.side_effect = RuntimeError('fixture upstream error ' + OTP + ' ' + API_KEY)
        status, body = self.response('POST', '/admin/login/login-fixture/verify', {'code': OTP})
        self.assertEqual(status, 500)
        self.assert_no_secrets(body)

    def test_code_validation_errors_never_echo_input(self):
        for code in ({'otp': OTP}, [OTP], int(OTP), None):
            with self.subTest(code_type=type(code).__name__):
                status, body = self.response('POST', '/admin/login/login-fixture/verify', {'code': code})
                self.assertEqual(status, 422)
                self.assert_no_secrets(body)
                errors = json.loads(body)['detail']
                self.assertTrue(all('input' not in error and 'ctx' not in error for error in errors))
        self.service.verify.assert_not_called()
        self.assertNotIn(OTP, repr(app.NativeLoginVerifyRequest(code=OTP)))

    def session(self, state='RUNNING'):
        return {'ok': True, 'status': 'assigned', 'vm_state': state}

    def credentials(self):
        return {'hatch_sess': SESSION_COOKIE, 'hatch_vml': 'native-vml-secret-fixture'}

    def test_save_verified_login_persists_credentials_but_returns_public_metadata(self):
        cookies = self.credentials()
        expires = {'hatch_sess': 2000000000, 'hatch_vml': 1900000000}
        result = app._save_native_login(cookies, expires, 'My account', self.session())
        saved = app.store.get_account(result['id'])
        self.assertEqual(saved['cookies'], cookies)
        self.assertEqual(saved['cookies_exp'], expires)
        self.assertTrue(result['session_ok'])
        self.assertTrue(result['workspace_available'])
        self.assertEqual(result['label'], 'My account')
        self.assert_no_secrets(result)
        self.assertNotIn('cookies', result)
        self.assertNotIn('api_key', result)
        persisted = json.loads(Path(app.CFG.accounts_file).read_text(encoding='utf-8'))
        self.assertEqual(persisted[0]['cookies'], cookies)

    def test_disabled_workspace_keeps_login_and_reports_unavailability(self):
        result = app._save_native_login(self.credentials(), {'hatch_sess': 2000000000},
                                        'Disabled workspace', self.session('DISABLED'))
        saved = app.store.get_account(result['id'])
        self.assertTrue(saved['enabled'])
        self.assertTrue(result['session_ok'])
        self.assertFalse(result['workspace_available'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['vm_state'], 'DISABLED')
        self.assertTrue(result['note'])
        self.assertEqual(saved['cookies'], self.credentials())
        self.assert_no_secrets(result)

    def test_other_accounts_are_unchanged_and_same_session_save_is_idempotent(self):
        unrelated = app.store.add_account({'hatch_sess': 'unrelated-fixture'}, label='Keep this account')
        unrelated.update(enabled=False, ok=None, quota={'weekly_used_pct': 22})
        before = copy.deepcopy(unrelated)
        first = app._save_native_login(self.credentials(), {'hatch_sess': 2000000000},
                                      'New account', self.session())
        app.store.get_account(first['id'])['enabled'] = False
        second = app._save_native_login(self.credentials(), {'hatch_sess': 2000000100},
                                       'Second label', self.session())
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(app.store.accounts), 2)
        self.assertFalse(second['enabled'], 'Reauthentication must preserve the user disable choice')
        self.assertEqual(app.store.get_account(first['id'])['cookies_exp']['hatch_sess'], 2000000100)
        self.assertEqual(app.store.get_account(unrelated['id']), before)
        self.assert_no_secrets(first)
        self.assert_no_secrets(second)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        HOME.cleanup()
