"""Full proxy URLs and non-persistent draft connectivity tests; no real credentials/network."""
import asyncio,copy,hashlib,json,os,sys,tempfile
from pathlib import Path
from unittest.mock import patch,Mock
from contextlib import contextmanager
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
with tempfile.TemporaryDirectory() as home:
 os.environ.update(MUSE2API_HOME=home,MUSE2API_PROFILE_ROOT=home,MUSE2API_KEY='fixture-only')
 import app
 from test_async_images import Client
 examples=[
 ('socks5://fixture:example-password@127.0.0.1:1080','socks5://127.0.0.1:1080','fixture','example-password'),
 ('socks5h://user:p%40ss%3A%2F%23%25@proxy.example:1080','socks5h://proxy.example:1080','user','p@ss:/#%'),
 ('http://us%65r:p%40ss@[::1]:8080','http://[::1]:8080','user','p@ss'),
 ('socks5://user:pass@proxy.example','socks5://proxy.example:1080','user','pass'),
 ('http://user:pass@proxy.example','http://proxy.example:80','user','pass')]
 for link,server,user,password in examples:
  model=app.AccountProxy(server=link,username='stale-manual-user',password='stale-manual-password')
  assert model.configuration()=={'server':server,'username':user,'password':password}
  assert model.model_dump(mode="json")["password"]=="**********" and "@" not in model.server
 print('PASS: full HTTP/SOCKS5/SOCKS5h URLs, encoded credentials, IPv6 and default ports')
 saved={'server':'socks5://127.0.0.1:1080','username':'fixture','password':'keep-secret'}
 assert app.AccountProxy(server=saved['server'],username='fixture').configuration(saved)==saved
 a=app.store.add_account({'hatch_sess':'synthetic-cookie'},'fixture')
 app.store.update_account(a['id'],proxy=saved,ok=True,proxy_status={'ok':True,'checked_at':1})
 before=copy.deepcopy(app.store.get_account(a['id']))
 before_bytes=Path(app.CFG.accounts_file).read_bytes()
 @contextmanager
 def fake_proxy(proxy):
  assert proxy['server']=='socks5://127.0.0.1:1081'
  assert proxy['username']=='draft' and proxy['password']=='draft-secret'
  yield {'proxies':{}}
 fake_response=Mock(status_code=200);fake_response.json.return_value={'ip':'198.51.100.7'}
 fake_session=Mock();fake_session.__enter__=Mock(return_value=fake_session);fake_session.__exit__=Mock(return_value=False)
 fake_session.get.return_value=fake_response
 async def api_checks():
  async with Client(app.app,{'Authorization':'Bearer fixture-only'}) as client:
   payload={'proxy':{'server':'socks5://draft:draft-secret@127.0.0.1:1081'}}
   with patch.object(app,'request_proxy_context',fake_proxy),patch('requests.Session',return_value=fake_session),patch.object(app.engine,'renew_session_http',return_value={'status':'assigned'}) as session:
    reply=await client.post('/admin/accounts/'+a['id']+'/proxy/test',json=payload)
   assert reply.status_code==200,reply.body
   result=reply.json();assert result['proxy_reachable'] and result['session_ok'] and not result['tested_saved_proxy']
   assert session.call_args.kwargs['proxy']=={'server':'socks5://127.0.0.1:1081','username':'draft','password':'draft-secret'}
   assert 'draft-secret' not in json.dumps(result)
   assert app.store.get_account(a['id'])==before
   assert Path(app.CFG.accounts_file).read_bytes()==before_bytes
   print('PASS: unsaved draft test uses entered route, redacts credentials and changes no account bytes')
   r=await client.request('PATCH','/admin/accounts/'+a['id'],json_data=payload)
   assert r.status_code==200,r.body
   public=r.json()
   assert public['proxy']['server']=='socks5://127.0.0.1:1081' and public['proxy']['has_password']
   assert 'draft-secret' not in json.dumps(public)
   assert app.store.get_account(a['id'])['proxy']['password']=='draft-secret'
   for bad in ['https://draft:do-not-echo@host:443','socks5://draft:do-not-echo@host:99999','socks5://draft:do-not-echo@host:1080/path']:
    r=await client.request('PATCH','/admin/accounts/'+a['id'],json_data={'proxy':{'server':bad}})
    assert r.status_code==422
    assert 'do-not-echo' not in json.dumps(r.json())
   print('PASS: full-URL save persists canonical fields and invalid-link errors do not reveal credentials')
 asyncio.run(api_checks())
