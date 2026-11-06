"""Regression for saved-proxy status and verified draft-to-save handoff; no real network."""
import argparse,copy,json,os,sys,tempfile,time
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,Mock

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
parser.add_argument('--probe-only',action='store_true')
args=parser.parse_args()
sys.path.insert(0,str(args.source))
with tempfile.TemporaryDirectory(prefix='muse-proxy-status-') as home:
 os.environ.update(MUSE2API_HOME=home,MUSE2API_PROFILE_ROOT=home,MUSE2API_KEY='fixture-only')
 import app
 original={'server':'socks5://127.0.0.1:1080','username':'fixture','password':'fixture-secret'}
 candidate={'server':'socks5://127.0.0.1:1081','username':'draft','password':'draft-secret'}
 @contextmanager
 def route(proxy):yield {'proxies':{}}
 session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
 response=Mock(status_code=200);response.json.return_value={'ip':'198.51.100.9'}
 session.get.return_value=response
 def account():
  a=app.store.add_account({'hatch_sess':'fixture-cookie-'+str(len(app.store.accounts))},'fixture')
  app.store.update_account(a['id'],proxy=copy.deepcopy(original),ok=True,note='healthy-fixture')
  return a['id']
 def test(aid,proxy):
  req=app.ProxyTestRequest(proxy=app.AccountProxy(**proxy))
  with patch.object(app,'request_proxy_context',route),patch('requests.Session',return_value=session),patch.object(app.engine,'renew_session_http',return_value={'status':'assigned'}):
   return app.test_account_proxy(aid,req)
 def save(aid,proxy):
  return app.patch_account(aid,app.AccountPatch(proxy=app.AccountProxy(**proxy)))
 aid=account();before=copy.deepcopy(app.store.get_account(aid))
 result=test(aid,original)
 stored=app.store.get_account(aid)
 print(json.dumps({'case':'saved_proxy_test_with_request_body','tested_saved_proxy':result['tested_saved_proxy'],
                   'status_persisted':stored.get('proxy_status',{}).get('proxy_reachable') is True,
                   'account_health_preserved':stored['ok']==before['ok']}),flush=True)
 assert stored.get('proxy_status',{}).get('proxy_reachable') is True
 assert result['tested_saved_proxy'] is True
 assert stored['proxy']==before['proxy'] and stored['ok']==before['ok']
 if args.probe_only:raise SystemExit(0)
 aid=account();before=copy.deepcopy(app.store.get_account(aid));before_bytes=Path(app.CFG.accounts_file).read_bytes()
 result=test(aid,candidate)
 assert not result['tested_saved_proxy'] and app.store.get_account(aid)==before
 assert Path(app.CFG.accounts_file).read_bytes()==before_bytes
 record=save(aid,candidate)
 assert record['proxy_status']['proxy_reachable'] and record['proxy_status']['checked_at']
 assert app.store.get_account(aid)['ok'] is None  # transport proof never marks browser health good
 assert 'draft-secret' not in json.dumps(record) and 'fixture-cookie' not in json.dumps(record)
 print('PASS: unsaved draft leaves account bytes unchanged; exact save adopts server-observed status')
 for change in ['password','server','cookies','expired']:
  aid=account();test(aid,candidate)
  new=copy.deepcopy(candidate)
  if change=='password':new['password']='different-secret'
  elif change=='server':new['server']='socks5://127.0.0.1:1082'
  elif change=='cookies':app.store.update_account(aid,cookies={'hatch_sess':'replaced-cookie'})
  else:app._PROXY_TEST_RECEIPTS[aid]['at']-=app._PROXY_TEST_RECEIPT_TTL+1
  record=save(aid,new)
  assert not record.get('proxy_status'),change
 print('PASS: different credentials, route, Cookie or expired proof never inherit success')
 aid=account();test(aid,candidate);save(aid,candidate)
 test(aid,candidate)
 assert app.store.get_account(aid)['proxy_status']['proxy_reachable']
 save(aid,candidate)
 assert app.store.get_account(aid)['proxy_status']['proxy_reachable']
 aid=account();test(aid,candidate);app.patch_account(aid,app.AccountPatch(proxy=None))
 assert aid not in app._PROXY_TEST_RECEIPTS
 aid=account();test(aid,candidate);app.del_account(aid)
 assert aid not in app._PROXY_TEST_RECEIPTS
 print('PASS: retesting same route and repeated saves retain status; remove/delete clear receipts')
 aid=account()
 response.status_code=502
 result=test(aid,candidate);record=save(aid,candidate)
 assert result['proxy_reachable'] is False and record['proxy_status']['proxy_reachable'] is False
 response.status_code=200
 print('PASS: failed tests remain failed after saving; never manufacture a green result')

# Exercise the actual account-row renderer, including empty and failed records.
import subprocess
html=(args.source/'admin.html').read_text()
start=html.index('function proxyStatus(');end=html.index('async function loadPool',start)
renderer=html[start:end]
script="const vm=require('node:vm');const renderer="+json.dumps(renderer)+";"+"""
const cases=[
 [{}, '代理未测试'],
 [{checked_at:1,proxy_reachable:true}, '代理已连通'],
 [{checked_at:1,proxy_reachable:false}, '代理未通过'],
 [{checked_at:1,proxy_reachable:null}, '代理未确认']
];
for(const [status,label] of cases){
 const ctx={account:{proxy:{server:'socks5://fixture:1080'},proxy_status:status},t:x=>x,esc:x=>String(x),fmtTime:x=>'checked'};
 const value=vm.runInNewContext(renderer+';proxyStatus(account)',ctx);
 if(!value.includes(label))throw Error('Wrong label: '+label);
}
console.log('PASS: empty, successful, failed and inconclusive proxy status rendering');
"""
done=subprocess.run(['node','-e',script],capture_output=True,text=True)
print(done.stdout,end='');assert done.returncode==0,done.stderr
