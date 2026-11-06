"""Video continuation safety tests with synthetic media; no real account or network."""
import asyncio,hashlib,os,sys,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from video_extend import extend_prompt,valid_thread,valid_locator,VIDEO_SNAPSHOT_JS
from engine import MuseEngine,MuseGenerationError,MuseSubmissionUncertainError,_mp4_duration


def mp4(seconds):
    def box(name,data):return (len(data)+8).to_bytes(4,'big')+name+data
    mvhd=b'\x00'*12+(1000).to_bytes(4,'big')+int(seconds*1000).to_bytes(4,'big')
    return box(b'ftyp',b'isom0000')+box(b'moov',box(b'mvhd',mvhd))

assert valid_thread('/thread/ab123456-0000-0000-0000-000000000000')
for s in ['/thread/new','https://evil/thread/ab123456','/thread/../../secret','/thread/ab123456?x=1']:
    assert not valid_thread(s)
for s in ['sandbox://workspace/../evil.mp4','sandbox://workspace/a%2fb.mp4','sandbox://workspace/a.mp4?x','sandbox://other/a.mp4']:
    assert not valid_locator(s)
assert valid_locator('sandbox://workspace/output.mp4')
assert 'sandbox://workspace/output.mp4' in extend_prompt('sandbox://workspace/output.mp4')

with tempfile.TemporaryDirectory() as root:
    cfg=SimpleNamespace(profile_dir=root,media_dir=root)
    eng=MuseEngine(cfg);source=Path(root)/'source.mp4';source.write_bytes(mp4(10))
    output=Path(root)/'extended.mp4';output.write_bytes(mp4(20))
    thread='/thread/ab123456-0000-0000-0000-000000000000'
    video={'output':'sandbox://workspace/original.mp4','size':len(source.read_bytes()),'src':'blob:fixture','message':'assistant-id'}
    snapshot={'host':'muse.ai','path':thread,'ready':True,'busy':False,'messages':[{'id':'assistant-id','videos':[video]}]}
    class Page:
        def send(self,*args,**kwargs):return {}
        def js(self,*args,**kwargs):return snapshot
    eng.page=Page();eng.ensure_page=lambda *a,**kw:eng.page;eng._wait_ws_ready=lambda *a,**kw:True;eng._scroll_bottom=lambda:None
    eng.extract_bytes=lambda *a,**kw:(source.read_bytes(),'video/mp4','blob:fixture')
    events=[]
    def generate(prompt,kind,deadline,progress,stop,hashes,on_sent,on_before):
        assert kind=='video' and hashlib.sha256(source.read_bytes()).hexdigest() in hashes
        assert 'sandbox://workspace/original.mp4' in prompt and 'follow the river' in prompt
        on_before();on_sent({'provider_thread':thread});events.append('sent')
        return {'path':str(output),'filename':output.name,'size':output.stat().st_size,'kind':'video'}
    eng._generate_attached=generate
    r=eng.extend_video({'hatch_sess':'fixture'},thread,str(source),'follow the river',timeout=20,account_id='fixture',on_before_submission=lambda:events.append('before'),on_submission=lambda ev:events.append('confirmed'))
    assert events==['before','confirmed','sent'] and r['duration_verified'] and r['duration']>r['source_duration']
    events.clear();eng.extract_bytes=lambda *a,**kw:(b'wrong','video/mp4','blob:fixture')
    try:eng.extend_video({},thread,str(source),timeout=20,account_id='fixture',on_before_submission=lambda:events.append('before'))
    except MuseGenerationError as exc:assert '不一致' in str(exc)
    else:raise AssertionError('wrong source accepted')
    assert not events
    print('PASS: same-account continuation validates original bytes, thread and output duration before marking success')

async def routes():
    with tempfile.TemporaryDirectory() as home:
        os.environ.update(MUSE2API_HOME=home,MUSE2API_PROFILE_ROOT=home,MUSE2API_KEY='fixture',MUSE2API_PUBLIC_BASE='https://fixture.example')
        import app as m
        assert Path(m.CFG.base_dir) == Path(home), 'test must use an isolated data directory'
        m.CFG.api_key = 'fixture'
        from test_async_images import Client
        acc=m.store.add_account({'hatch_sess':'fixture'},'fixture')
        parent=m.store.create_task('video','source')
        name='fixture.mp4';(Path(m.CFG.media_dir)/name).write_bytes(mp4(10))
        m.store.update_task(parent['id'],status='completed',account=acc['id'],provider_thread='/thread/ab123456-0000-0000-0000-000000000000',result={'filename':name})
        c=Client(m.app,{'authorization':'Bearer fixture'})
        route='/v1/videos/'+parent['id']+'/extend'
        with patch.object(m,'_run_video_extension',lambda *args: None):
            first=await c.post(route,json={'instruction':'slowly pan left'});assert first.status_code==200,first.body
            second=await c.post(route,json={});assert second.status_code==409
        child=m.store.get_task(first.json()['id']);assert child['account']==acc['id'] and child['source_task_id']==parent['id']
        try:m.store.delete_task(child['id'])
        except ValueError:pass
        else:raise AssertionError('active continuation deleted')
        m.store.clear_tasks(keep=0);assert m.store.get_task(child['id'])
        m.store.update_task(child['id'],status='needs_review',submission_started=True)
        assert (await c.post('/v1/videos/'+child['id']+'/resolve-review',json={})).status_code==400
        assert (await c.post('/v1/videos/'+child['id']+'/resolve-review',json={'confirmed_checked':True})).status_code==200
        print('PASS: authenticated API reserves one extension per thread and protects unresolved submissions')
asyncio.run(routes())
