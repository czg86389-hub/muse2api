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
from generation_timing import QueuePolicy, QueueSettings, TaskTiming



class GenerationQueueTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts=[];app.store.tasks={}
        self.accounts=[app.store.add_account({"hatch_sess":n},n) for n in ["a","b","c"]]
        policy=QueuePolicy(str(Path(HOME.name)/"queue-test.json"));policy.save(QueueSettings())
        self.patches=[patch.object(app,"QUEUE_POLICY",policy),patch.object(app,"GEN_LOCK",app.ConcurrencyLock(2)),
          patch.object(app,"ACCOUNT_POOL",app.AccountPoolManager(app.store)),patch.object(app,"_renew_and_persist",return_value=None),
          patch.object(app,"_sync_cookies"),patch.object(app.engine,"start"),patch.object(app.engine,"close_current_page"),patch.object(app.engine,"reset_thread")]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def test_queue_wait_does_not_consume_generation_budget(self):
        clock=[0.0];seen={};real_pool=app.ACCOUNT_POOL
        class QueuedLock:
            def acquire(self,timeout):clock[0]+=515;return True
            def release(self):seen['released']=True
        def lease(**kwargs):clock[0]+=20;return real_pool.acquire(**kwargs)
        def generate(*args,**kwargs):seen['timeout']=kwargs['timeout'];return {'filename':'x.mp4'}
        queues=[]
        with patch.object(app,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time)), \
             patch.object(app,'GEN_LOCK',QueuedLock()),patch.object(real_pool,'acquire',wraps=real_pool.acquire) as acquire, \
             patch.object(app.engine,'generate',side_effect=generate):
            original=acquire._mock_wraps
            def delayed(**kwargs):clock[0]+=20;return original(**kwargs)
            acquire.side_effect=delayed
            app._run_generation('x','video',600,on_queue=queues.append)
        self.assertEqual(seen['timeout'],600)
        self.assertEqual(queues[-1],535)
        self.assertTrue(seen['released']);self.assertFalse(real_pool.busy_accounts)

    def test_retry_account_wait_does_not_consume_generation_budget(self):
        clock=[0.0];timeouts=[];real_acquire=app.ACCOUNT_POOL.acquire
        def lease(**kwargs):clock[0]+=50 if not timeouts else 90;return real_acquire(**kwargs)
        def generate(*args,**kwargs):
            timeouts.append(kwargs['timeout'])
            if len(timeouts)==1:clock[0]+=30;raise app.MuseAuthError('expired login')
            return {'filename':'x.mp4'}
        with patch.object(app,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time)), \
             patch.object(app.ACCOUNT_POOL,'acquire',side_effect=lease),patch.object(app.engine,'generate',side_effect=generate):
            app._run_generation('x','video',600)
        self.assertEqual(timeouts,[600,570])

    def test_original_timeout_is_not_overwritten(self):
        clock=[0.0];calls=[]
        def generate(*args,**kwargs):clock[0]+=605;calls.append(1);raise app.MuseGenerationError('original upstream timeout')
        with patch.object(app,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time)),patch.object(app.engine,'generate',side_effect=generate):
            with self.assertRaisesRegex(app.MuseGenerationError,'original upstream timeout'):
                app._run_generation('x','video',600)
        self.assertEqual(calls,[1]);self.assertFalse(app.GEN_LOCK.locked());self.assertFalse(app.ACCOUNT_POOL.busy_accounts)

    def test_queue_failure_never_generates(self):
        with patch.object(app.GEN_LOCK,'acquire',return_value=False),patch.object(app.engine,'generate') as generate:
            with self.assertRaisesRegex(app.MuseGenerationError,'排队时限'):app._run_generation('x','video',1)
        generate.assert_not_called()

    def test_failed_job_keeps_account_and_durations(self):
        with patch.object(app.engine,'generate',side_effect=app.MuseModelRefusalError('只有文字，无媒体')):
            result=asyncio.run(app.create_video(app.VideoRequest(prompt='x')))
            until=time.monotonic()+2
            while app.store.get_task(result['id'])['status'] in ['queued','processing'] and time.monotonic()<until:time.sleep(.01)
        task=app.store.get_task(result['id'])
        self.assertEqual(task['status'],'failed');self.assertIn(task['account'],[a['id'] for a in self.accounts])
        self.assertIn('只有文字',task['last_error']);self.assertEqual(task['attempt'],1)
        self.assertGreaterEqual(task['total_elapsed'],task['elapsed']);self.assertIn('queue_elapsed',task)

if __name__=="__main__":unittest.main(verbosity=2)
