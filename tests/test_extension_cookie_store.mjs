import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../extension/popup.js', import.meta.url), 'utf8');
const cookie = (value, extra = {}) => ({name: 'hatch_sess', value, domain: '.muse.ai', path: '/', httpOnly: true, ...extra});

function fixture({incognito = false, tab = true, stores, cookies, language = 'zh-CN'} = {}) {
  const active = tab ? {id: 27, incognito} : undefined;
  const storeId = incognito ? 'private-7' : 'regular-3';
  const calls = {reads: [], uploads: []};
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: '', innerHTML: '', addEventListener() {}});
    return elements.get(id);
  };
  element('base').value = 'https://test.invalid';
  element('key').value = 'fixture-key';
  const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => [], documentElement: {}, title: ''},
    navigator: {language}, localStorage: {getItem: () => null},
    chrome: {
      storage: {local: {get: async () => ({}), set: async () => {}}},
      tabs: {query: async () => active ? [active] : []},
      cookies: {
        getAllCookieStores: async () => stores ?? [
          {id: 'wrong-default', tabIds: [99]}, {id: storeId, tabIds: [27]},
        ],
        getAll: async query => {
          calls.reads.push(query);
          assert.equal(query.storeId, storeId, 'must use the active tab store, including regular mode');
          assert.equal(query.url, 'https://muse.ai/api/session');
          assert.equal(query.domain, undefined, 'must not collect every Muse subdomain');
          return cookies ?? [cookie(incognito ? 'private-valid' : 'regular-valid')];
        },
      },
    },
    fetch: async (url, request) => {
      calls.uploads.push({url, body: JSON.parse(request.body)});
      return {status: 200, ok: true, text: async () => JSON.stringify({added: [{id: 'fixture', label: 'fixture'}]})};
    },
  });
  vm.runInContext(source, context);
  return {calls, element, run: code => vm.runInContext(code, context)};
}

let checks = 0;
for (const incognito of [false, true]) {
  const f = fixture({incognito});
  const result = await f.run('grabCookies()');
  assert.equal(result.cookies.hatch_sess, incognito ? 'private-valid' : 'regular-valid');
  assert.equal(f.calls.reads.length, 1);
  checks++;
}
for (const options of [{tab: false}, {stores: [{id: 'wrong-default', tabIds: [99]}]}]) {
  const f = fixture(options);
  await assert.rejects(f.run('grabCookies()'), /Cookie 存储区/);
  assert.equal(f.calls.reads.length, 0, 'unknown store must fail closed');
  checks++;
}
{
  const f = fixture({cookies: [cookie('path-specific', {path:'/api', expirationDate:2000000000}), cookie('root-lower-priority', {expirationDate:2100000000})]});
  const result = await f.run('grabCookies()');
  assert.equal(result.cookies.hatch_sess, 'path-specific');
  assert.equal(result.expires.hatch_sess, 2000000000);
  checks++;
}
{
  const f = fixture({cookies: [cookie('earlier-session'), cookie('later-cookie', {expirationDate:2100000000})]});
  const result = await f.run('grabCookies()');
  assert.equal(result.cookies.hatch_sess, 'earlier-session');
  assert.equal(result.expires.hatch_sess, undefined, 'do not copy expiry from a discarded duplicate');
  checks++;
}
{
  const f = fixture({incognito: true, cookies: [cookie('httpOnly%3Afixture%2Bvalue', {expirationDate:2000000000})]});
  await f.run('run()');
  assert.equal(f.calls.uploads.length, 1);
  assert.equal(f.calls.uploads[0].body.cookies.hatch_sess, 'httpOnly%3Afixture%2Bvalue');
  assert.equal(f.calls.uploads[0].body.expires.hatch_sess, 2000000000);
  assert.match(f.element('log').innerHTML, /Cookie 来源：当前无痕\/InPrivate窗口/);
  assert.match(f.element('log').innerHTML, /请到管理页测试账号/);
  checks++;
}
for (const options of [{tab: false}, {cookies: [cookie('')]}]) {
  const f = fixture(options);
  await f.run('run()');
  assert.equal(f.calls.uploads.length, 0);
  assert.equal(f.element('go').disabled, false);
  checks++;
}
{
  const f = fixture({incognito: true, language: 'en-US'});
  await f.run('run()');
  assert.match(f.element('log').innerHTML, /current Incognito\/InPrivate window/);
  checks++;
}
console.log(`PASS: ${checks} extension cookie-store, URL scope, duplicate precedence and import checks; no live cookies/network`);
