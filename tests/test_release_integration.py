"""Combined-release checks: real provenance, queue API and mixed resource ordering."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
HOME=tempfile.TemporaryDirectory(prefix='m2a-release-integration-')
os.environ.update(MUSE2API_HOME=HOME.name,MUSE2API_PROFILE_ROOT=HOME.name,MUSE2API_KEY='fixture-only')
import app
import requests
from store import Store
from test_media_delete import Client

COMMIT='b'*40

def response(data):
    r=requests.Response();r.status_code=200;r._content=json.dumps(data).encode();return r

def remote(url,**kwargs):
    if 'raw.githubusercontent' in url:
        return response({'version':'1.6.11','highlights':['fixture'],'highlights_en':['fixture']})
    return response([{'sha':COMMIT,'commit':{'message':'fixture','committer':{'date':'2026-11-15'}},
                      'html_url':'https://example.invalid/commit'}])

class ReleaseIntegrationTests(unittest.TestCase):
    def test_remote_metadata_is_bound_to_the_reported_commit(self):
        seen=[]
        def get(url,**kw):seen.append(url);return remote(url,**kw)
        with patch('requests.get',side_effect=get),patch.object(app,'_read_local_version',return_value={'version':'1.6.10'}),patch.dict(os.environ,{'MUSE2API_BUILD_SHA':'a'*40}):
            data=app._check_update_sync(force=True)
        self.assertEqual(data['local_version'],'1.6.10')
        self.assertEqual(data['remote_commit'],COMMIT)
        self.assertTrue(any('/'+COMMIT+'/version.json' in url for url in seen),seen)

    def test_git_worktree_reports_its_real_commit(self):
        expected=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()[:7]
        with patch('requests.get',side_effect=remote),patch.dict(os.environ,{'MUSE2API_BUILD_SHA':''}):
            data=app._check_update_sync(force=True)
        self.assertTrue(data['has_git'])
        self.assertEqual(data['local_sha'],expected)

    def test_new_modules_are_in_docker_and_maintainer_copy_lists(self):
        docker=(ROOT/'Dockerfile').read_text()
        for name in ['generation_timing.py','thumbnails.py']:
            self.assertIn(name,docker);self.assertIn(name,app.TRACKED_REPO_PATHS)
        for name in ['GENERATION_QUEUE.md','VERSION_REPORTING.md']:
            self.assertIn(name,app.TRACKED_REPO_PATHS)

    def test_queue_put_preflight_is_allowed(self):
        async def check():
            async with Client(app.app,{}) as client:
                r=await client.request('OPTIONS','/admin/generation-queue',headers={
                    'origin':'https://example.invalid',
                    'access-control-request-method':'PUT',
                    'access-control-request-headers':'authorization,content-type'})
                self.assertEqual(r.status_code,200,r.body)
                r=await client.request('PUT','/admin/generation-queue',body=b'{"queue_timeout":60}',headers={'content-type':'application/json'})
                self.assertEqual(r.status_code,401)
        asyncio.run(check())

    def test_waiting_chat_does_not_reserve_a_media_account(self):
        store=Store(app.CFG);store.accounts=[];store.tasks={}
        account=store.add_account({'hatch_sess':'fixture'},'fixture')
        pool=app.AccountPoolManager(store);lock=app.ConcurrencyLock(1)
        waiting=threading.Event();errors=[]
        class TrackedLock:
            def acquire(self,*a,**kw):waiting.set();return lock.acquire(*a,**kw)
            def release(self):return lock.release()
            def __enter__(self):self.acquire();return self
            def __exit__(self,*a):self.release()
        def consume():
            try:list(app.safe_chat_stream(account['cookies'],'fixture',None,2,account['id']))
            except BaseException as ex:errors.append(ex)
        with patch.object(app,'store',store),patch.object(app,'ACCOUNT_POOL',pool),patch.object(app,'GEN_LOCK',TrackedLock()), \
             patch.object(app,'_renew_and_persist',return_value=None),patch.object(app,'_sync_cookies'), \
             patch.object(app.engine,'start'),patch.object(app.engine,'close_current_page'), \
             patch.object(app.engine,'chat_stream',return_value=iter(['ok'])):
            lock.acquire();thread=threading.Thread(target=consume,daemon=True);thread.start()
            leased=False
            try:
                self.assertTrue(waiting.wait(2),'chat did not reach slot acquisition')
                try:
                    pool.acquire(account_id=account['id'],timeout=.08);leased=True
                except app.MuseGenerationError:
                    pass
            finally:
                if leased:pool.release(account['id'])
                lock.release();thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors,errors)
            self.assertTrue(leased,'chat reserved an account while waiting for the media-held slot')
            self.assertFalse(lock.locked());self.assertFalse(pool.busy_accounts)

    def test_chat_cleanup_failure_releases_resources(self):
        store=Store(app.CFG);store.accounts=[];store.tasks={}
        account=store.add_account({'hatch_sess':'fixture'},'fixture')
        pool=app.AccountPoolManager(store);lock=app.ConcurrencyLock(1)
        with patch.object(app,'store',store),patch.object(app,'ACCOUNT_POOL',pool),patch.object(app,'GEN_LOCK',lock), \
             patch.object(app,'_renew_and_persist',return_value=None),patch.object(app,'_sync_cookies'), \
             patch.object(app.engine,'start'),patch.object(app.engine,'close_current_page',side_effect=RuntimeError('fixture cleanup')), \
             patch.object(app.engine,'chat_stream',return_value=iter(['ok'])):
            self.assertEqual(list(app.safe_chat_stream(account['cookies'],'fixture',None,2,account['id'])),['ok'])
            self.assertFalse(lock.locked());self.assertFalse(pool.busy_accounts)

    def test_active_tasks_reject_manual_upgrade_before_network(self):
        with patch.object(app,'MANAGED_DEPLOYMENT',False),patch.object(app.store,'tasks',{'fixture':{'status':'queued'}}),patch('requests.get')as get:
            with self.assertRaises(app.HTTPException)as cm:app._upgrade_from_github_sync()
        self.assertEqual(cm.exception.status_code,409);get.assert_not_called()

if __name__=='__main__':
    unittest.main(verbosity=2)
