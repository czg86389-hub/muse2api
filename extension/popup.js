/* Muse2API Cookie 导入 —— 读取 muse.ai 的 cookie 并 POST 到 muse2api 服务。
 *
 * 关键点：用 chrome.cookies 而不是 document.cookie。
 * muse.ai 的关键会话 cookie（hatch_sess / hatch_vml）带 httpOnly，
 * 网页 JS 读不到，只有浏览器扩展的 cookies 接口能拿到。
 * hatch_gw 是可重新签发的路由缓存，不应阻止账号导入。
 */

const $ = (id) => document.getElementById(id);
const STORE = 'muse2api_ext_cfg';

const ESSENTIAL = ['hatch_sess'];

/* ================= 界面语言 / UI language =================
 * 中文是源语言；英文通过下面的词典替换。
 * 新增中文文案时，请在 I18N_EN（整句，可含 {name} 占位符）或 I18N_EN_PATTERNS（服务端返回的含变量文案）里补上英文。 */
const LANG_STORE = 'muse2api_lang';
const LANG = (() => {
  try { const v = localStorage.getItem(LANG_STORE); if (v === 'zh' || v === 'en') return v; } catch (e) {}
  return /^zh\b/i.test(navigator.language || '') ? 'zh' : 'en';
})();
function setLang(v) {
  try { localStorage.setItem(LANG_STORE, v); } catch (e) {}
  location.reload();
}

/* 整句：中文 → 英文 */
const I18N_EN = {
  // popup.html 静态文案
  'Muse2API Cookie 导入': 'Muse2API Cookie Import',
  '把当前浏览器里 muse.ai 的登录态，一键同步到你的 muse2api 服务。':
    'Sync the muse.ai login session from this browser to your muse2api service in one click.',
  '服务地址（BASE URL）': 'Service URL (BASE URL)',
  '账号标签（可留空）': 'Account label (optional)',
  '例如 muse-main': 'e.g. muse-main',
  '读取并导入': 'Read and import',
  // popup.js 日志 / 状态
  '请先填服务地址': 'Please enter the service URL first',
  '请先填 API Key': 'Please enter the API Key first',
  '正在读取 muse.ai 的 Cookie…': 'Reading muse.ai cookies…',
  '没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 https://muse.ai/ ，再回来点一次。':
    'No muse.ai cookies found.\nOpen and log in to https://muse.ai/ in this browser first, then click again.',
  '读到 {n} 条 Cookie，但缺核心项：{missing}\n未读到登录会话，请检查当前浏览器的登录状态和扩展权限。':
    'Read {n} cookies, but missing core items such as: {missing}\nThe login session could not be read. Check this browser\'s login and extension permissions.',
  '读到 {n} 条 Cookie，正在上传到 {base} …': 'Read {n} cookies, uploading to {base} …',
  'API Key 不对（服务返回 401）。\n请到管理页「账号池」页顶部复制正确的 API Key。':
    'Invalid API Key (the service returned 401).\nCopy the correct API Key from the top of the "Account Pool" tab on the admin page.',
  '导入失败：HTTP {status}\n{body}': 'Import failed: HTTP {status}\n{body}',
  '(自动)': '(auto)',
  '未知': 'unknown',
  '✓ 导入成功\n账号标签：{label}\n账号 ID：{id}\nCookie 条数：{count}\n有效期到：{expires}\n':
    '✓ Imported successfully\nAccount label: {label}\nAccount ID: {id}\nCookie count: {count}\nValid until: {expires}\n',
  '\n注意：{warning}': '\nNote: {warning}',
  '出错了：{msg}\n\n常见原因：\n· 服务地址填错或服务没启动\n· 这个地址不是 https（或证书不被信任）\n· 浏览器拦截了跨域请求':
    'Something went wrong: {msg}\n\nCommon causes:\n· The service URL is wrong or the service is not running\n· The URL is not https (or the certificate is not trusted)\n· The browser blocked the cross-origin request',
  // 服务端（/admin/accounts）可能返回的整句
  '未解析到任何 cookie，请检查格式': 'No cookies could be parsed; please check the format',
  '缺少 Authorization: Bearer <key>': 'Missing Authorization: Bearer <key>',
  'API key 无效': 'Invalid API key',
};

/* 含富文本的块：data-i18n="key" → 英文 innerHTML */
const I18N_EN_HTML = {
  steps: '<b>Before you start, check two things:</b><br>\n'
    + '    1. You are logged in to <a href="https://muse.ai/" target="_blank"\n'
    + '       style="color:#4f8cff">muse.ai</a> in <b>this browser</b> (you can see the chat interface).<br>\n'
    + '    2. The service URL and API Key above are filled in (you can copy them from the top of the "Account Pool" tab on the muse2api admin page).',
  foot: 'This extension does only one thing: it uses the browser\'s <code>chrome.cookies</code> API to read muse.ai\n'
    + '    cookies (including the httpOnly ones) and POSTs them to the service URL you entered.\n'
    + '    Nothing is collected or uploaded to any third-party server.',
};

/* 服务端返回的含变量文案：[正则, 替换串] */
const I18N_EN_PATTERNS = [
  [/^缺少核心 cookie：(.*)，该账号可能无法生成$/s, 'Missing core cookies: $1; this account may not be able to generate'],
];

function t(s, vars) {
  let out = s;
  if (LANG === 'en' && typeof s === 'string') {
    if (Object.prototype.hasOwnProperty.call(I18N_EN, s)) out = I18N_EN[s];
    else for (const [re, rep] of I18N_EN_PATTERNS) if (re.test(s)) { out = s.replace(re, rep); break; }
  }
  if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  return out;
}

function applyLang() {
  $('langSel').value = LANG;
  if (LANG !== 'en') return;
  document.documentElement.lang = 'en';
  document.title = t(document.title);
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const html = I18N_EN_HTML[el.dataset.i18n];
    if (html) el.innerHTML = html;
  });
  document.querySelectorAll('[data-i18n-text]').forEach((el) => {
    el.textContent = t(el.textContent.trim());
  });
  document.querySelectorAll('[placeholder]').forEach((el) => {
    el.placeholder = t(el.placeholder);
  });
}

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

  if (!base) return log(t('请先填服务地址'), 'bad');
  if (!key) return log(t('请先填 API Key'), 'bad');

  $('go').disabled = true;
  log(t('正在读取 muse.ai 的 Cookie…'));

  try {
    const { cookies, expires } = await grabCookies();
    const names = Object.keys(cookies);
    if (!names.length) {
      return log(t('没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 '
                 + 'https://muse.ai/ ，再回来点一次。'), 'bad');
    }
    const missing = ESSENTIAL.filter((n) => !(n in cookies));
    if (missing.length) {
      log(t('读到 {n} 条 Cookie，但缺核心项：{missing}\n'
          + '未读到登录会话，请检查当前浏览器的登录状态和扩展权限。',
          { n: names.length, missing: missing.join(LANG === 'en' ? ', ' : '、') }), 'warn');
      return;
    }

    log(t('读到 {n} 条 Cookie，正在上传到 {base} …', { n: names.length, base }));

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
      return log(t('API Key 不对（服务返回 401）。\n'
                 + '请到管理页「账号池」页顶部复制正确的 API Key。'), 'bad');
    }
    if (!r.ok) {
      const body = typeof data.detail === 'string' ? t(data.detail) : text.slice(0, 300);
      return log(t('导入失败：HTTP {status}\n{body}', { status: r.status, body }), 'bad');
    }

    const a = (data.added && data.added[0]) || {};
    await saveCfg();
    log(t('✓ 导入成功\n账号标签：{label}\n账号 ID：{id}\nCookie 条数：{count}\n有效期到：{expires}\n', {
          label: a.label || label || t('(自动)'),
          id: a.id || '?',
          count: a.cookie_count || names.length,
          expires: a.expires_at ? new Date(a.expires_at * 1000).toLocaleString() : t('未知'),
        })
        + (data.warning ? t('\n注意：{warning}', { warning: t(data.warning) }) : ''), 'ok');
  } catch (e) {
    log(t('出错了：{msg}\n\n常见原因：\n'
        + '· 服务地址填错或服务没启动\n'
        + '· 这个地址不是 https（或证书不被信任）\n'
        + '· 浏览器拦截了跨域请求', { msg: e && e.message ? e.message : String(e) }), 'bad');
  } finally {
    $('go').disabled = false;
  }
}

applyLang();
$('langSel').addEventListener('change', (e) => setLang(e.target.value));
$('go').addEventListener('click', run);
loadCfg();
