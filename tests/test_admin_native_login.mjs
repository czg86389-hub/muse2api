/** Offline behavior checks for actual admin login code. No OTP/account/network. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const html = readFileSync(fileURLToPath(new URL('../admin.html', import.meta.url)), 'utf8');
const start = html.indexOf('/* ================= 验证码登录 ================= */');
const end = html.indexOf('/* ================= 验证码登录结束 ================= */', start);
assert.ok(start >= 0 && end > start, 'native login implementation must exist');
const source = html.slice(start, end);
const apiStart = html.indexOf('async function api(path, opts){');
const apiEnd = html.indexOf('\nfunction esc(', apiStart);
const apiSource = html.slice(apiStart, apiEnd);
const inline = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
new vm.Script(inline, {filename:'admin-inline.js'});
assert.ok(html.indexOf('id="nativeLoginCard"') < html.indexOf('方式一：浏览器扩展'));
assert.match(html, /data-t="cookie">导入账号/);
assert.match(html, /id="nativeLoginCode"[^>]*maxlength="6"/);
assert.match(html, /id="nativeLoginCode"[^>]*autocomplete="one-time-code"/);
assert.doesNotMatch(source, /localStorage|sessionStorage|console\.|location\./);

function harness(){
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value:'', hidden:false, disabled:false, textContent:'', style:{}});
    return elements.get(id);
  };
  let now = 1_800_000_000_000;
  let timerId = 0;
  const timers = new Map();
  const calls = [], responses = [];
  let poolLoads = 0;
  const context = {
    $, Date:class extends Date {static now(){return now;}},
    KEY:'fixture-admin-key', appUrl:path=>'/muse'+path, setAuth:()=>{},
    loadPool:()=>{poolLoads += 1;},
    setInterval:fn=>{const id=++timerId;timers.set(id,fn);return id;},
    clearInterval:id=>timers.delete(id),
    localStorage:new Proxy({}, {get(){throw Error('credential storage forbidden');}}),
    sessionStorage:new Proxy({}, {get(){throw Error('credential storage forbidden');}}),
    console:new Proxy({}, {get(){throw Error('credential logging forbidden');}}),
    fetch:async (url, opts)=>{
      calls.push({url, opts});
      assert.ok(responses.length, 'unexpected request '+url);
      let response = responses.shift();
      if (typeof response === 'function') response = await response();
      if (response instanceof Error) throw response;
      const status = response.httpStatus ?? 200;
      return {status,ok:status>=200&&status<300,text:async()=>JSON.stringify(response.body ?? response)};
    },
  };
  vm.createContext(context);
  vm.runInContext(apiSource+'\n'+source, context, {filename:'admin-native-login.js'});
  return { $, calls, responses, context, timers, poolLoads:()=>poolLoads,
    run:code=>vm.runInContext(code,context),
    advance:seconds=>{now+=seconds*1000;for(const fn of [...timers.values()]) fn();},
    awaiting:(overrides={})=>({login_id:'fixture-login',state:'awaiting_code',expires_at:now/1000+300,resend_after:60,...overrides}),
  };
}

let checks = 0;
async function check(name, test){await test();checks++;console.log('PASS',name);}
async function begin(h, overrides={}){
  h.$('nativeLoginIdentifier').value='fixture@example.test';
  h.$('nativeLoginLabel').value='fixture';
  h.responses.push(h.awaiting(overrides));
  await h.context.nativeLoginStart();
}

await check('empty identifier never requests OTP', async()=>{
  const h=harness();await h.context.nativeLoginStart();assert.equal(h.calls.length,0);
  assert.match(h.$('nativeLoginMessage').textContent,/手机号或邮箱/);
});
await check('start uses prefixed authenticated API and activates six-digit step', async()=>{
  const h=harness();await begin(h);
  assert.equal(h.calls[0].url,'/muse/admin/login/start');
  assert.equal(h.calls[0].opts.headers.Authorization,'Bearer fixture-admin-key');
  assert.deepEqual(JSON.parse(h.calls[0].opts.body),{identifier:'fixture@example.test',label:'fixture'});
  assert.equal(h.$('nativeLoginCodeRow').hidden,false);
  assert.equal(h.$('nativeLoginIdentifier').disabled,true);
  assert.equal(h.$('nativeLoginResendBtn').disabled,true);
  assert.equal(h.poolLoads(),0);
});
await check('duplicate start blocked while request is pending', async()=>{
  const h=harness();h.$('nativeLoginIdentifier').value='+15550000000';
  let release;h.responses.push(()=>new Promise(resolve=>{release=resolve;}));
  const pending=h.context.nativeLoginStart();
  assert.equal(h.$('nativeLoginStartBtn').disabled,true);
  await h.context.nativeLoginStart();assert.equal(h.calls.length,1);
  release(h.awaiting());await pending;
});
await check('resend countdown defaults to 60 seconds and blocks early requests', async()=>{
  const h=harness();const reply={...h.awaiting()};delete reply.resend_after;
  h.$('nativeLoginIdentifier').value='fixture@example.test';h.responses.push(reply);
  await h.context.nativeLoginStart();await h.context.nativeLoginResend();
  assert.equal(h.calls.length,1);assert.match(h.$('nativeLoginResendBtn').textContent,/60/);
  h.advance(59);assert.equal(h.$('nativeLoginResendBtn').disabled,true);
  h.advance(1);assert.equal(h.$('nativeLoginResendBtn').disabled,false);
});
await check('server resend delay is respected; resend clears old code', async()=>{
  const h=harness();await begin(h,{resend_after:90});h.advance(60);
  assert.equal(h.$('nativeLoginResendBtn').disabled,true);h.advance(30);
  h.$('nativeLoginCode').value='123456';h.responses.push(h.awaiting({resend_after:75}));
  await h.context.nativeLoginResend();assert.equal(h.$('nativeLoginCode').value,'');
  assert.equal(h.calls[1].url,'/muse/admin/login/fixture-login/resend');
  assert.equal(h.calls[1].opts.body,'{}');assert.match(h.$('nativeLoginResendBtn').textContent,/75/);
});
await check('invalid code does not call backend', async()=>{
  const h=harness();await begin(h);
  for(const code of ['12345','1234567','abcdef','']){h.$('nativeLoginCode').value=code;await h.context.nativeLoginVerify();}
  assert.equal(h.calls.length,1);assert.match(h.$('nativeLoginMessage').textContent,/6 位数字/);
});
await check('code cleared before verify network completes; duplicate verify blocked', async()=>{
  const h=harness();await begin(h);h.$('nativeLoginCode').value='123456';
  let release;h.responses.push(()=>new Promise(resolve=>{release=resolve;}));
  const pending=h.context.nativeLoginVerify();
  assert.equal(h.$('nativeLoginCode').value,'');assert.equal(h.$('nativeLoginCancelBtn').disabled,true);
  assert.deepEqual(JSON.parse(h.calls[1].opts.body),{code:'123456'});
  assert.ok(!h.calls[1].url.includes('123456'));
  await h.context.nativeLoginVerify();assert.equal(h.calls.length,2);
  release({state:'authenticated',account:{workspace_available:true}});await pending;
  assert.equal(h.poolLoads(),1);assert.equal(h.run('nativeLogin.id'),null);
  assert.equal(h.$('nativeLoginCodeRow').hidden,true);assert.equal(h.$('nativeLoginIdentifier').value,'');
  assert.match(h.$('nativeLoginMessage').textContent,/登录并保存账号成功/);
});
await check('wrong code remains awaiting without claiming success', async()=>{
  const h=harness();await begin(h);h.$('nativeLoginCode').value='123456';
  h.responses.push(h.awaiting({message:'验证码不正确，请重试',resend_after:30}));
  await h.context.nativeLoginVerify();assert.equal(h.poolLoads(),0);
  assert.match(h.$('nativeLoginMessage').textContent,/不正确/);
  assert.equal(h.$('nativeLoginCode').value,'');assert.equal(h.$('nativeLoginCodeRow').hidden,false);
});
await check('unsupported challenge displays backend message and website help', async()=>{
  const h=harness();h.$('nativeLoginIdentifier').value='fixture@example.test';
  h.responses.push({login_id:'fixture-login',state:'unsupported',message:'请在官网完成人机验证'});
  await h.context.nativeLoginStart();assert.equal(h.poolLoads(),0);
  assert.equal(h.$('nativeLoginWebsiteHelp').hidden,false);
  assert.equal(h.$('nativeLoginCodeRow').hidden,true);
  assert.match(h.$('nativeLoginMessage').textContent,/人机验证/);
  assert.doesNotMatch(h.$('nativeLoginMessage').textContent,/登录并保存账号成功/);
});
await check('authenticated but unavailable workspace is not presented as usable', async()=>{
  const h=harness();await begin(h);h.$('nativeLoginCode').value='123456';
  h.responses.push({state:'authenticated',account:{workspace_available:false}});
  await h.context.nativeLoginVerify();assert.equal(h.poolLoads(),1);
  assert.match(h.$('nativeLoginMessage').textContent,/工作区当前不可用/);
  assert.equal(h.$('nativeLoginMessage').style.color,'var(--warn)');
});
await check('expired login clears code, stops timer, and blocks verify', async()=>{
  const h=harness();await begin(h,{expires_at:1_800_000_002});h.$('nativeLoginCode').value='123456';h.advance(3);
  await h.context.nativeLoginVerify();assert.equal(h.calls.length,1);
  assert.equal(h.$('nativeLoginCode').value,'');assert.equal(h.timers.size,0);
  assert.equal(h.run('nativeLogin.state'),'expired');
});
await check('network failure does not auto-repeat verification; status can confirm success', async()=>{
  const h=harness();await begin(h);h.$('nativeLoginCode').value='123456';h.responses.push(new Error('network unavailable'));
  await h.context.nativeLoginVerify();assert.equal(h.calls.length,2);assert.equal(h.poolLoads(),0);
  assert.equal(h.$('nativeLoginCode').value,'');assert.match(h.$('nativeLoginMessage').textContent,/未确认/);
  h.responses.push({state:'authenticated',account:{workspace_available:true}});await h.context.nativeLoginRefresh();
  assert.equal(h.calls[2].url,'/muse/admin/login/fixture-login');
  assert.equal(h.calls[2].opts.method,undefined);assert.equal(h.poolLoads(),1);
});
await check('cancel deletes only the in-memory login and clears sensitive inputs', async()=>{
  const h=harness();await begin(h);h.$('nativeLoginCode').value='123456';h.responses.push({ok:true});
  await h.context.nativeLoginCancel();assert.equal(h.calls[1].opts.method,'DELETE');
  assert.equal(h.$('nativeLoginCode').value,'');assert.equal(h.$('nativeLoginIdentifier').value,'');
  assert.equal(h.run('nativeLogin.id'),null);assert.equal(h.run('nativeLogin.state'),'idle');
  assert.equal(h.timers.size,0);
});
await check('cancel failure retains login ID for retry, never claims cancellation', async()=>{
  const h=harness();await begin(h);h.$('nativeLoginCode').value='123456';h.responses.push(new Error('offline'));
  await h.context.nativeLoginCancel();assert.equal(h.run('nativeLogin.id'),'fixture-login');
  assert.equal(h.$('nativeLoginCode').value,'');assert.match(h.$('nativeLoginMessage').textContent,/取消未完成/);
  assert.equal(h.$('nativeLoginCancelBtn').disabled,false);
});
await check('session identifiers are encoded in routes', async()=>{
  const h=harness();await begin(h,{login_id:'fixture/id?part'});h.responses.push({ok:true});
  await h.context.nativeLoginCancel();assert.equal(h.calls[1].url,'/muse/admin/login/fixture%2Fid%3Fpart');
});
await check('unknown/missing server state never claims authenticated', async()=>{
  const h=harness();h.$('nativeLoginIdentifier').value='fixture@example.test';h.responses.push({ok:true});
  await h.context.nativeLoginStart();assert.equal(h.poolLoads(),0);
  assert.match(h.$('nativeLoginMessage').textContent,/未返回可确认/);
});
await check('server text is rendered as text, not HTML', async()=>{
  const h=harness();h.$('nativeLoginIdentifier').value='fixture@example.test';
  h.responses.push({state:'failed',message:'<img src=x onerror=alert(1)>'});await h.context.nativeLoginStart();
  assert.equal(h.$('nativeLoginMessage').textContent,'<img src=x onerror=alert(1)>');
  assert.equal(h.$('nativeLoginMessage').innerHTML,undefined);
});
await check('restarting an expired attempt cancels the old ID before requesting a new code', async()=>{
  const h=harness();await begin(h,{expires_at:1_800_000_001});h.advance(2);
  h.responses.push({ok:true},h.awaiting({login_id:'replacement-login'}));
  await h.context.nativeLoginStart();
  assert.equal(h.calls[1].url,'/muse/admin/login/fixture-login');
  assert.equal(h.calls[1].opts.method,'DELETE');
  assert.equal(h.calls[2].url,'/muse/admin/login/start');
  assert.equal(h.run('nativeLogin.id'),'replacement-login');
  assert.equal(h.$('nativeLoginCodeRow').hidden,false);
});
await check('fresh page recovers an awaiting session without resending OTP or persisting credentials', async()=>{
  const h=harness();h.responses.push(h.awaiting({login_id:'recovered-login',resend_after:42,contact_hint:'f***@example.test',message:'继续输入收到的验证码'}));
  await h.context.nativeLoginRecover();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/muse/admin/login/current');
  assert.equal(h.calls[0].opts.method,undefined);
  assert.equal(h.calls[0].opts.headers.Authorization,'Bearer fixture-admin-key');
  assert.equal(h.run('nativeLogin.id'),'recovered-login');
  assert.equal(h.$('nativeLoginCodeRow').hidden,false);
  assert.equal(h.$('nativeLoginIdentifier').value,'');
  assert.equal(h.$('nativeLoginContact').textContent,'接收账号：f***@example.test');
  assert.match(h.$('nativeLoginResendBtn').textContent,/42/);
  assert.match(h.$('nativeLoginMessage').textContent,/继续输入/);
  assert.equal(h.$('nativeLoginCancelBtn').hidden,false);
  assert.equal(h.poolLoads(),0);
  h.responses.push({ok:true});await h.context.nativeLoginCancel();
  assert.equal(h.calls[1].url,'/muse/admin/login/recovered-login');
  assert.equal(h.calls[1].opts.method,'DELETE');
});
await check('recovery with no active login allows a new start without side effects', async()=>{
  const h=harness();h.responses.push({state:'idle'});await h.context.nativeLoginRecover();
  assert.equal(h.calls.length,1);assert.equal(h.run('nativeLogin.id'),null);
  assert.equal(h.$('nativeLoginStartBtn').disabled,false);
  assert.equal(h.$('nativeLoginCodeRow').hidden,true);
  assert.match(h.$('nativeLoginMessage').textContent,/没有未完成/);
  assert.equal(h.timers.size,0);assert.equal(h.poolLoads(),0);
});
await check('duplicate recovery is blocked while lookup is in flight', async()=>{
  const h=harness();let release;h.responses.push(()=>new Promise(resolve=>{release=resolve;}));
  const pending=h.context.nativeLoginRecover();await h.context.nativeLoginRecover();
  assert.equal(h.calls.length,1);assert.equal(h.$('nativeLoginRecoverBtn').disabled,true);
  release({state:'idle'});await pending;
  assert.equal(h.$('nativeLoginRecoverBtn').disabled,false);
});
console.log(`PASS ${checks} native login behavior cases; inline JS syntax valid; no network/accounts/OTP`);
