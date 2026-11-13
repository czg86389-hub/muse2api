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
from scheduler import ConcurrencyLock, SchedulerPolicy, SchedulerSettings
from fastapi.testclient import TestClient


class RuntimeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts = []
        app.store.tasks = {}
        self.accounts = [app.store.add_account({'hatch_sess': name}, name) for name in ['a', 'b', 'c']]
        self.policy = SchedulerPolicy(str(Path(HOME.name)/'settings-test.json'), lambda:app.store.accounts)
        self.policy.save(SchedulerSettings())
        self.patches = [patch.object(app,'SCHEDULER',self.policy),
                        patch.object(app,'GEN_LOCK',ConcurrencyLock(2, self.policy.limit)),
                        patch.object(app,'ACCOUNT_POOL',app.AccountPoolManager(app.store)),
                        patch.object(app,'_renew_and_persist',return_value=None),
                        patch.object(app,'_sync_cookies'), patch.object(app.engine,'start'),
                        patch.object(app.engine,'close_current_page'), patch.object(app.engine,'reset_thread')]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def test_live_resize_and_fifo(self):
        lock=ConcurrencyLock(2)
        self.assertTrue(lock.acquire());self.assertTrue(lock.acquire())
        entered=[];release=threading.Event()
        def worker(i):
            if lock.acquire(timeout=2):
                entered.append(i);release.wait(2);lock.release()
        workers=[]
        for i in range(2):
            t=threading.Thread(target=worker,args=(i,));t.start();workers.append(t)
            until=time.monotonic()+1
            while lock.snapshot()['waiting']!=i+1 and time.monotonic()<until:time.sleep(.005)
        lock.limit=3;lock.notify()
        until=time.monotonic()+1
        while not entered and time.monotonic()<until:time.sleep(.005)
        self.assertEqual(entered,[0])
        lock.limit=1;lock.notify();lock.release();lock.release()
        self.assertEqual(lock.snapshot()['active'],1)
        self.assertFalse(lock.acquire(blocking=False))
        release.set()
        for t in workers:t.join(2);self.assertFalse(t.is_alive())
        self.assertEqual(entered,[0,1]);self.assertFalse(lock.locked())

    def test_auto_counts_and_persistence(self):
        self.policy.save(SchedulerSettings(mode='auto',auto_limit=3))
        self.assertEqual(self.policy.limit(),3)
        app.store.update_account(self.accounts[0]['id'],enabled=False)
        app.store.update_account(self.accounts[1]['id'],ok=False)
        self.assertEqual(self.policy.limit(),1)
        app.store.update_account(self.accounts[2]['id'],cookies_exp={'hatch_sess':time.time()-1})
        self.assertEqual(self.policy.limit(),0)
        loaded=SchedulerPolicy(str(self.policy.path),lambda:app.store.accounts)
        self.assertEqual(loaded.settings()['mode'],'auto')
        self.assertEqual(loaded.limit(),0)

    def test_scheduler_api_auth_and_validation(self):
        c=TestClient(app.app)
        self.assertEqual(c.get('/admin/scheduler').status_code,401)
        c.headers['Authorization']='Bearer scheduler-test'
        before=c.get('/admin/scheduler').json()
        self.assertEqual(c.put('/admin/scheduler',json={'mode':'auto','auto_limit':0}).status_code,422)
        self.assertEqual(c.get('/admin/scheduler').json()['mode'],before['mode'])
        self.assertEqual(c.put('/admin/scheduler',json={'mode':'auto','auto_limit':3}).status_code,200)
        self.assertEqual(c.get('/admin/scheduler').json()['effective_limit'],3)

if __name__=="__main__":
    unittest.main(verbosity=2)
