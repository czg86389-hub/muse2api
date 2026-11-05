"""Native login protocol tests use fake HTTP only; no OTP is ever sent."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_login import NativeLoginError, NativeLoginService, SITE


class Clock:
    now = 1800000000.0

    def __call__(self):
        return self.now


class Timer:
    def __init__(self, delay, callback):
        self.delay, self.callback = delay, callback
        self.started = self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class Response:
    def __init__(self, status=200, data=None, headers=None, cookies=None, callback=None):
        self.status_code = status
        self.data = {} if data is None else data
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.callback = callback

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, steps, clock):
        self.steps = list(steps)
        self.clock = clock
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.steps:
            raise AssertionError('Unexpected HTTP call')
        expected, response = self.steps.pop(0)
        assert url == SITE + expected, (url, expected)
        assert kwargs['allow_redirects'] is False
        if isinstance(response, Exception):
            raise response
        if response.callback:
            response.callback()
        for name, value in response.cookies.items():
            self.cookies.set(name, value, domain='muse.ai', path='/', expires=int(self.clock.now + 600))
        return response

    def close(self):
        self.closed = True


def bootstrap(state='started'):
    return ('/api/auth/native/restart', Response(201, {'flow_state': state, 'csrf_token': 'fixture-csrf-token'},
                                               cookies={'hatch_native_auth_state': 'private-state'}))


def otp(state='otp_sent'):
    return ('/api/auth/native/send-otp', Response(data={'registration_state': state}))


def confirm(data=None, **kwargs):
    return ('/api/auth/native/confirm-otp', Response(data=data, **kwargs))


class NativeLoginTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('No live network in tests'))
        self.network.start()
        self.clock = Clock()
        self.saved = []
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
        self.network.stop()

    def create(self, steps, **kwargs):
        session = Session(steps, self.clock)

        def save(cookies, expires, label, state):
            self.saved.append((dict(cookies), dict(expires), label, state))
            return {'id': 'new-account', 'label': label, 'session_ok': True,
                    'workspace_available': state.get('vm_state') != 'DISABLED',
                    'cookies': {'should': 'never be returned'}, 'csrf': 'private-csrf'}

        service = NativeLoginService(save, session_factory=lambda: session, clock=self.clock,
                                     timer_factory=Timer, **kwargs)
        self.services.append(service)
        return service, session

    def start(self, service):
        return service.start('person@example.com', 'fixture')['login_id']

    def test_success_requires_verified_session_and_uses_observed_protocol(self):
        service, session = self.create([bootstrap(), otp(),
            confirm({'auth_meta_session': {'frl_access_token': 'do-not-return'}}, cookies={'hatch_sess': 'login-secret'}),
            ('/api/session', Response(data={'status': 'assigned', 'vm_state': 'RUNNING'}, cookies={'hatch_vml': 'workspace-secret'}))])
        login_id = self.start(service)
        self.assertEqual(service.status(login_id)['state'], 'awaiting_code')
        session.cookies.set('unrelated', 'do-not-import', domain='other.example', path='/')
        result = service.verify(login_id, '123456')
        self.assertEqual(result['state'], 'authenticated')
        self.assertEqual(result['account']['id'], 'new-account')
        self.assertEqual(self.saved[0][0]['hatch_sess'], 'login-secret')
        self.assertEqual(self.saved[0][0]['hatch_vml'], 'workspace-secret')
        self.assertNotIn('unrelated', self.saved[0][0])
        self.assertNotIn('cookies', result['account'])
        self.assertNotIn('csrf', result['account'])
        self.assertNotIn('login-secret', str(result))
        self.assertEqual(session.headers['Sec-Fetch-Site'], 'same-origin')
        self.assertEqual(session.headers['Sec-Fetch-Mode'], 'cors')
        self.assertEqual(session.headers['Sec-Fetch-Dest'], 'empty')
        self.assertEqual(session.calls[1][2]['json'], {'contact_point': 'person@example.com'})
        self.assertEqual(session.calls[2][2]['json'], {'otp_code': '123456'})
        self.assertEqual(session.calls[2][2]['headers']['X-Hatch-CSRF-Token'], 'fixture-csrf-token')
        self.assertFalse(session.cookies)
        self.assertTrue(session.closed)
        self.assertEqual(len(self.saved), 1)
        service.verify(login_id, '123456')
        self.assertEqual(len(self.saved), 1, 'retrying success must not create duplicate accounts')

    def test_new_flow_runs_start_once(self):
        service, session = self.create([bootstrap('new'), ('/api/auth/native/start', Response(data={'flow_state': 'started'})), otp()])
        self.start(service)
        self.assertEqual(len(session.calls), 3)

    def test_password_required_stops_without_reset_or_confirmation(self):
        service, session = self.create([bootstrap(), otp('password_required')])
        result = service.start('person@example.com')
        self.assertEqual(result['state'], 'unsupported')
        self.assertIn('密码', result['message'])
        self.assertFalse(self.saved)
        self.assertFalse(session.cookies)

    def test_registration_and_extra_verification_never_automatically_complete(self):
        for state in ('two_factor_required', 'account_selection_required', 'completion_required', 'ntp_upsell', 'registration_required'):
            with self.subTest(state=state):
                service, session = self.create([bootstrap(), otp(), confirm({'registration_state': state}, cookies={'hatch_sess': 'temporary'})])
                login_id = self.start(service)
                self.assertEqual(service.verify(login_id, '123456')['state'], 'unsupported')
                self.assertEqual(len(session.calls), 3)
                self.assertFalse(session.cookies)
                self.assertFalse(self.saved)

    def test_success_is_proved_by_session_not_invented_registration_enum(self):
        service, session = self.create([bootstrap(), otp(),
            confirm({'registration_state': 'some_future_success_name'}, cookies={'hatch_sess': 'login-secret'}),
            ('/api/session', Response(data={'status': 'assigned', 'vm_state': 'RUNNING'}))])
        self.assertEqual(service.verify(self.start(service), '123456')['state'], 'authenticated')
        self.assertEqual(len(self.saved), 1)

    def test_unknown_confirmation_without_session_is_not_imported(self):
        service, session = self.create([bootstrap(), otp(), confirm({'registration_state': 'unexpected_state'})])
        self.assertEqual(service.verify(self.start(service), '123456')['state'], 'unsupported')
        self.assertFalse(self.saved)

    def test_unknown_send_state_does_not_request_confirmation(self):
        service, session = self.create([bootstrap(), otp('unknown')])
        self.assertEqual(service.start('person@example.com')['state'], 'unsupported')
        self.assertEqual(len(session.calls), 2)

    def test_code_errors_allow_five_attempts_without_leaking_upstream_body(self):
        replies = [confirm(status=400, data={'type': 'invalidCode', 'detail': 'OTP=123456 SECRET'}) for _ in range(5)]
        service, session = self.create([bootstrap(), otp(), *replies])
        login_id = self.start(service)
        for attempt in range(5):
            result = service.verify(login_id, '123456')
            self.assertEqual(result['state'], 'awaiting_code' if attempt < 4 else 'failed')
            self.assertNotIn('123456', str(result))
            self.assertNotIn('SECRET', str(result))
        self.assertFalse(session.cookies)
        self.assertFalse(self.saved)

    def test_resend_obeys_minute_cooldown_and_preserves_same_session(self):
        service, session = self.create([bootstrap(), otp(), ('/api/auth/native/resend-otp', Response(data={'registration_state': 'otp_sent'}))])
        login_id = self.start(service)
        with self.assertRaises(NativeLoginError) as error:
            service.resend(login_id)
        self.assertEqual(error.exception.status_code, 429)
        self.clock.now += 60
        self.assertEqual(service.resend(login_id)['state'], 'awaiting_code')
        self.assertEqual(session.calls[-1][2]['json'], {})

    def test_only_one_active_login_and_cancel_does_not_reset_send_rate(self):
        service, session = self.create([bootstrap(), otp()])
        login_id = self.start(service)
        with self.assertRaises(NativeLoginError) as error:
            service.start('other@example.com')
        self.assertEqual(error.exception.status_code, 409)
        service.cancel(login_id)
        with self.assertRaises(NativeLoginError) as error:
            service.start('other@example.com')
        self.assertEqual(error.exception.status_code, 429)
        self.assertFalse(session.cookies)

    def test_cookie_presence_alone_never_counts_as_success(self):
        service, session = self.create([bootstrap(), otp(), confirm(cookies={'hatch_sess': 'temporary'}),
                                      ('/api/session', Response(data={'status': 'unassigned'}))])
        login_id = self.start(service)
        self.assertEqual(service.verify(login_id, '123456')['state'], 'unsupported')
        self.assertFalse(self.saved)
        self.assertFalse(session.cookies)

    def test_meta_token_without_muse_cookie_requires_manual_completion(self):
        service, session = self.create([bootstrap(), otp(), confirm({'auth_meta_session': {'frl_access_token': 'sensitive'}})])
        login_id = self.start(service)
        result = service.verify(login_id, '123456')
        self.assertEqual(result['state'], 'unsupported')
        self.assertNotIn('sensitive', str(result))
        self.assertEqual(len(session.calls), 3)

    def test_disabled_workspace_is_saved_as_authenticated_but_unavailable(self):
        service, session = self.create([bootstrap(), otp(), confirm(cookies={'hatch_sess': 'new-login'}),
                                      ('/api/session', Response(data={'status': 'assigned', 'vm_state': 'DISABLED'}))])
        result = service.verify(self.start(service), '123456')
        self.assertEqual(result['state'], 'authenticated')
        self.assertFalse(result['account']['workspace_available'])
        self.assertIn('工作区不可用', result['message'])
        self.assertEqual(self.saved[0][3]['vm_state'], 'DISABLED')

    def test_cross_origin_redirect_never_receives_code_or_cookies(self):
        service, session = self.create([bootstrap(), otp(), confirm(status=302, headers={'Location': 'https://attacker.example/collect'})])
        self.assertEqual(service.verify(self.start(service), '123456')['state'], 'unsupported')
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(self.saved)

    def test_same_origin_completion_redirect_is_get_without_otp_body(self):
        service, session = self.create([bootstrap(), otp(),
            confirm(status=303, headers={'Location': '/thread/new'}, cookies={'hatch_sess': 'new-login'}),
            ('/thread/new', Response(data=ValueError('html'))),
            ('/api/session', Response(data={'status': 'assigned', 'vm_state': 'RUNNING'}))])
        self.assertEqual(service.verify(self.start(service), '123456')['state'], 'authenticated')
        self.assertEqual(session.calls[3][0], 'GET')
        self.assertNotIn('json', session.calls[3][2])

    def test_timer_expires_without_another_api_request(self):
        service, session = self.create([bootstrap(), otp()])
        login_id = self.start(service)
        flow = service._flows[login_id]
        timer = flow.timer
        self.assertTrue(timer.started and timer.daemon)
        timer.fire()
        self.assertEqual(flow.state, 'expired')
        self.assertEqual(flow.csrf, '')
        self.assertFalse(session.cookies)
        self.assertTrue(session.closed)

    def test_cancel_while_confirm_in_flight_prevents_import_and_clears_late_cookies(self):
        service, session = self.create([bootstrap(), otp()])
        login_id = self.start(service)
        session.steps.append(confirm(cookies={'hatch_sess': 'late'}, callback=lambda: service.cancel(login_id)))
        self.assertEqual(service.verify(login_id, '123456')['state'], 'expired')
        self.assertFalse(self.saved)
        self.assertFalse(session.cookies)

    def test_expiry_during_request_prevents_import(self):
        service, session = self.create([bootstrap(), otp()])
        login_id = self.start(service)
        session.steps.append(confirm(cookies={'hatch_sess': 'late'}, callback=lambda: setattr(self.clock, 'now', self.clock.now + 601)))
        self.assertEqual(service.verify(login_id, '123456')['state'], 'expired')
        self.assertFalse(self.saved)
        self.assertFalse(session.cookies)

    def test_shutdown_clears_temporary_state_and_cancels_timer(self):
        service, session = self.create([bootstrap(), otp()])
        login_id = self.start(service)
        timer = service._flows[login_id].timer
        service.shutdown()
        self.assertTrue(timer.cancelled)
        self.assertFalse(session.cookies)
        self.assertFalse(service._flows)
        with self.assertRaises(NativeLoginError) as error:
            service.start('person@example.com')
        self.assertEqual(error.exception.status_code, 503)

    def test_current_recovers_active_login_without_exposing_full_identifier(self):
        service, session = self.create([bootstrap(), otp()])
        self.assertEqual(service.current(), {'state': 'idle'})
        login_id = self.start(service)
        result = service.current()
        self.assertEqual(result['login_id'], login_id)
        self.assertEqual(result['state'], 'awaiting_code')
        self.assertEqual(result['contact_hint'], 'p***@example.com')
        self.assertNotIn('person@example.com', str(result))
        service.cancel(login_id)
        self.assertEqual(service.current(), {'state': 'idle'})

    def test_current_expires_stale_login(self):
        service, session = self.create([bootstrap(), otp()])
        login_id = self.start(service)
        self.clock.now += 601
        self.assertEqual(service.current(), {'state': 'idle'})
        self.assertFalse(session.cookies)

    def test_checkpoint_and_network_errors_are_safe_and_stop(self):
        for response in (Response(403, {'type': 'checkpointRequired', 'detail': 'private-token'}),
                         requests.ConnectionError('https://user:password@example.invalid/?otp=123456')):
            with self.subTest(kind=type(response).__name__):
                service, session = self.create([('/api/auth/native/restart', response)])
                result = service.start('person@example.com')
                self.assertIn(result['state'], ('unsupported', 'failed'))
                self.assertNotIn('private-token', str(result))
                self.assertNotIn('password', str(result))
                self.assertNotIn('123456', str(result))
                self.assertTrue(session.closed)

    def test_invalid_inputs_do_not_send_or_consume_attempts(self):
        service, session = self.create([bootstrap(), otp()])
        with self.assertRaises(NativeLoginError):
            service.start('invalid\nheader')
        self.assertFalse(session.calls)
        login_id = self.start(service)
        with self.assertRaises(NativeLoginError):
            service.verify(login_id, 'not-a-code')
        self.assertEqual(service._flows[login_id].attempts, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
