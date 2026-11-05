"""Offline workspace-disabled and page-preparation regressions; no live accounts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as module
from engine import MuseEngine, MuseGenerationError, MuseWorkspaceUnavailableError


NOTICE = ('Muse is disconnected\nWe disconnected Muse to protect your information. '
          'Submit a feedback ticket to reconnect.')


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, seconds):
        self.now += seconds


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.engine = MuseEngine(SimpleNamespace(profile_dir=self.home.name, login_wait=1))
        self.clock = Clock()
        self.time_patch = patch.object(module, 'time', self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def page(self, disconnected=False, readiness='ready'):
        def js(script):
            if script == self.engine._WORKSPACE_UNAVAILABLE_JS:
                return disconnected() if callable(disconnected) else disconnected
            if 'document.readyState' in script:
                return readiness() if callable(readiness) else readiness
            if '(function(forChat)' in script:
                return True
            return None
        page = SimpleNamespace(js=Mock(side_effect=js), send=Mock(), close=Mock())
        self.engine.page = page
        return page

    def test_session_disabled_blocks_before_browser_navigation(self):
        self.clock.now = 1000  # Beyond the existing per-account renewal cache window.
        session = {'ok': True, 'vm_state': 'DISABLED', 'cookies': {'fixture': 'renewed'}}
        with patch.object(self.engine, 'renew_session_http', return_value=session), \
                patch.object(self.engine, '_open_page') as open_page:
            with self.assertRaises(MuseWorkspaceUnavailableError) as caught:
                self.engine.ensure_page({'fixture': 'original'}, account_id='fixture-account')
        self.assertIn('提交反馈', str(caught.exception))
        self.assertNotIsInstance(caught.exception, module.MuseAuthError)
        open_page.assert_not_called()

    def test_http_renew_retains_disabled_session_payload_without_wake(self):
        response = SimpleNamespace(status_code=200, cookies=[], json=lambda: {
            'status': 'assigned', 'vm_id': 'fixture-vm', 'vm_state': 'DISABLED'})
        with patch('requests.get', return_value=response), patch('requests.post') as post:
            result = MuseEngine.renew_session_http({'hatch_sess': 'fixture'})
        self.assertEqual(result['vm_state'], 'DISABLED')
        self.assertEqual(result['cookies']['hatch_sess'], 'fixture')
        self.assertFalse(result['wake_requested'])
        post.assert_not_called()

    def test_disconnected_notice_requires_specific_upstream_message(self):
        for body, expected in ((NOTICE, True), ('Connecting...', False),
                               ('Network disconnected, reconnecting', False),
                               ('Muse is disconnected', False), ('', False)):
            script = ('const document={body:{innerText:' + json.dumps(body) + '}};'
                      'console.log(JSON.stringify(' + self.engine._WORKSPACE_UNAVAILABLE_JS + '));')
            result = subprocess.check_output(['node', '-e', script], text=True).strip()
            self.assertEqual(json.loads(result), expected, body)

    def test_initial_textarea_flash_does_not_override_disconnected_state(self):
        page = self.page(disconnected=lambda: self.clock.now >= 0.2)
        with self.assertRaises(MuseWorkspaceUnavailableError):
            self.engine._wait_ws_ready(page, timeout=1)
        self.assertLess(self.clock.now, 1)

    def test_transient_ready_then_missing_never_succeeds(self):
        page = self.page(readiness=lambda: 'ready' if self.clock.now < 0.2 else 'no-ta')
        self.assertFalse(self.engine._wait_ws_ready(page, timeout=1))

    def test_stable_ready_page_remains_supported(self):
        self.assertTrue(self.engine._wait_ws_ready(self.page(), timeout=1))
        self.assertGreaterEqual(self.clock.now, 0.5)

    def test_reset_timeout_stops_generation_before_send(self):
        page = self.page(readiness='no-ta')
        with patch.object(self.engine, 'ensure_page', return_value=page), \
                patch.object(self.engine, '_send') as send:
            with self.assertRaisesRegex(MuseGenerationError, '已停止发送'):
                self.engine.generate({}, 'fixture not submitted')
        send.assert_not_called()
        page.send.assert_called_once_with('Page.navigate', {'url': 'https://muse.ai/thread/new'})

    def test_reset_disconnected_exception_is_not_swallowed(self):
        page = self.page(disconnected=True)
        with self.assertRaises(MuseWorkspaceUnavailableError):
            self.engine.reset_thread()
        page.send.assert_not_called()

    def test_send_rechecks_workspace_without_typing_or_submitting(self):
        page = self.page(disconnected=True)
        with self.assertRaises(MuseWorkspaceUnavailableError):
            self.engine._send('fixture not submitted')
        page.send.assert_not_called()

    def test_send_missing_composer_does_not_type_or_submit(self):
        page = self.page(readiness='no-ta')
        with self.assertRaisesRegex(MuseGenerationError, '已停止发送'):
            self.engine._send('fixture not submitted')
        page.send.assert_not_called()

    def test_quota_accepts_direct_dialog_and_menu_without_duplicate_clicks(self):
        for direct in (True, False):
            with self.subTest(direct=direct):
                clicks = []
                def js(script):
                    if script == self.engine._WORKSPACE_UNAVAILABLE_JS:
                        return False
                    if script == self.engine._QUOTA_SETTINGS_JS:
                        return '{"x":10,"y":20}'
                    if script == self.engine._QUOTA_MENU_JS:
                        return '{"x":30,"y":40}' if not direct and len(clicks) == 1 else None
                    if script == self.engine._QUOTA_DIALOG_JS:
                        visible = len(clicks) >= (1 if direct else 2)
                        return 'Settings\nUsage\nFree plan\n7% used' if visible else ''
                    return None
                page = SimpleNamespace(js=Mock(side_effect=js))
                def ensure(*args, **kwargs):
                    self.engine.page = page
                with patch.object(self.engine, '_close_page') as close, \
                        patch.object(self.engine, 'ensure_page', side_effect=ensure), \
                        patch.object(self.engine, '_click_point', side_effect=lambda x,y: clicks.append((x,y))):
                    result = self.engine.quota({'fixture': 'value'})
                self.assertEqual(result['weekly_used_pct'], 7)
                self.assertEqual(clicks, [(10,20)] if direct else [(10,20),(30,40)])
                self.assertEqual(close.call_count, 2)

    def test_quota_stops_when_disconnected_notice_appears_after_settings(self):
        clicks = []
        def js(script):
            if script == self.engine._WORKSPACE_UNAVAILABLE_JS:
                return bool(clicks)
            if script == self.engine._QUOTA_SETTINGS_JS:
                return '{"x":10,"y":20}'
            return ''
        page = SimpleNamespace(js=Mock(side_effect=js))
        def ensure(*args, **kwargs):
            self.engine.page = page
        with patch.object(self.engine, '_close_page') as close, \
                patch.object(self.engine, 'ensure_page', side_effect=ensure), \
                patch.object(self.engine, '_click_point', side_effect=lambda x,y: clicks.append((x,y))):
            with self.assertRaises(MuseWorkspaceUnavailableError):
                self.engine.quota({'fixture': 'value'})
        self.assertEqual(clicks, [(10,20)])
        self.assertEqual(close.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
