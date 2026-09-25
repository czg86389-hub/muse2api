/* Muse2API Cookie 导入 —— 读取 muse.ai 的 cookie 并 POST 到 muse2api 服务。
 *
 * 关键点：用 chrome.cookies 而不是 document.cookie。
 * muse.ai 的 4 条核心 cookie（hatch_sess / hatch_gw / hatch_vml /
 * hatch_native_auth_device）都带 httpOnly，网页 JS 读不到，
 * 只有浏览器扩展的 cookies 接口能拿到。
 */

const $ = (id) => document.getElementById(id);
const STORE = 'muse2api_ext_cfg';

const ESSENTIAL = ['hatch_sess', 'hatch_gw', 'hatch_vml', 'hatch_native_auth_device'];

function log(html, cls) {
  const el = $('log');
  el.className = 'show';
  el.innerHTML = cls ? `<span class="${cls}">${html}</span>` : html;
}

/* 规范化服务地址：去掉结尾斜杠和 /v1 后缀 */
function normBase(v) {
  let s = (v || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  s = s.replace(/\/+$/, '');
  s = s.replace(/\/v1$/i, '');
  return s;
}

async function loadCfg() {
  const o = await chrome.storage.local.get(STORE);
  const c = o[STORE] || {};
  if (c.base) $('base').value = c.base;
  if (c.key) $('key').value = c.key;
  if (c.label) $('label').value = c.label;
  // 没有配置过就尝试从当前标签页猜一个（用户在管理页上时）
  if (!c.base) {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const u = tab && tab.url ? new URL(tab.url) : null;
      if (u && /\/admin/.test(u.pathname)) {
        $('base').value = u.origin;
        const k = new URLSearchParams(u.search).get('key');
        if (k) $('key').value = k;
      }
    } catch (e) { /* 忽略 */ }
  }
}

async function saveCfg() {
  await chrome.storage.local.set({
    [STORE]: {
      base: normBase($('base').value),
      key: $('key').value.trim(),
      label: $('label').value.trim(),
    },
  });
}

async function grabCookies() {
  const all = await chrome.cookies.getAll({ domain: 'muse.ai' });
  const out = {}, exp = {};
  for (const c of all) {
    const dom = (c.domain || '').replace(/^\./, '');
    if (!dom.endsWith('muse.ai')) continue;
    out[c.name] = c.value;
    if (c.expirationDate) exp[c.name] = Math.floor(c.expirationDate);
  }
  return { cookies: out, expires: exp };
}

async function run() {
  const base = normBase($('base').value);
  const key = $('key').value.trim();
  const label = $('label').value.trim();

  if (!base) return log('请先填服务地址', 'bad');
  if (!key) return log('请先填 API Key', 'bad');

  $('go').disabled = true;
  log('正在读取 muse.ai 的 Cookie…');

  try {
    const { cookies, expires } = await grabCookies();
    const names = Object.keys(cookies);
    if (!names.length) {
      return log('没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 '
                 + 'https://muse.ai/ ，再回来点一次。', 'bad');
    }
    const missing = ESSENTIAL.filter((n) => !(n in cookies));
    if (missing.length) {
      log(`读到 ${names.length} 条 Cookie，但缺核心项：${missing.join('、')}\n`
          + '说明这个浏览器还没登录成功。请登录到能看到聊天界面再试。', 'warn');
      return;
    }

    log(`读到 ${names.length} 条 Cookie，正在上传到 ${base} …`);

    const r = await fetch(base + '/admin/accounts', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
      },
      body: JSON.stringify({ label, cookies, expires }),
    });

    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }

    if (r.status === 401) {
      return log('API Key 不对（服务返回 401）。\n'
                 + '请到管理页「账号池」页顶部复制正确的 API Key。', 'bad');
    }
    if (!r.ok) {
      return log(`导入失败：HTTP ${r.status}\n${text.slice(0, 300)}`, 'bad');
    }

    const a = (data.added && data.added[0]) || {};
    await saveCfg();
    log(`✓ 导入成功\n账号标签：${a.label || label || '(自动)'}\n`
        + `账号 ID：${a.id || '?'}\nCookie 条数：${a.cookie_count || names.length}\n`
        + `有效期到：${a.expires_at ? new Date(a.expires_at * 1000).toLocaleString() : '未知'}\n`
        + (data.warning ? `\n注意：${data.warning}` : ''), 'ok');
  } catch (e) {
    log('出错了：' + (e && e.message ? e.message : String(e))
        + '\n\n常见原因：\n'
        + '· 服务地址填错或服务没启动\n'
        + '· 这个地址不是 https（或证书不被信任）\n'
        + '· 浏览器拦截了跨域请求', 'bad');
  } finally {
    $('go').disabled = false;
  }
}

$('go').addEventListener('click', run);
loadCfg();
