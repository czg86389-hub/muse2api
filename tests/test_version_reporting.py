"""Offline scheduling, budget, persistence and provenance regressions."""
import json
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix='muse-scheduler-test-')
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY='scheduler-test', MUSE2API_AUTO_UPDATE='0')
import app


class VersionReportingTests(unittest.TestCase):
    def test_version_check_never_upgrades_or_masks_version(self):
        app._UPDATE_CACHE['ts']=0
        def get(url,**kwargs):
            body={'version':'1.6.10'} if 'raw.githubusercontent' in url else [{'sha':'abcdef01234','commit':{'message':'fix','committer':{'date':'2026-11-13'}}}]
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:body)
        with patch.object(app,'_read_local_version',return_value={'version':'1.6.9'}),patch.dict(os.environ,{'MUSE2API_BUILD_SHA':'1234567890'}),patch('requests.get',side_effect=get),patch.object(app,'_silent_upgrade_worker') as upgrade:
            result=app._check_update_sync(force=True)
        self.assertEqual(result['local_version'],'1.6.9');self.assertEqual(result['remote_version'],'1.6.10')
        self.assertEqual(result['local_sha'],'1234567');self.assertTrue(result['has_update']);self.assertFalse(result['up_to_date'])
        upgrade.assert_not_called()
        with patch('requests.get',side_effect=ConnectionError('offline')):
            result=app._check_update_sync(force=True)
        self.assertIsNone(result['up_to_date']);self.assertTrue(result['check_error'])

    def test_managed_upgrade_is_blocked(self):
        with patch.object(app,'MANAGED_DEPLOYMENT',True),patch('requests.get') as get:
            with self.assertRaises(app.HTTPException) as cm:app._upgrade_from_github_sync()
        self.assertEqual(cm.exception.status_code,409);get.assert_not_called()

    def test_failed_upgrade_does_not_record_remote_as_installed(self):
        target='a'*40
        marker=Path(HOME.name)/'failed-installed-sha'
        broken=SimpleNamespace(check_returncode=lambda:(_ for _ in ()).throw(RuntimeError('git failed')))
        app.store.tasks={}
        with patch.object(app,'MANAGED_DEPLOYMENT',False),patch.object(app,'_check_update_sync',return_value={'remote_commit':target}), \
             patch.object(app,'_ensure_git_repo'),patch.object(app,'_git',return_value=broken), \
             patch.object(app,'_installed_sha_file',return_value=str(marker)),patch('requests.get',return_value=SimpleNamespace(status_code=503)):
            with self.assertRaises(app.HTTPException) as cm:app._upgrade_from_github_sync()
        self.assertEqual(cm.exception.status_code,502)
        self.assertFalse(marker.exists())


if __name__=="__main__":unittest.main(verbosity=2)
