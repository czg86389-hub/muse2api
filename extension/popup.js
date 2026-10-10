/* 读取 muse.ai 的 httpOnly Cookie，展示必填项并复制成管理页能粘贴的字符串。 */

const $ = (id) => document.getElementById(id);
const ext = globalThis.chrome || globalThis.browser;

const REQUIRED = ['hatch_sess', 'hatch_vml', 'hatch_native_auth_device'];
const OPTIONAL = ['hatch_gw'];
const ORDER = [...REQUIRED, ...OPTIONAL];

function log(text, cls) {
  const el = $('log');
  el.className = cls ? `show ${cls}` : 'show';
  el.textContent = text;
}

function cookieHeader(cookies) {
  return ORDER.filter((name) => cookies[name]).map((name) => `${name}=${cookies[name]}`).join('; ');
}

function renderList(cookies) {
  const list = $('list');
  list.replaceChildren();
  for (const name of ORDER) {
    const row = document.createElement('div');
    const present = Object.prototype.hasOwnProperty.call(cookies, name);
    const optional = OPTIONAL.includes(name);
    row.className = 'item';
    const title = document.createElement('b');
    title.textContent = name;
    const state = document.createElement('span');
    if (present) {
      state.className = 'ok';
      state.textContent = '已读取';
    } else if (optional) {
      state.className = 'opt';
      state.textContent = '未下发，可忽略';
    } else {
      state.className = 'bad';
      state.textContent = '缺失';
    }
    row.append(title, state);
    list.append(row);
  }
}

function showMissing(names) {
  const box = $('missing');
  if (!names.length) {
    box.hidden = true;
    box.textContent = '';
    return;
  }
  box.hidden = false;
  box.textContent = `缺少必填 Cookie：${names.join('、')}\n`
    + '请在这个窗口打开 https://muse.ai/ ，登录到能看到聊天界面，再点「刷新会话」。';
}

function museHost(url) {
  try {
    const host = new URL(url).hostname;
    return host === 'muse.ai' || host.endsWith('.muse.ai');
  } catch (err) {
    return false;
  }
}

async function museTab() {
  const tabs = await ext.tabs.query({ currentWindow: true });
  return tabs.find((tab) => tab.active && museHost(tab.url))
    || tabs.find((tab) => museHost(tab.url));
}

function requestSession() {
  return fetch('https://muse.ai/api/session', {
    credentials: 'include',
    headers: { accept: 'application/json' },
  }).then(async (response) => {
    let status = '';
    try {
      const data = await response.json();
      status = data && typeof data.status === 'string' ? data.status : '';
    } catch (err) {
      status = '';
    }
    return { http: response.status, status };
  });
}

async function renewSession() {
  if (!ext.tabs || !ext.scripting || typeof ext.scripting.executeScript !== 'function') {
    throw new Error('这个浏览器没有开放页面脚本接口。请用 1.3.0 的安装包重新上传扩展，并关联到当前窗口。');
  }
  const tab = await museTab();
  if (!tab || tab.id == null) {
    throw new Error('这个窗口里没有打开 muse.ai。请先打开 https://muse.ai/ 并登录到聊天界面。');
  }
  const target = { tabId: tab.id };
  let injected;
  try {
    injected = await ext.scripting.executeScript({ target, world: 'MAIN', func: requestSession });
  } catch (err) {
    injected = await ext.scripting.executeScript({ target, func: requestSession });
  }
  const info = injected && injected[0] && injected[0].result;
  if (!info || typeof info.http !== 'number') {
    throw new Error('没有拿到会话接口的结果。请确认 muse.ai 页面已打开。');
  }
  if (info.http === 401) {
    throw new Error('会话已失效。请在这个窗口重新登录 muse.ai，直到能看到聊天界面。');
  }
  if (info.http !== 200 || info.status !== 'assigned') {
    const detail = info.status ? `，状态 ${info.status}` : '';
    throw new Error(`会话接口没有签发新 Cookie（HTTP ${info.http}${detail}）。`);
  }
}

async function grabCookies() {
  if (!ext || !ext.cookies || typeof ext.cookies.getAll !== 'function') {
    throw new Error('这个浏览器没有开放 cookies 接口。请从 Roxy「扩展中心 → 本地上传」重新安装本扩展，并关联到当前项目窗口。');
  }
  const all = await ext.cookies.getAll({ domain: 'muse.ai' });
  const out = {};
  for (const item of all) {
    const host = (item.domain || '').replace(/^\./, '').toLowerCase();
    if (host !== 'muse.ai' && !host.endsWith('.muse.ai')) continue;
    out[item.name] = item.value;
  }
  return out;
}

async function refresh(fromRenew) {
  $('refresh').disabled = true;
  $('copy').disabled = true;
  log('正在读取…');
  try {
    const cookies = await grabCookies();
    renderList(cookies);
    const missing = REQUIRED.filter((name) => !cookies[name]);
    const header = cookieHeader(cookies);
    $('cookieText').value = header;
    $('copy').disabled = !header;
    showMissing(missing);
    if (!Object.keys(cookies).length) {
      log('没读到 muse.ai 的 Cookie。请先在这个窗口登录 https://muse.ai/ 。', 'bad');
    } else if (missing.length) {
      log(fromRenew
        ? `会话接口已请求，但仍缺少：${missing.join('、')}。`
        : '必填 Cookie 不齐，先不要粘贴到网站。', 'bad');
    } else {
      log(fromRenew
        ? '会话已刷新，必填 Cookie 已重新读取，可以复制。'
        : `已读到 ${REQUIRED.length} 条必填 Cookie，可以复制。`, 'ok');
    }
  } catch (err) {
    renderList({});
    $('cookieText').value = '';
    showMissing(REQUIRED);
    log(err && err.message ? err.message : String(err), 'bad');
  } finally {
    $('refresh').disabled = false;
  }
}

async function copyText() {
  const text = $('cookieText').value.trim();
  if (!text) {
    log('没有可复制的 Cookie。', 'bad');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    $('cookieText').focus();
    $('cookieText').select();
    const ok = document.execCommand('copy');
    if (!ok) {
      log('复制失败，请在文本框里手动全选复制。', 'bad');
      return;
    }
  }
  const missing = REQUIRED.filter((name) => !text.includes(name + '='));
  if (missing.length) {
    log(`已复制，但仍缺少：${missing.join('、')}。补齐后再粘到网站。`, 'bad');
    return;
  }
  log('已复制。到管理页「添加账号」或「粘贴导入」，贴进 Cookie 字符串后点导入。', 'ok');
}

async function renewAndRead() {
  $('renew').disabled = true;
  log('正在这个窗口的 muse.ai 页面请求会话接口…');
  try {
    await renewSession();
    await refresh(true);
  } catch (err) {
    log(err && err.message ? err.message : String(err), 'bad');
  } finally {
    $('renew').disabled = false;
  }
}

$('renew').addEventListener('click', renewAndRead);
$('refresh').addEventListener('click', () => refresh(false));
$('copy').addEventListener('click', copyText);
refresh();
