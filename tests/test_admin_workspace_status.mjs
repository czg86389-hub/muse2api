import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import vm from 'node:vm';
const html = readFileSync(new URL('../admin.html', import.meta.url), 'utf8');
function extract(name, async = true) {
  const start = html.indexOf(`${async ? 'async ' : ''}function ${name}(`);
  assert.ok(start >= 0);
  return html.slice(start, html.indexOf('\n}', start) + 2);
}
for (const [result, available, failed, tone] of [
  [{refreshed_count: 4, succeeded_count: 0, failed_count: 4}, 0, 4, 'warn'],
  [{refreshed_count: 2, succeeded_count: 1, failed_count: 1}, 1, 1, 'warn'],
  [{refreshed_count: 2, refreshed: [{ok: true}, {ok: false}]}, 1, 1, 'warn'],
  [{refreshed_count: 1, succeeded_count: 1, failed_count: 0}, 1, 0, 'ok'],
]) {
  const notices = [], button = {};
  const ctx = vm.createContext({$:()=>button, api:async()=>result, toast:(...args)=>notices.push(args), loadPool:()=>{}});
  await vm.runInContext(extract('keepaliveAll')+';keepaliveAll()', ctx);
  assert.match(notices.at(-1)[0], new RegExp(`可用 ${available} 个，异常 ${failed} 个`));
  assert.equal(notices.at(-1)[1], tone);
  assert.equal(button.disabled, false);
}
let refreshed = false;
const ctx = vm.createContext({api:async()=>{throw Error('workspace unavailable')},toast:()=>{},loadPool:()=>{refreshed=true}});
await vm.runInContext(extract('queryQuota')+';queryQuota("fixture")', ctx);
assert.equal(refreshed, true, 'quota failure must refresh account availability');
const quota = vm.runInNewContext(extract('quotaBadge', false)+';quotaBadge({workspace_available:false,quota:{found:true,weekly_used_pct:0}})');
assert.match(quota, /暂不可查询/);
assert.ok(!quota.includes('已用 0%'), 'cached quota must not look currently usable');
console.log('PASS: 6 admin workspace/keepalive behavior cases; no network');
