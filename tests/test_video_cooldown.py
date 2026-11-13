"""Video-only, reversible cooldown policy and account lease regression."""
import os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HOME=tempfile.TemporaryDirectory(prefix='muse-video-health-test-')
os.environ.update(MUSE2API_HOME=HOME.name,MUSE2API_PROFILE_ROOT=HOME.name,MUSE2API_KEY='test-only')
import app
from video_health import VideoHealth,VideoHealthSettings

class VideoCooldownTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts=[]
        self.accounts=[app.store.add_account({'hatch_sess':n},n) for n in ['a','b']]
        policy=VideoHealth(str(Path(HOME.name)/'policy.json'));policy.save(VideoHealthSettings())
        self.patches=[patch.object(app,'VIDEO_HEALTH',policy),patch.object(app,'ACCOUNT_POOL',app.AccountPoolManager(app.store)),
          patch.object(app,'_renew_and_persist',return_value=None),patch.object(app,'_sync_cookies'),
          patch.object(app.engine,'start'),patch.object(app.engine,'close_current_page'),patch.object(app.engine,'reset_thread')]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def test_refusal_is_terminal_and_only_video_is_cooled(self):
        aid=self.accounts[0]['id']
        with patch.object(app.engine,'generate',side_effect=app.MuseModelRefusalError('视频生成地区不可用，403')) as generate:
            with self.assertRaises(app.MuseModelRefusalError):app._run_generation('x','video',600,account_id=aid)
        self.assertEqual(generate.call_count,1)
        account=app.store.get_account(aid)
        self.assertGreater(account['video_cooldown_until'],time.time())
        self.assertTrue(app.VIDEO_HEALTH.eligible(account,'image'));self.assertTrue(app.VIDEO_HEALTH.eligible(account,'chat'))
        self.assertFalse(app.VIDEO_HEALTH.eligible(account,'video'))
        leased=app.ACCOUNT_POOL.acquire(kind='video');self.assertNotEqual(leased['id'],aid);app.ACCOUNT_POOL.release(leased['id'])
        leased=app.ACCOUNT_POOL.acquire(account_id=aid,kind='image');app.ACCOUNT_POOL.release(leased['id'])
        app.reset_video_cooldown(aid);self.assertTrue(app.VIDEO_HEALTH.eligible(app.store.get_account(aid)))

    def test_generic_refusal_requires_streak_and_expiry_recovers(self):
        a=self.accounts[0]
        a.update(app.VIDEO_HEALTH.refusal_updates(a,'仅有文字，无媒体'));self.assertTrue(app.VIDEO_HEALTH.eligible(a))
        a.update(app.VIDEO_HEALTH.refusal_updates(a,'仅有文字，无媒体'));self.assertFalse(app.VIDEO_HEALTH.eligible(a))
        a['video_cooldown_until']=time.time()-1;self.assertTrue(app.VIDEO_HEALTH.eligible(a))
        a.update(app.VIDEO_HEALTH.reset_updates());self.assertEqual(a['video_refusal_count'],0)

    def test_disable_applies_immediately_and_persists(self):
        a=self.accounts[0];a['video_cooldown_until']=time.time()+900
        app.VIDEO_HEALTH.save(VideoHealthSettings(video_cooldown=0))
        self.assertTrue(app.VIDEO_HEALTH.eligible(a))
        loaded=VideoHealth(app.VIDEO_HEALTH.path);self.assertEqual(loaded.settings()['video_cooldown'],0)
        self.assertEqual(loaded.status(self.accounts)['cooling_accounts'],[])

    def test_all_cooled_fails_without_starting_browser(self):
        for a in self.accounts:a['video_cooldown_until']=time.time()+900
        with patch.object(app.engine,'generate') as generate:
            with self.assertRaisesRegex(app.MuseGenerationError,'冷却'):app._run_generation('x','video',600)
        generate.assert_not_called();self.assertFalse(app.GEN_LOCK.locked());self.assertFalse(app.ACCOUNT_POOL.busy_accounts)

if __name__=='__main__':unittest.main(verbosity=2)
