"""Real Chromium dashboard checks with synthetic accounts; no Muse login or external API."""
import argparse,base64,os,json,shutil,socket,signal,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cdp import CDP
parser=argparse.ArgumentParser();parser.add_argument('--html',default=str(ROOT/'admin.html'));parser.add_argument('--screenshots',type=Path);parser.add_argument('--probe-only',action='store_true');args=parser.parse_args()
html=Path(args.html).read_bytes()
VERSION=json.loads((ROOT/'version.json').read_text())['version']
now=int(time.time())
accounts=[{'id':'fixture'+str(i).zfill(5),'label':'Fixture-'+str(i),'enabled':True,'ok':True,'essential_ok':True,'cookie_count':12,
 'proxy':None,'note':'会话有效 · 自动保活 (VM: RUNNING)','last_used':now-43200,'expires_at':now+172800,
 'quota':{'found':True,'plan':'Free plan','weekly_used_pct':29,'extra_left':'1B tokens left','checked_at':now-10000}} for i in range(8)]
events=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  path=urlsplit(self.path).path
  if path in ['/','/admin']:data=html;kind='text/html; charset=utf-8'
  elif path=='/admin/accounts':data={'accounts':accounts,'stats':{'total':8,'healthy':8}};kind='application/json'
  elif path=='/admin/apikey':data={'api_key':'fixture-only'};kind='application/json'
  elif path in ['/admin/update/check','/admin/repo/status']:
   data={'local_version':VERSION,'remote_version':VERSION,'local_sha':'fixture','remote_sha':'fixture','up_to_date':True,'has_update':False,'recent_commits':[]};kind='application/json'
  else:data={'browser_running':True,'media_count':0,'status':'ok'};kind='application/json'
  body=data if isinstance(data,bytes) else json.dumps(data).encode()
  self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
  events.append({'method':'POST','path':urlsplit(self.path).path,'body':body})
  data=json.dumps({'ok':True,'proxy_reachable':True,'session_ok':True,'exit_ip':'198.51.100.7','workspace_status':'assigned','tested_saved_proxy':False}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
with tempfile.TemporaryDirectory(prefix='muse-layout-',dir=Path.home()) as home:
 log=open(Path(home)/'chrome.log','wb')
 proc=subprocess.Popen([shutil.which('chromium') or shutil.which('google-chrome'),'--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-allow-origins=*',
 '--remote-debugging-port='+str(port),'--user-data-dir='+str(Path(home)/'browser'),'about:blank'],stdout=log,stderr=log,start_new_session=True)
 page=None
 try:
  import requests
  session=requests.Session();session.trust_env=False
  for _ in range(150):
   try:
    tab=next(x for x in session.get(f'http://127.0.0.1:{port}/json/list',timeout=1).json() if x.get('type')=='page')
    page=CDP(tab['webSocketDebuggerUrl'],timeout=15);break
   except (requests.RequestException,StopIteration):time.sleep(.1)
  assert page
  page.send('Page.enable');page.send('Runtime.enable')
  page.send('Page.addScriptToEvaluateOnNewDocument',{'source':"localStorage.setItem('muse2api_lang',localStorage.getItem('muse2api_lang')||'zh');window.__errors=[];window.addEventListener('error',e=>__errors.push(e.message));"})
  page.send('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/?key=fixture-only'})
  def wait(expr):
   end=time.monotonic()+15
   while time.monotonic()<end:
    if page.js(expr):return
    time.sleep(.1)
   raise AssertionError('Condition failed: '+expr)
  wait("document.querySelectorAll('#poolBody tr').length===8")
  measure="""(()=>{const m=document.querySelector('main'),w=document.querySelector('#poolBody').closest('.tblwrap'),n=document.querySelector('#poolBody td.ellip');return {viewport:innerWidth,mainWidth:m.getBoundingClientRect().width,bodyWidth:document.documentElement.scrollWidth,tableWidth:w.clientWidth,tableScroll:w.scrollWidth,noteDisplay:getComputedStyle(n).display,noteWhiteSpace:getComputedStyle(n).whiteSpace,buttons:document.querySelectorAll('#poolBody tr:first-child button').length}})()"""
  for lang in (['zh'] if args.probe_only else ['zh','en']):
   if lang=='en':
    page.js("localStorage.setItem('muse2api_lang','en');location.reload()");wait("document.querySelectorAll('#poolBody tr').length===8")
   for width in ([1920] if args.probe_only else [2560,1920,1536,1440,1366,1280,768,390,320]):
    page.send('Emulation.setDeviceMetricsOverride',{'width':width,'height':1080,'deviceScaleFactor':1,'mobile':False})
    time.sleep(.15);m=page.js(measure);print(json.dumps({'lang':lang,**m}),flush=True)
    assert m['mainWidth']>=width-24,'Page does not fill the viewport'
    assert m['bodyWidth']<=width+1,'Page overflows the viewport'
    if width>=1280:assert m['tableScroll']<=m['tableWidth']+1,'Desktop account controls need horizontal scrolling'
    assert m['noteDisplay']=='table-cell' and m['noteWhiteSpace']=='normal','Notes must retain table-cell layout and wrap'
    assert m['buttons']==8
    if width==1920 and not args.probe_only and args.screenshots:
     page.js("document.querySelector('#poolBody').scrollIntoView({block:'end'})")
     image=page.send('Page.captureScreenshot',{'format':'png'})
     args.screenshots.mkdir(parents=True,exist_ok=True)
     (args.screenshots/('layout-'+lang+'.png')).write_bytes(base64.b64decode(image['result']['data']))
   if not args.probe_only:
    page.send('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':False})
    page.js("openProxy('fixture00000');$('proxyMode').value='proxy';updateProxyMode();$('proxyServer').value='socks5://draft:draft-secret@127.0.0.1:1081';$('proxyServer').dispatchEvent(new Event('input',{bubbles:true}))")
    assert page.js("!$('proxyTest').disabled && !document.querySelector('.proxy-auth').open")
    page.js("$('proxyTest').click()");wait("$('proxyResult').innerText.includes('198.51.100.7') && !$('proxyTest').disabled")
    assert events[-1]['body']['proxy']['server']=='socks5://draft:draft-secret@127.0.0.1:1081'
    assert page.js("!accounts[0].proxy && $('proxyDialog').getBoundingClientRect().right<=innerWidth")
    assert all(e['path'].endswith('/proxy/test') for e in events)
    page.js("$('proxyDialog').close()")
    wait("$('proxyServer').value==='' && $('proxyPassword').value===''")
  if not args.probe_only:
   assert not page.js('window.__errors')
   print('PASS: full-width layout, desktop controls, contained mobile scrolling, Chinese/English and test-without-saving')
 finally:
  if page:
   try:page.send('Browser.close')
   except Exception:pass
   page.close()
  try:os.killpg(proc.pid,signal.SIGTERM)
  except ProcessLookupError:pass
  try:proc.wait(timeout=8)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
  try:os.killpg(proc.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  time.sleep(.2);log.close();server.shutdown();server.server_close()
