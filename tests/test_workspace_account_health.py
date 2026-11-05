"""Offline checks for valid login + disabled workspace and rotated quota cookies."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_home = tempfile.TemporaryDirectory(prefix="muse-workspace-account-tests-")
os.environ.update(MUSE2API_HOME=_home.name, MUSE2API_PROFILE_ROOT=_home.name,
                  MUSE2API_KEY="test-only", MUSE2API_CONCURRENCY="1")
import app


class WorkspaceAccountTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts.clear()
        app.store.tasks.clear()
        app.engine._last_http_renew.clear()
        self.acc = app.store.add_account({'hatch_sess': 'old-fixture'}, label='fixture')
        self.aid = self.acc['id']
        self.acc.update(enabled=True, ok=True, quota={'found': True, 'weekly_used_pct': 12})
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.assertEqual(app.ACCOUNT_POOL.busy_accounts, set())

    def session(self, state='DISABLED'):
        return {'ok': True, 'status': 'assigned', 'vm_state': state,
                'cookies': {'hatch_sess': 'renewed-fixture'},
                'cookies_exp': {'hatch_sess': 2000000000}, 'wake_ok': False}

    def assert_unavailable(self):
        self.assertTrue(self.acc['enabled'], 'User enable/disable choice must remain unchanged')
        self.assertFalse(self.acc['ok'])
        self.assertTrue(self.acc['session_ok'])
        self.assertFalse(self.acc['workspace_available'])
        self.assertIn('Muse', self.acc['note'])
        self.assertNotIn('会话正常', self.acc['note'])

    def test_disabled_pre_renew_persists_credentials_then_raises(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()):
            with self.assertRaises(app.MuseWorkspaceUnavailableError):
                app._renew_and_persist(self.aid, force=True)
        self.assert_unavailable()
        self.assertEqual(self.acc['cookies']['hatch_sess'], 'renewed-fixture')
        self.assertEqual(self.acc['cookies_exp']['hatch_sess'], 2000000000)
        self.assertEqual(self.acc['vm_state'], 'DISABLED')
        self.assertGreater(self.acc['synced_at'], 0)

    def test_keepalive_never_overwrites_disabled_with_healthy(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()), \
             patch.object(app.engine, 'start') as start:
            for _ in range(2):
                result = app._probe_account_sync(self.aid, check_quota=True)
                self.assertFalse(result['ok'])
                self.assertTrue(result['session_ok'])
                self.assertFalse(result['workspace_available'])
                self.assert_unavailable()
            start.assert_not_called()
        self.assertEqual(self.acc['cookies']['hatch_sess'], 'renewed-fixture')

    def test_manual_test_reports_workspace_instead_of_login_failure(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()), \
             patch.object(app.engine, 'start') as start:
            response = asyncio.run(app.test_account(self.aid))
            result = json.loads(response.body)
            self.assertFalse(result['ok'])
            self.assertTrue(result['session_ok'])
            self.assertFalse(result['workspace_available'])
            start.assert_not_called()
        self.assert_unavailable()

    def test_disabled_quota_fails_fast_without_overwriting_cached_quota(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()), \
             patch.object(app.engine, 'quota') as quota:
            with self.assertRaises(app.HTTPException) as caught:
                asyncio.run(app.query_quota(self.aid))
            self.assertEqual(caught.exception.status_code, 502)
            quota.assert_not_called()
        self.assert_unavailable()
        self.assertEqual(self.acc['quota']['weekly_used_pct'], 12)

    def test_generation_does_not_mark_disabled_healthy_or_start_browser(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()), \
             patch.object(app.engine, 'start') as start, patch.object(app.engine, 'generate') as generate:
            with self.assertRaises(app.MuseWorkspaceUnavailableError):
                app._run_generation('fixture', 'image', timeout=5)
            start.assert_not_called()
            generate.assert_not_called()
        self.assert_unavailable()

    def test_chat_does_not_mark_disabled_healthy(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()), \
             patch.object(app.engine, 'chat_stream') as chat:
            with self.assertRaises(app.MuseWorkspaceUnavailableError):
                list(app.safe_chat_stream(self.acc['cookies'], 'fixture', None, 5, self.aid))
            chat.assert_not_called()
        self.assert_unavailable()

    def test_quota_uses_and_persists_renewed_credentials(self):
        def quota(cookies, expires, account_id=None):
            self.assertEqual(cookies['hatch_sess'], 'renewed-fixture')
            self.assertEqual(app.store.get_account(account_id)['cookies'], cookies)
            self.assertEqual(expires['hatch_sess'], 2000000000)
            return {'found': True, 'weekly_used_pct': 20}

        with patch.object(app.engine, 'renew_session_http', return_value=self.session('RUNNING')) as renew, \
             patch.object(app.engine, 'start'), patch.object(app.engine, 'quota', side_effect=quota):
            for _ in range(2):
                self.assertEqual(asyncio.run(app.query_quota(self.aid))['weekly_used_pct'], 20)
            self.assertEqual(renew.call_count, 1, 'Second quota read should safely reuse saved credentials')
        self.assertTrue(self.acc['ok'])
        self.assertTrue(self.acc['workspace_available'])

    def test_page_disconnected_overrides_assigned_running_in_quota_keepalive(self):
        with patch.object(app.engine, 'renew_session_http', return_value=self.session('RUNNING')), \
             patch.object(app.engine, 'start'), \
             patch.object(app.engine, 'quota', side_effect=app.MuseWorkspaceUnavailableError()):
            result = app._probe_account_sync(self.aid, check_quota=True)
        self.assertFalse(result['ok'])
        self.assert_unavailable()

    def test_real_auth_failure_clears_previous_valid_session_flag(self):
        self.acc.update(session_ok=True, workspace_available=False)
        with patch.object(app.engine, 'renew_session_http', side_effect=app.MuseAuthError('fixture HTTP 401')):
            with self.assertRaises(app.MuseAuthError):
                app._renew_and_persist(self.aid, force=True)
        self.assertFalse(self.acc['session_ok'])
        self.assertTrue(self.acc['enabled'])

    def test_disabled_then_unknown_clears_unavailable_marker_and_persists(self):
        for state in ('STARTING', ''):
            with self.subTest(state=state):
                with patch.object(app.engine, 'renew_session_http', return_value=self.session()):
                    with self.assertRaises(app.MuseWorkspaceUnavailableError):
                        app._renew_and_persist(self.aid, force=True)
                self.assert_unavailable()
                with patch.object(app.engine, 'renew_session_http', return_value=self.session(state)):
                    app._renew_and_persist(self.aid, force=True)
                self.assertTrue(self.acc['session_ok'])
                self.assertTrue(self.acc['enabled'])
                self.assertIsNone(self.acc['workspace_available'])
                saved = json.loads(Path(app.store.cfg.accounts_file).read_text(encoding='utf-8'))
                account = next(entry for entry in saved if entry['id'] == self.aid)
                self.assertIsNone(account['workspace_available'])

    def test_keepalive_summary_all_disabled_reports_zero_success(self):
        app.store.add_account({'hatch_sess': 'second-fixture'})
        with patch.object(app.engine, 'renew_session_http', return_value=self.session()), \
             patch.object(app.asyncio, 'sleep', new_callable=AsyncMock):
            result = asyncio.run(app.run_keepalive_all(force=True))
        self.assertEqual(result['refreshed_count'], 2)
        self.assertEqual(result['succeeded_count'], 0)
        self.assertEqual(result['failed_count'], 2)
        self.assertTrue(all(not entry['ok'] for entry in result['refreshed']))

    def test_keepalive_summary_mixed_results_counts_success_and_failure(self):
        app.store.add_account({'hatch_sess': 'running-fixture'})

        def renew(cookies, *args, **kwargs):
            return self.session('RUNNING' if cookies['hatch_sess'] == 'running-fixture' else 'DISABLED')

        with patch.object(app.engine, 'renew_session_http', side_effect=renew), \
             patch.object(app.asyncio, 'sleep', new_callable=AsyncMock):
            result = asyncio.run(app.run_keepalive_all(force=True))
        self.assertEqual(result['refreshed_count'], 2)
        self.assertEqual(result['succeeded_count'], 1)
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual([entry['ok'] for entry in result['refreshed']], [False, True])


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _home.cleanup()
