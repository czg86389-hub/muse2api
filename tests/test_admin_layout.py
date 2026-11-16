"""Chromium UI checks: provenance, manual updates, lazy video and layout; fixtures only."""
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
network=[]
server_state={'mode':'update'}
feature_errors=[]
def check(value,message):
 if not value:feature_errors.append(message)

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  path=urlsplit(self.path).path
  network.append({'method':'GET','path':path})
  if path in ['/','/admin']:data=html;kind='text/html; charset=utf-8'
  elif path=='/admin/accounts':data={'accounts':accounts,'stats':{'total':8,'healthy':8}};kind='application/json'
  elif path=='/admin/apikey':data={'api_key':'fixture-only'};kind='application/json'
  elif path in ['/admin/update/check','/admin/repo/status']:
   known=server_state['mode']=='update'
   data={'repo_url':'https://github.com/czg86389-hub/muse2api','repo_ref':'main','local_version':'1.6.10','remote_version':VERSION if known else '', 'local_sha':'aaaaaaa','remote_sha':'bbbbbbb' if known else '', 'up_to_date':False if known else None,'has_update':known,'check_error':'' if known else 'fixture remote unavailable','managed_deployment':False,'highlights':['fixture update'],'highlights_en':['fixture update'],'recent_commits':[]};kind='application/json'
  elif path=='/admin/media':
   data={'count':2,'media':[{'name':name,'kind':'video','url':'/v1/media/'+name,'thumbnail_url':'/v1/media/'+name+'/thumbnail','bytes':4000,'mtime':now}for name in ['clip-one.mp4','clip-two.mp4']]};kind='application/json'
  elif path=='/admin/tasks':
   data={'tasks':[{'id':'fixture-task','kind':'video','prompt':'fixture task','status':'failed','error':'fixture failure','created_at':now-7,'account':'fixture00000','queue_elapsed':2.0,'elapsed':5.0,'total_elapsed':7.0,'attempt':1}]};kind='application/json'
  elif path.endswith('/thumbnail'):data=thumbnail_bytes;kind='image/jpeg'
  elif path.endswith('.mp4'):data=video_bytes;kind='video/mp4'
  else:data={'browser_running':True,'media_count':2,'status':'ok','generation_queue':{'queue_timeout':3600},'config':{'image_timeout':240,'video_timeout':600}};kind='application/json'
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
 ffmpeg=shutil.which('ffmpeg')
 assert ffmpeg, 'Install FFmpeg for media UI integration checks'
 clip=Path(home)/'fixture.mp4';thumb=Path(home)/'fixture.jpg'
 subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10','-t','1','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-y',str(clip)],check=True)
 subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(clip),'-frames:v','1','-threads','1','-y',str(thumb)],check=True)
 video_bytes=clip.read_bytes();thumbnail_bytes=thumb.read_bytes()
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
  page.send('Page.addScriptToEvaluateOnNewDocument',{'source':"localStorage.setItem('muse2api_lang',localStorage.getItem('muse2api_lang')||'zh');window.__confirms=[];window.confirm=(m)=>{window.__confirms.push(m);return false};window.__errors=[];window.addEventListener('error',e=>__errors.push(e.message));"})
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
   network.clear()
   server_state['mode']='update'
   page.js("loadRepoStatus(true)",await_promise=True)
   check('1.6.10' in page.js("$('repoBadge').textContent"),lang+': installed version was replaced by remote')
   check(page.js("$('updateBanner').classList.contains('show')"),lang+': update notification banner is hidden')
   check(not any(x['path']=='/admin/update/upgrade' for x in events),lang+': update check posted an upgrade')
   page.js("window.__confirms=[];upgradeNow()")
   wait("window.__confirms.length===1")
   if lang=='en':check('Update and restart' in page.js("window.__confirms[0]"),'en: manual confirmation is not localized')
   check(not any(x['path']=='/admin/update/upgrade' for x in events),lang+': cancelled confirmation still upgraded')
   page.js("goTab('media')")
   wait("document.querySelectorAll('#mediaGrid .tile').length===2")
   check(page.js("document.querySelectorAll('#mediaGrid video').length===0"),lang+': video elements preload in media list')
   check(not any(x['path'].endswith('.mp4') for x in network),lang+': listing downloaded original video')
   page.js("document.querySelector('#mediaGrid [data-play-video]').scrollIntoView({block:'center'})")
   rect=page.js("(()=>{const r=document.querySelector('#mediaGrid [data-play-video]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
   page.send('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','clickCount':1,**rect})
   page.send('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','clickCount':1,**rect})
   wait("document.querySelectorAll('#mediaPlayer video').length===1")
   page.send('Runtime.evaluate',{'expression':"document.querySelector('#mediaPlayer video').play().catch(()=>{})",'userGesture':True,'awaitPromise':True})
   end=time.monotonic()+4
   while not any(x['path']=='/v1/media/clip-one.mp4' for x in network)and time.monotonic()<end:time.sleep(.1)
   check(any(x['path']=='/v1/media/clip-one.mp4' for x in network),lang+': selected video was not requested')
   check(not any(x['path']=='/v1/media/clip-two.mp4' for x in network),lang+': unselected video was requested')
   page.js("closeMediaPreview()")
   check(page.js("document.querySelectorAll('#mediaPlayer video').length===0"),lang+': preview player not released')
   page.js("goTab('tasks')")
   wait("document.querySelector('#taskBody').textContent.includes('fixture-task')")
   text=page.js("document.querySelector('#tab-tasks tbody').innerText")
   check('Fixture-0' in text,lang+': task account is not visible')
   for word in (['Queue','Generate','Total']if lang=='en'else['排队','生成','总计']):check(word in text,lang+': task duration missing '+word)
   server_state['mode']='unknown'
   page.js("loadRepoStatus(true)",await_promise=True)
   badge=page.js("$('repoBadge').textContent")
   check('已是最新'not in badge and'Up to date'not in badge,lang+': unknown remote was reported current')
   if lang=='en':check('unconfirmed'in badge.lower(),'en: unknown state is not localized')
   server_state['mode']='update'
   page.js("loadRepoStatus(true);goTab('pool')")
   print('UI_FEATURE_PROBE',lang,json.dumps({'errors':feature_errors,'original_video_requests':[x['path']for x in network if x['path'].endswith('.mp4')]}),flush=True)
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
   print('FEATURE_ERRORS',json.dumps(feature_errors),flush=True)
   assert not feature_errors,feature_errors
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
