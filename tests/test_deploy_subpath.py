"""Run: python tests/test_deploy_subpath.py (requires Node; no network/accounts)."""
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('admin.html', 'utf8');
const popup = fs.readFileSync('extension/popup.js', 'utf8');
let checks = 0;
const eq = (actual, expected) => { assert.equal(actual, expected); checks++; };
function section(source, from, to) {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start);
  assert(start >= 0 && end > start, `Missing source section: ${from}`);
  return source.slice(start, end);
}
// Parse every shipped inline script as well as the extension, not a rewritten copy.
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
  new vm.Script(match[1]);
}
new vm.Script(popup);
const basics = section(html, 'const $ = id =>', '/* 登录状态');
const api = section(html, 'async function api(path, opts){', '\nfunction esc(');
const loadKey = section(html, 'async function loadApikey(){', '\nfunction maskKey(');
const buildCmd = section(html, 'function buildCmd(baseUrl){', '\nasync function copyText(');
const fillExt = section(html, 'function fillExtFields(baseUrl){', '\nasync function loadExtFiles(');
const healthLine = html.match(/^\s*const hr = await fetch\([^\n]+/m)?.[0];
assert(healthLine, 'Missing restart health check');
const localLinks = [...html.matchAll(/<a\b[^>]*data-app-path="([^"]+)"[^>]*>/g)]
  .map(match => match[1]);
eq(JSON.stringify(localLinks), JSON.stringify([
  '/admin/extension', '/admin/cookie-helper?download=1', '/admin/cookie-helper'
]));
assert(!/<a\b[^>]*href="\/admin\//.test(html), 'Download link escapes deployment prefix');

(async () => {
  for (const pathname of ['/', '/admin', '/admin/', '/muse/', '/muse/admin', '/muse/admin/']) {
    const prefix = pathname.startsWith('/muse') ? '/muse' : '';
    const base = 'https://example.test' + prefix;
    const elements = {};
    const links = localLinks.map(path => ({ dataset: { appPath: path }, href: '' }));
    const calls = [];
    let reply = {};
    let status = 200;
    const ctx = vm.createContext({
      location: { pathname, origin: 'https://example.test', search: '?key=test-only' },
      URLSearchParams,
      localStorage: { getItem: () => '' },
      document: {
        getElementById: id => elements[id] ||= {},
        querySelectorAll: () => links,
      },
      setAuth: () => {}, renderKey: () => {}, realKey: '',
      fetch: async (url, opts) => {
        calls.push({ url, opts });
        return { status, ok: status === 200, text: async () => JSON.stringify(reply) };
      },
    });
    vm.runInContext([basics, api, loadKey, buildCmd, fillExt,
      `async function checkHealth(){ ${healthLine}\n return hr; }`].join('\n'), ctx);
    eq(elements.dBase.textContent, base + '/v1');
    links.forEach((link, i) => eq(link.href, prefix + localLinks[i]));
    await vm.runInContext("api('/admin/status?test=1', {method:'POST',body:'{}'})", ctx);
    eq(calls.at(-1).url, prefix + '/admin/status?test=1');
    eq(calls.at(-1).opts.method, 'POST');
    eq(calls.at(-1).opts.headers.Authorization, 'Bearer test-only');

    // No PUBLIC_BASE configured: address, extension fields and helper stay under app prefix.
    await vm.runInContext('loadApikey()', ctx);
    eq(elements.inBase.value, base + '/v1');
    eq(elements.exBase.value, base);
    assert(elements.cmdText.textContent.includes('--base ' + base + ' ')); checks++;
    // Explicit external address still takes precedence.
    reply = { base_url: 'https://external.test/prefix/v1', api_key: 'test-only' };
    await vm.runInContext('loadApikey()', ctx);
    eq(elements.inBase.value, reply.base_url);
    eq(elements.exBase.value, 'https://external.test/prefix');
    assert(elements.cmdText.textContent.includes('--base https://external.test/prefix ')); checks++;
    // Unauthenticated page must also show the correct local deployment address.
    status = 401;
    await vm.runInContext('loadApikey()', ctx);
    eq(elements.inBase.value, base + '/v1');
    eq(elements.exBase.value, base);
    await vm.runInContext('checkHealth()', ctx);
    assert(calls.at(-1).url.startsWith(prefix + '/healthz?t=')); checks++;
  }

  const norm = section(popup, 'function normBase(v)', '\nasync function loadCfg()');
  const loadCfg = section(popup, 'async function loadCfg()', '\nasync function saveCfg()');
  for (const [url, expected] of [
    ['https://example.test/admin?key=test-only', 'https://example.test'],
    ['https://example.test/admin/?key=test-only', 'https://example.test'],
    ['https://example.test/muse/admin?key=test-only', 'https://example.test/muse'],
    ['https://example.test/muse/admin/?key=test-only', 'https://example.test/muse'],
  ]) {
    const elements = {};
    const ctx = vm.createContext({
      URL, URLSearchParams, STORE: 'test',
      $: id => elements[id] ||= {},
      chrome: { storage: { local: { get: async () => ({}) } },
        tabs: { query: async () => [{ url }] } },
    });
    vm.runInContext(norm + '\n' + loadCfg, ctx);
    await vm.runInContext('loadCfg()', ctx);
    eq(elements.base.value, expected);
    eq(elements.key.value, 'test-only');
    ctx.input = expected + '/v1/';
    eq(vm.runInContext('normBase(input)', ctx), expected);
  }
  console.log(`PASS ${checks} subpath checks; inline/extension JS syntax valid; no network/accounts`);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


if __name__ == "__main__":
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node is required for the shipped JavaScript behavior checks")
    result = subprocess.run([node, "-"], input=SCRIPT, text=True, encoding="utf-8", cwd=ROOT)
    raise SystemExit(result.returncode)
