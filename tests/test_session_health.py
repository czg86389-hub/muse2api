"""Offline regression: python tests/test_session_health.py [project_dir] [--observe]."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


def check(root, home, observe):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY='test-only',
                      REDIS_URL='memory://', MUSE2API_REDIS_URL='memory://')
    sys.path.insert(0, str(root))
    import app
    import requests
    checks = 0

    def expect(condition, message):
        nonlocal checks
        checks += 1
        if not observe:
            assert condition, message

    def account(prior=True, expiry=True):
        a = app.store.add_account({'hatch_sess': 'fixture'}, cookies_exp={'hatch_vml': 1900012345} if expiry else {})
        a.update(ok=prior, last_keepalive=1899999900, synced_at=1899999900)
        return a

    def response(code, body=None):
        def decode():
            if isinstance(body, Exception):
                raise body
            return body
        jar = requests.cookies.RequestsCookieJar()
        jar.set('hatch_sess', 'renewed-fixture', expires=1900020000)
        return SimpleNamespace(status_code=code, json=decode, cookies=jar)

    cases = [('401', response(401)), ('403', response(403)), ('429', response(429)),
             ('503', response(503)), ('redirect', response(302)),
             ('non_json', response(200, ValueError('fixture'))),
             ('non_object', response(200, [])), ('unassigned', response(200, {'status': 'unassigned'})),
             ('timeout', requests.Timeout('fixture')), ('connection', requests.ConnectionError('fixture')),
             ('assigned', response(200, {'status': 'assigned', 'vm_state': 'RUNNING'}))]
    protected = ('cookies', 'cookies_exp', 'expires_at', 'synced_at', 'last_keepalive')
    for prior in (True, None, False):
        for name, reply in cases:
            a = account(prior)
            before = copy.deepcopy(a)
            with patch('requests.get', side_effect=reply if isinstance(reply, Exception) else None, return_value=reply) as get:
                result = app._probe_account_sync(a['id'])
            expected_ok = True if name == 'assigned' else False if name == '401' else prior
            expect(a['ok'] is expected_ok, f'{name}: account state changed incorrectly')
            expect(result['ok'] is (name == 'assigned'), f'{name}: wrong probe result')
            expect(get.call_args.kwargs.get('allow_redirects') is False, 'session must not follow redirects')
            if name != 'assigned':
                expect(all(a[k] == before[k] for k in protected), f'{name}: modified protected session data')
            else:
                expect(a['cookies']['hatch_sess'] == 'renewed-fixture', 'renewed cookie not saved')
                expect(a['last_keepalive'] == 1900000000, 'successful keepalive not recorded')
                expect(a['expires_at'] == 1900012345, 'expiry fabricated on success')
            if name in ('401', '403', '429', '503'):
                expect('HTTP ' + name in a['note'], 'HTTP diagnostic missing')
            print(json.dumps({'case': name, 'prior': prior, 'result_ok': result['ok'], 'stored_ok': a['ok'],
                              'protected_unchanged': all(a[k] == before[k] for k in protected)}, sort_keys=True))

    for name in ('401', '403', '503', 'assigned'):
        a = account()
        before = copy.deepcopy(a)
        reply = next(r for n, r in cases if n == name)
        auth_error = False
        with patch('requests.get', return_value=reply):
            try:
                app._renew_and_persist(a['id'], force=True)
            except app.MuseAuthError:
                auth_error = True
        expect(auth_error is (name == '401'), name + ': pre-renew wrong exception')
        expect(a['ok'] is (name != '401'), name + ': pre-renew wrong account state')
        if name in ('403', '503'):
            expect(all(a[k] == before[k] for k in protected), 'failed pre-renew changed session data')
        print('pre_renew', name, 'auth_error', auth_error, 'stored_ok', a['ok'])

    for prior in (True, None, False):
        a = account(prior)
        before = copy.deepcopy(a)
        app.store.touch_keepalive(a['id'], None, 'fixture diagnostic')
        expect(a['ok'] is prior and all(a[k] == before[k] for k in protected), 'unknown touch changed state')
    a = account(expiry=False)
    before_expiry = a['expires_at']
    with patch('time.time', return_value=1900003600):
        app.store.touch_keepalive(a['id'], True)
    expect(a['expires_at'] == before_expiry, 'estimated expiry advanced without renewed cookie')

    for error in (app.MuseAuthError('fixture auth'), app.MuseGenerationError('fixture loading')):
        for prior in (True, None, False):
            a = account(prior)
            with patch.object(app.engine, 'start'), patch.object(app.engine, 'refresh', side_effect=error):
                asyncio.run(app.test_account(a['id']))
            want = False if isinstance(error, app.MuseAuthError) else prior
            expect(a['ok'] is want, 'manual test misclassified account')

    for body, want in [('loading workspace', 'MuseGenerationError'), ('Sign in', 'MuseAuthError')]:
        page = SimpleNamespace(js=lambda script: body if 'innerText' in script else False,
                               send=lambda *a, **k: None, close=lambda: None)
        e = app.MuseEngine(SimpleNamespace(profile_dir=home, login_wait=0))
        reply = next(r for n, r in cases if n == 'assigned')
        with patch.object(e, '_open_page', return_value=page), patch.object(e, '_apply_cookies'), patch('requests.get', return_value=reply):
            try:
                e.ensure_page({'hatch_sess': 'fixture'})
                error_name = 'none'
            except Exception as exc:
                error_name = type(exc).__name__
        expect(error_name == want, 'page load failure misclassified')
        print('page', body, error_name)
    print(('OBSERVED' if observe else 'PASS'), checks, 'checks; no live accounts/network/browser')


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--observe']
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as home, patch('time.time', return_value=1900000000), patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network')):
        check(root, home, '--observe' in sys.argv)
