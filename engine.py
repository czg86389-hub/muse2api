"""Muse 生成引擎：用真实网页会话驱动 muse.ai 完成生图/生视频。

已验证流程：
  注入 cookie -> 打开 https://muse.ai/ -> 定位 textarea(placeholder=消息)
  -> Input.insertText 填入 -> 点「发送」
  -> 等待新的附件容器 [data-testid^=hatch-chat-attachment-presentation-]
  -> 从该容器内的 img/video 取 blob 字节 -> 落盘
"""
from __future__ import annotations

import base64
import mimetypes
import urllib.request
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import unicodedata

from cdp import CDP, http_json

log = logging.getLogger("muse2api")

ATT_SEL = '[data-testid^="hatch-chat-attachment-presentation-"]'

# 决定账号生死的核心 cookie（缺失或过期 = 会话失效）
# 注：hatch_gw 为网关路由缓存，会话生死主要依赖 hatch_sess 与 hatch_vml，缺 gw 亦可正常续签与生成
ESSENTIAL_COOKIES = ("hatch_sess", "hatch_vml",
                     "hatch_native_auth_device")


class MuseAuthError(RuntimeError):
    pass


class MuseGenerationError(RuntimeError):
    pass


class MuseEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.browser: CDP | None = None
        self._local = threading.local()
        self._start_lock = threading.Lock()
        self._active_contexts: set[str] = set()
        self._contexts_lock = threading.Lock()
        self._last_http_renew: dict[str, float] = {}
        self._log = None
        os.makedirs(cfg.profile_dir, exist_ok=True)

    def _ensure_local(self):
        local = getattr(self, "_local", None)
        if local is None:
            local = threading.local()
            self._local = local
        return local

    @property
    def page(self) -> CDP | None:
        return getattr(self._ensure_local(), "page", None)

    @page.setter
    def page(self, val: CDP | None):
        self._ensure_local().page = val

    @property
    def current_acc_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_acc_id", None)

    @current_acc_id.setter
    def current_acc_id(self, val: str | None):
        self._ensure_local().current_acc_id = val

    @property
    def current_context_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_context_id", None)

    @current_context_id.setter
    def current_context_id(self, val: str | None):
        self._ensure_local().current_context_id = val

    # ---------------- 浏览器生命周期 ----------------
    def _debug_url(self):
        return f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"

    def start(self):
        with self._start_lock:
            if self.proc and self.proc.poll() is None and self.browser:
                return
        env = dict(os.environ)
        env.setdefault("HOME", self.cfg.home_dir)
        env["PATH"] = (self.cfg.extra_path + os.pathsep + env.get("PATH", "")) if self.cfg.extra_path else env.get("PATH", "")
        args = [
            self.cfg.chromium,
            "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1440,2400",
            f"--remote-debugging-port={self.cfg.cdp_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.cfg.profile_dir}",
            "about:blank",
        ]
        # 尝试复用已有健康 CDP
        if not self.proc:
            try:
                v = http_json(self._debug_url(), timeout=1)
                if v and "webSocketDebuggerUrl" in v:
                    self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                    return
            except Exception:
                pass

        # 清理残留锁
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lp = os.path.join(self.cfg.profile_dir, lock_name)
            if os.path.exists(lp) or os.path.islink(lp):
                try:
                    os.unlink(lp)
                except Exception:
                    pass

        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self._log = open(os.path.join(self.cfg.data_dir, "chromium.log"), "ab", buffering=0)
        cwd_dir = self.cfg.home_dir if (self.cfg.home_dir and os.path.isdir(self.cfg.home_dir)) else None
        self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT,
                                     env=env, cwd=cwd_dir)
        last = None
        for _ in range(90):
            try:
                v = http_json(self._debug_url(), timeout=2)
                self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1)
        raise MuseGenerationError(f"Chromium 启动失败: {last}")

    def stop(self):
        with self._start_lock:
            self._close_page()
            if self.browser:
                with self._contexts_lock:
                    for ctx_id in list(self._active_contexts):
                        try:
                            self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
                        except Exception:
                            pass
                    self._active_contexts.clear()
                try:
                    self.browser.close()
                except Exception:
                    pass
                self.browser = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
            self.proc = None

    # ---------------- 页面 ----------------
    def _close_page(self):
        """关闭当前线程专属的页面并销毁隔离的 BrowserContext，彻底释放内存与 Session。"""
        p = self.page
        ctx_id = self.current_context_id
        if p:
            try:
                p.close()
            except Exception:
                pass
            self.page = None
        if ctx_id and self.browser:
            try:
                self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
            except Exception:
                pass
            with self._contexts_lock:
                self._active_contexts.discard(ctx_id)
            self.current_context_id = None
        self.current_acc_id = None

    def _open_page(self):
        """为当前工作线程创建独立的页面。
        若浏览器主连接存在，则创建独立的 Incognito BrowserContext，实现多账号 Cookies 与会话 100% 隔离。"""
        if self.browser:
            try:
                res = self.browser.send("Target.createBrowserContext")
                ctx_id = res.get("result", {}).get("browserContextId")
                if ctx_id:
                    self.current_context_id = ctx_id
                    with self._contexts_lock:
                        self._active_contexts.add(ctx_id)
                    t_res = self.browser.send("Target.createTarget", {
                        "url": "about:blank",
                        "browserContextId": ctx_id
                    })
                    target_id = t_res.get("result", {}).get("targetId")
                    if target_id:
                        ws_url = f"ws://127.0.0.1:{self.cfg.cdp_port}/devtools/page/{target_id}"
                        page = CDP(ws_url, timeout=180)
                        page.send("Network.enable")
                        page.send("Page.enable")
                        page.send("Runtime.enable")
                        page.send("Browser.setDownloadBehavior",
                                  {"behavior": "allow", "downloadPath": self.cfg.download_dir})
                        return page
            except Exception as exc:
                log.warning("Target.createBrowserContext 创建隔离上下文失败，回退到普通标签页: %s", exc)

        import requests
        tgt = requests.put(
            f"http://127.0.0.1:{self.cfg.cdp_port}/json/new?about:blank",
            timeout=10).json()
        page = CDP(tgt["webSocketDebuggerUrl"], timeout=180)
        page.send("Network.enable")
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Browser.setDownloadBehavior",
                  {"behavior": "allow", "downloadPath": self.cfg.download_dir})
        return page

    @staticmethod
    def renew_session_http(cookies: dict, expires: dict | None = None,
                           wake_vm: bool = True) -> dict:
        """直接调用 muse.ai/api/session 续签 hatch_vml (+48h) / hatch_sess (+30d) / hatch_gw (+1y)，
        并按需调用 /api/hatch/vm/wake 唤醒云端工作区 VM。"""
        import requests
        cur_cookies = dict(cookies or {})
        cur_exp = dict(expires or {})
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Origin": "https://muse.ai",
            "Referer": "https://muse.ai/thread/new",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v),
        }
        try:
            r = requests.get("https://muse.ai/api/session", headers=headers,
                             timeout=12, allow_redirects=False)
        except requests.RequestException as exc:
            # 不回显请求内容：异常可能包含带凭据的代理 URL。
            raise MuseGenerationError(
                f"/api/session 网络请求失败 ({type(exc).__name__})；请检查服务器网络/代理后重试") from None
        if r.status_code == 401:
            raise MuseAuthError("会话认证失败 (/api/session HTTP 401)，请在官网确认登录后重新导入 cookie")
        if r.status_code != 200:
            hint = ("访问被拒绝，请检查服务器出口/地区/访问限制；不能据此判定 Cookie 失效"
                    if r.status_code == 403 else "上游请求未成功，请稍后重试并检查服务器网络")
            raise MuseGenerationError(f"/api/session HTTP {r.status_code}：{hint}")
        try:
            sj = r.json()
        except ValueError:
            raise MuseGenerationError("/api/session HTTP 200 返回非 JSON；会话状态未确认") from None
        if not isinstance(sj, dict) or sj.get("status") != "assigned":
            raise MuseGenerationError("/api/session HTTP 200 未返回 assigned 会话；请在官网检查账号/工作区状态")
        for c in r.cookies:
            if c.value:
                cur_cookies[c.name] = c.value
            if c.expires:
                cur_exp[c.name] = int(float(c.expires))
        vm_id = sj.get("vm_id")
        vm_state = sj.get("vm_state")
        wake_ok = False
        wake_requested = False
        if wake_vm and vm_id and vm_state != "DISABLED":
            try:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v)
                rw = requests.post("https://muse.ai/api/hatch/vm/wake", headers=headers, json={
                    "vm_id": vm_id,
                    "retry_count": 0,
                    "connect_attempt_id": str(uuid.uuid4()),
                }, timeout=8)
                wake_requested = rw.status_code == 200
                if wake_requested:
                    # HTTP 200 acknowledges the request; only session state confirms the VM woke.
                    verify = requests.get("https://muse.ai/api/session", headers=headers,
                                          timeout=12, allow_redirects=False)
                    if verify.status_code == 200:
                        verified_session = verify.json()
                        if isinstance(verified_session, dict) and verified_session.get("status") == "assigned":
                            vm_state = verified_session.get("vm_state") or vm_state
                            wake_ok = vm_state == "RUNNING"
            except Exception:
                pass
        return {
            "ok": sj.get("status") == "assigned",
            "status": sj.get("status"),
            "vm_id": vm_id,
            "vm_state": vm_state,
            "wake_ok": wake_ok,
            "wake_requested": wake_requested,
            "cookies": cur_cookies,
            "cookies_exp": cur_exp,
        }

    def _apply_cookies(self, page: CDP, cookies: dict, expires: dict | None = None):
        """注入 cookie。彻底清空旧账号 cookie 保证隔离，且绝不传入过去时间的 expires 防止 Chromium 丢弃 hatch_vml。"""
        try:
            page.send("Network.clearBrowserCookies")
        except Exception:
            pass
        now = time.time()
        for name, value in cookies.items():
            if not value:
                continue
            exp = (expires or {}).get(name)
            try:
                exp_val = float(exp) if exp and float(exp) > now + 3600 else (now + 7 * 86400)
            except (TypeError, ValueError):
                exp_val = now + 7 * 86400
            for dom in (".muse.ai", "muse.ai"):
                params = {
                    "name": name,
                    "value": value,
                    "domain": dom,
                    "path": "/",
                    "secure": True,
                    "expires": exp_val,
                }
                try:
                    page.send("Network.setCookie", params)
                except Exception:  # noqa: BLE001
                    pass

    def read_cookies(self) -> dict[str, dict]:
        """从当前页面读回 cookie（**包含 httpOnly**，这是网页 JS 做不到的）。

        返回 {name: {"value":..., "expires": unix秒 或 -1}}。
        用途：muse.ai 在访问时会续期部分 cookie，生成完读回来写进账号池，
        账号就不容易过期。
        """
        if not self.page:
            return {}
        try:
            msg = self.page.send("Network.getCookies",
                                 {"urls": [self.cfg.site_url]}, timeout=20)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict] = {}
        for c in (msg.get("result", {}).get("cookies") or []):
            name = c.get("name")
            if not name:
                continue
            try:
                exp = int(float(c.get("expires", -1)))
            except (TypeError, ValueError):
                exp = -1
            out[name] = {"value": c.get("value", ""), "expires": exp}
        return out

    def _wait_ws_ready(self, page: CDP, timeout: float = 15.0) -> bool:
        """等待 muse.ai 页面完成 React hydration 且不再处于 Connecting... 状态。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = page.js("""(function(){
                    if (document.readyState !== 'complete') return 'loading';
                    if (!document.querySelector('textarea')) return 'no-ta';
                    var h = document.querySelector('[data-hatch-shell-hydration-state]');
                    if (h && h.getAttribute('data-hatch-shell-hydration-state') !== 'hydrated') return 'hydrating';
                    var b = document.body ? (document.body.innerText || '') : '';
                    if (b.indexOf('Connecting...') !== -1) return 'connecting';
                    // Empty composers may not render Send until text has been entered.
                    if (document.querySelector('textarea').disabled) return 'input-disabled';
                    return 'ready';
                })()""")
                if st == "ready":
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def reset_thread(self, for_chat: bool = False):
        """关闭残留弹窗并确保处于干净会话且 WebSocket 已就绪。
        对于纯文本对话（for_chat=True），若当前热页面无附件、无卡死且气泡数较少，直接复用现有热连接以实现 2s 级秒回。"""
        if not self.page:
            return
        try:
            needs_nav = self.page.js("""(function(forChat){
                var d = document.querySelector('[role="dialog"]');
                if (d) {
                    var b = d.querySelector('button[aria-label*="close" i], button');
                    if (b) b.click();
                }
                var scope = document.querySelector('main,[class*="chat-scroll"],[class*="hatch-chat-scroll"]') || document.body;
                var bubbleCount = scope ? scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length : 0;
                var hasAtts = document.querySelectorAll('[data-testid^="hatch-chat-attachment-presentation-"]').length > 0;
                var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                    || document.querySelector('button[aria-label*="Stop" i]')
                    || document.querySelector('button[aria-label*="停止"]'));
                var bodyTxt = document.body ? (document.body.innerText || '') : '';
                var hasStuck = bodyTxt.indexOf('Still sending') !== -1 || bodyTxt.indexOf('Connecting...') !== -1;
                if (hasStop || hasStuck || hasAtts) return true;
                if (forChat) {
                    return bubbleCount >= 24;
                }
                return (window.location.pathname !== '/thread/new') || bubbleCount > 0;
            })(%s)""" % ("true" if for_chat else "false"))
            if needs_nav:
                self.page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
                t_end = time.time() + 10.0
                while time.time() < t_end:
                    time.sleep(0.08)
                    ready = self.page.js("""(function(){
                        return document.readyState === 'complete'
                            && !!document.querySelector('textarea')
                            && document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length === 0;
                    })()""")
                    if ready:
                        break
                self._wait_ws_ready(self.page, timeout=12.0)
        except Exception:
            pass

    def ensure_page(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        if self.page is not None and (account_id is None or getattr(self, "current_acc_id", None) == account_id):
            try:
                if self.page.js("!!document.querySelector('textarea')") and self._wait_ws_ready(self.page, timeout=8.0):
                    return self.page
            except Exception:
                pass
        self._close_page()
        # 仅当距离上次 HTTP 续签超过 10 分钟时才在主链路调用 /api/session，避免每次切号重复阻塞
        last_map = getattr(self, "_last_http_renew", None)
        if last_map is None:
            last_map = {}
            self._last_http_renew = last_map
        now_ts = time.time()
        if not account_id or (now_ts - last_map.get(account_id, 0) > 600):
            try:
                renewed = self.renew_session_http(cookies, expires, wake_vm=True)
                if renewed.get("cookies"):
                    cookies = renewed["cookies"]
                if renewed.get("cookies_exp"):
                    expires = renewed["cookies_exp"]
                if account_id:
                    last_map[account_id] = now_ts
            except MuseAuthError:
                raise
            except Exception as e:
                log.warning("预续签 /api/session 失败（继续尝试浏览器加载）: %s", e)

        page = self._open_page()
        self._apply_cookies(page, cookies, expires)
        page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
        self.page = page  # Cleanup must also own pages that never become ready.
        if self._wait_ws_ready(page, timeout=self.cfg.login_wait):
            self.current_acc_id = account_id
            return page
        try:
            body = (page.js("document.body ? document.body.innerText.slice(0,1200) : ''") or "").lower()
        except Exception:
            body = ""
        self._close_page()
        if "connecting..." in body:
            raise MuseGenerationError("页面网关连接超时 (Connecting...)，正在自动切换账号")
        # Distinguish an expired session from a page that never became ready.
        if re.search(r"log in|sign in|create an account|登录|use another account", body):
            raise MuseAuthError("会话已被 muse.ai 登出（可能被其它登录挤掉或触发风控），"
                                "请用浏览器扩展重新导入 cookie")
        raise MuseGenerationError("muse.ai 页面加载超时（未出现聊天输入框），请检查服务器网络后重试；未确认会话失效")

    def close_current_page(self):
        """显式释放当前线程所持有的页面和隔离上下文。"""
        self._close_page()

    def refresh(self, cookies: dict, expires: dict | None = None):
        self._close_page()
        return self.ensure_page(cookies, expires)

    # ---------------- 额度查询（Settings 面板） ----------------
    # muse.ai 的额度在底部 Settings 菜单 → Settings 项 → 设置面板的
    # General → Usage 区块里，形如：
    #   Free plan
    #   Weekly limit resets on Sep 30
    #   1% used
    #   Additional tokens / Never expires / 0% used (2B tokens left)
    def _click_point(self, x: int, y: int):
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": x, "y": y,
                            "button": "left", "clickCount": 1})

    _CLICK_JS = (
        "(function(){var sel=%s;"
        "var b=[...document.querySelectorAll(sel)]"
        ".filter(function(x){return x.offsetParent!==null;})[0];"
        "if(!b)return null;var r=b.getBoundingClientRect();"
        "return JSON.stringify({x:Math.round(r.x+r.width/2),"
        "y:Math.round(r.y+r.height/2)});})()")

    _QUOTA_SETTINGS_JS = r"""(() => {
        const visible = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden';
        const labels = ['Settings', '設定', '设置'];
        const b = [...document.querySelectorAll('[data-testid="hatch-dock-more"]')].find(visible)
            || [...document.querySelectorAll('button,[role=button]')].find(e =>
                visible(e) && labels.includes((e.getAttribute('aria-label') || '').trim()));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    })()"""

    _QUOTA_MENU_JS = r"""(() => {
        const visible = e => e.getClientRects().length > 0
            && getComputedStyle(e).visibility !== 'hidden';
        const labels = ['Settings', '設定', '设置'];
        const exact = e => labels.includes((e.textContent || '').trim());
        // Menu items include a shortcut suffix (for example 設定Ctrl+,).
        const b = [...document.querySelectorAll('[role=menuitem]')].find(e =>
            visible(e) && (exact(e) || [...e.querySelectorAll('span,div')].some(exact)))
            || [...document.querySelectorAll('[role=menu] button,[role=menu] span,[role=menu] div')]
                .find(e => visible(e) && exact(e));
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    })()"""

    _QUOTA_DIALOG_JS = r"""(() => {
        const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
            .find(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
        return d ? (d.innerText || '') : '';
    })()"""

    def _quota_click(self, expr: str, error: str, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while True:
            raw = self.page.js(expr)
            if raw:
                pt = json.loads(raw)
                self._click_point(pt['x'], pt['y'])
                return
            if time.monotonic() >= deadline:
                raise MuseGenerationError(error)
            time.sleep(0.15)

    def _dismiss_quota_ui(self):
        # A failed lookup can leave a menu open; clean up menus and dialogs.
        for _ in range(2):
            for typ in ('keyDown', 'keyUp'):
                self.page.send('Input.dispatchKeyEvent',
                               {'type': typ, 'key': 'Escape', 'code': 'Escape',
                                'windowsVirtualKeyCode': 27, 'nativeVirtualKeyCode': 27})
            time.sleep(0.1)  # Let the UI finish closing before the next interaction.

    def quota(self, cookies: dict, expires: dict | None = None,
              account_id: str | None = None) -> dict:
        """Read the account's quota without depending on the browser UI language."""
        self.ensure_page(cookies, expires, account_id=account_id)
        self._dismiss_quota_ui()
        try:
            self._quota_click(self._QUOTA_SETTINGS_JS, '找不到设置入口（Settings／設定／设置）')
            self._quota_click(self._QUOTA_MENU_JS, '设置菜单未出现可识别的设置项')
            deadline = time.monotonic() + 12.0
            while True:
                parsed = self._parse_quota(self.page.js(self._QUOTA_DIALOG_JS) or '')
                if parsed['found']:
                    return parsed
                if time.monotonic() >= deadline:
                    raise MuseGenerationError('额度面板未读到有效用量；旧缓存未覆盖')
                time.sleep(0.2)
        finally:
            try:
                self._dismiss_quota_ui()
            except Exception:
                log.warning('关闭额度设置面板失败')

    @staticmethod
    def _parse_quota(txt: str) -> dict:
        """Parse English, traditional Chinese and simplified Chinese usage text."""
        normalized = unicodedata.normalize('NFKC', txt or '')
        lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
        # Avoid unrelated account/profile text before the Usage section.
        start = next((i + 1 for i, ln in enumerate(lines)
                      if ln.lower() in ('usage', '用量', '使用量')), 0)
        lines = lines[start:]
        end = next((i for i, ln in enumerate(lines)
                    if ln.lower() in ('language', '语言', '語言')), len(lines))
        lines = lines[:end]
        out: dict = {'raw': '\n'.join(lines[:40])}
        for ln in lines:
            if re.fullmatch(r'.{1,60}?(?:\s+plan|計劃|计划|方案)', ln, re.I):
                out['plan'] = ln
                break
        text = '\n'.join(lines)
        reset = re.search(r'Weekly limit resets? on ([^\n]+)', text, re.I)
        if not reset:
            reset = re.search(
                r'(?:每(?:週|周|星期)|週|周)(?:上限|限額|限额|額度|额度)'
                r'(?:會|会|將|将)?(?:於|于|在)\s*([^\n]+?)\s*(?:重設|重置)', text)
        if reset:
            out['weekly_reset'] = reset.group(1).strip()

        extra_at = next((i for i, ln in enumerate(lines) if re.match(
            r'^(?:(?:Additional|Extra)\s+tokens|(?:額外|额外)(?:的)?(?:符元|符記|代幣|代币|令牌|憑證|凭证|tokens))',
            ln, re.I)), len(lines))

        def percentage(line):
            m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:used\b|已(?:使)?用)', line, re.I)
            if not m:
                m = re.search(r'已(?:使)?用\s*(\d+(?:\.\d+)?)\s*%', line)
            if not m:
                return None
            value = float(m.group(1))
            if not 0 <= value <= 100:
                return None
            return int(value) if value.is_integer() else value

        for ln in lines[:extra_at]:
            value = percentage(ln)
            if value is not None:
                out['weekly_used_pct'] = value
                break
        for ln in lines[extra_at:]:
            value = percentage(ln)
            if value is not None:
                out['extra_used_pct'] = value
                left = re.search(r'\(([^)]+)\)', ln)
                if left:
                    out['extra_left'] = left.group(1).strip()
                break
        if re.search(r'Never expires|永不過期|永不过期', text, re.I):
            out['extra_expires'] = 'never'
        # A plan label alone is not a successful quota read. Preserve old cache.
        out['found'] = 'weekly_used_pct' in out
        return out

    # ---------------- 附件（生成结果） ----------------
    _ATT_JS = (
        "(function(){"
        "var list = []; var seen = new Set();"
        "function addEl(el, tid){"
        "  if(!el || seen.has(el)) return;"
        "  seen.add(el);"
        "  if(el.closest('form, [class*=chat-user-bubble], [class*=\"group/msg\"]')) return;"
        "  var v = el.querySelector('video') || (el.tagName === 'VIDEO' ? el : null);"
        "  var img = el.querySelector('img') || (el.tagName === 'IMG' ? el : null);"
        "  var isVid = (tid || '').includes('video') || !!v;"
        "  var primary = isVid ? (v || img) : (img || v);"
        "  var src = primary ? (primary.currentSrc || primary.src || '') : '';"
        "  if(src && !seen.has(src)){"
        "    seen.add(src);"
        "    list.push({"
        "      tid: tid || el.getAttribute('data-testid') || (isVid ? 'video' : 'image'),"
        "      hasVideo: !!v,"
        "      hasImg: !!img,"
        "      src: src,"
        "      vSrc: v ? (v.currentSrc || v.src || '') : '',"
        "      iSrc: img ? (img.currentSrc || img.src || '') : '',"
        "      w: primary ? (primary.videoWidth || primary.naturalWidth || 0) : 0,"
        "      h: primary ? (primary.videoHeight || primary.naturalHeight || 0) : 0"
        "    });"
        "  }"
        "}"
        "document.querySelectorAll('[data-testid^=\"hatch-chat-attachment-presentation-\"]').forEach(function(a){ addEl(a, a.getAttribute('data-testid')); });"
        "document.querySelectorAll('div[class*=\"hatch-agent-bubble-bg\"] img, div[class*=\"hatch-agent-bubble-bg\"] video').forEach(function(m){"
        "  var s = m.currentSrc || m.src || '';"
        "  if(s && !s.includes('avatar') && !s.includes('emoji')) addEl(m.parentElement || m, 'agent-media');"
        "});"
        "return JSON.stringify(list);"
        "})()"
    )

    def attachments(self) -> list[dict]:
        try:
            raw = self.page.js(self._ATT_JS)
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []

    # ---------------- 发送 ----------------
    # 检查「文字真的进了输入框 + Send 按钮真的被渲染出来」。
    # 两个条件缺一不可：Send 按钮只有 React state 里有文字才会渲染 ——
    # 它在，就说明 React 真的收到了输入（不是 DOM value 被改了而已）。
    _SEND_STATE_JS = (
        "(function(){var ta=document.querySelector('textarea');"
        "var b=[...document.querySelectorAll('button,[role=button]')]"
        ".find(function(x){return /send/i.test(x.getAttribute('aria-label')||'');});"
        "return JSON.stringify({v:ta?ta.value:'',btn:b?(b.disabled?2:1):0});})()"
    )

    def _send(self, prompt: str):
        # 1. 确保 textarea 滚动到视口中央并获得真实焦点
        try:
            self.page.js("""(function(){
                var ta = document.querySelector('textarea');
                if (ta) {
                    ta.scrollIntoView({block: 'center', inline: 'nearest'});
                    ta.focus();
                }
            })()""")
        except Exception:
            pass
        time.sleep(0.1)

        rect = self.page.js(
            "(function(){var t=document.querySelector('textarea');if(!t)return null;"
            "var r=t.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.left+r.width/2),"
            "y:Math.round(r.top+r.height/2)});})()")
        if not rect:
            raise MuseGenerationError("找不到聊天输入框")
        c = json.loads(rect)
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": c["x"], "y": c["y"],
                            "button": "left", "clickCount": 1})
        time.sleep(0.1)

        # 触发 React 18 原型 setter 以及 input/change 事件以同步发送按钮状态
        # （对于 DeepSeek/Codex 等 100KB+ 超长上下文，直接走原型 setter 仅需 <1s，避免 Input.insertText 逐字注入卡死）
        _SETTER_JS = (
            "(function(t){var ta=document.querySelector('textarea');"
            "if(!ta) return 0;"
            "ta.focus();"
            "var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(ta,t);"
            "ta.dispatchEvent(new Event('input',{bubbles:true}));"
            "ta.dispatchEvent(new Event('change',{bubbles:true}));"
            "return (ta.value||'').length;})(%s)")
        val_len = self.page.js(_SETTER_JS % json.dumps(prompt)) or 0
        if not val_len and len(prompt) < 500:
            self.page.send("Input.insertText", {"text": prompt})
            self.page.js(_SETTER_JS % json.dumps(prompt))

        # 等待发送按钮就绪并点击
        clicked = "no-button"
        t_deadline = time.time() + 3.0
        while time.time() < t_deadline:
            res = self.page.js(
                "(function(){var b=[...document.querySelectorAll('button,[role=button]')]"
                ".filter(function(x){return x.offsetParent!==null;})"
                ".find(function(x){return /发送|send/i.test(x.getAttribute('aria-label')||'')"
                "||/发送|send/i.test(x.getAttribute('data-testid')||'')"
                "||/send/i.test(x.innerText||'');});"
                "if(!b)return 'no-button';"
                "if(b.disabled)return 'disabled';"
                "b.click();return 'clicked';})()")
            if res == "clicked":
                clicked = "clicked"
                break
            time.sleep(0.08)

        if clicked != "clicked":
            # 兜底：Ctrl+Enter 或普通 Enter
            for combo in ({"modifiers": 1}, {}):
                for t in ("keyDown", "char", "keyUp"):
                    params = {"type": t, "key": "Enter", "code": "Enter",
                              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                              "modifiers": combo.get("modifiers", 0)}
                    if t == "char":
                        params["text"] = "\r"
                        params["unmodifiedText"] = "\r"
                    self.page.send("Input.dispatchKeyEvent", params)
                time.sleep(1.0)
                try:
                    if self.page.js("!(document.querySelector('textarea')||{value:''}).value"):
                        clicked = "enter-sent"
                        break
                except Exception:
                    pass
        time.sleep(0.3)
        return clicked

    # ---------------- 等待生成 ----------------
    def _last_attachment(self) -> dict | None:
        atts = self.attachments()
        return atts[-1] if atts else None

    def _scroll_bottom(self):
        """滚到聊天底部。muse.ai 的聊天滚动容器是内层 div（不是 document），
        虚拟列表按滚动位置渲染节点 —— 不滚到底，新消息根本不在 DOM 里。"""
        try:
            self.page.js(
                "(function(){"
                "var els=[...document.querySelectorAll('*')].filter(function(e){"
                "var s=getComputedStyle(e);"
                "return (s.overflowY==='auto'||s.overflowY==='scroll')"
                "&&e.scrollHeight>e.clientHeight+100;});"
                "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
                "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
                "var s=document.scrollingElement||document.body;"
                "s.scrollTop=s.scrollHeight;"
                "var el=document.querySelector('textarea');"
                "if(el)el.scrollIntoView({block:'end'});return 1;})()")
        except Exception:  # noqa: BLE001
            pass

    def _wait_attachment(self, baseline_src: str, timeout: int, expect: str,
                         on_progress=None, base_agent_cnt: int = 0,
                         base_att_cnt: int = 0, stop_event=None, baseline_sources=None) -> dict | None:
        baseline_sources = set(baseline_sources or ()) | {baseline_src}
        deadline = time.time() + timeout
        t_start = time.time()
        stable_src, stable_n = "", 0
        last_txt, txt_stable = "", 0
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("客户端已断开连接，终止生成任务")
            time.sleep(0.6)
            self._scroll_bottom()
            atts = self.attachments()
            # 严格按期望类型挑选附件，绝不接受跨模态兜底（如将视频请求降级为图片封面）
            matching_atts = []
            for a in atts:
                tid = (a.get("tid") or "").lower()
                has_video = bool(a.get("hasVideo"))
                src = a.get("src") or ""
                v_src = a.get("vSrc") or ""
                if expect == "video":
                    # tid 含 "video" 不足以命中：muse 会先把 data-testid 标成
                    # ...-presentation-video，此时 <video> 可能尚未挂上，_ATT_JS 的
                    # primary = v || img 会退回封面 img，src/w/h 全是静帧。
                    # 只认真正的视频源（vSrc / hasVideo / 视频扩展名）。
                    is_match = (
                        has_video
                        or bool(v_src)
                        or src.endswith((".mp4", ".webm", ".mov"))
                    )
                else:
                    is_match = ("image" in tid) or (not has_video and not src.endswith((".mp4", ".webm", ".mov")))
                if is_match:
                    matching_atts.append(a)

            att = matching_atts[-1] if matching_atts else None
            if att:
                src = att.get("src") or ""
                v_src = att.get("vSrc") or ""
                w = att.get("w", 0) or 0
                h = att.get("h", 0) or 0
                # 视频任务禁止用封面 src 冒充成品；无 vSrc 时仅接受显式视频后缀的 src
                if expect == "video":
                    check_src = v_src if v_src else (
                        src if src.endswith((".mp4", ".webm", ".mov")) else ""
                    )
                else:
                    check_src = src
                if check_src and check_src not in baseline_sources:
                    if w > 0 and h > 0:
                        return att
                    if check_src == stable_src:
                        stable_n += 1
                    else:
                        stable_src, stable_n = check_src, 0
                    if stable_n >= 1:
                        return att
            elapsed = time.time() - t_start
            if on_progress:
                prog = min(92, int(25 + elapsed * 1.0))
                try:
                    on_progress(prog)
                except Exception:
                    pass
            cur_cnt = base_agent_cnt
            cur_txt = ""
            has_stop = False
            tail = ""
            try:
                st_raw = self.page.js("""(function(){
                    var bs=[].slice.call(document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]'))
                        .filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});
                    var lastTxt = bs.length ? (bs[bs.length-1].innerText||'').trim() : '';
                    var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                        || document.querySelector('button[aria-label*="Stop" i]')
                        || document.querySelector('button[aria-label*="停止"]'));
                    var tail = document.body ? (document.body.innerText||'').slice(-700) : '';
                    return JSON.stringify({cnt: bs.length, txt: lastTxt, stop: hasStop, tail: tail});
                })()""")
                st = json.loads(st_raw) if st_raw else {}
            except Exception:
                st = {}
            tail = st.get("tail") or ""
            if re.search(r"额度不足|积分不足|out of credits|达到上限|token limit", tail):
                raise MuseGenerationError("账号额度不足")
            # A Stop button is also shown for messages never acknowledged by the gateway.
            if elapsed > 30.0 and cur_cnt == base_agent_cnt and re.search(
                    r"Still sending|Delivery not confirmed|正在发送|未确认送达", tail):
                raise MuseGenerationError("提示词未成功送达后端（Still sending / Delivery not confirmed），终止等待并重试")
            cur_cnt = st.get("cnt") or 0
            cur_txt = st.get("txt") or ""
            has_stop = bool(st.get("stop"))
            if cur_cnt > base_agent_cnt and cur_txt and not has_stop and len(atts) <= base_att_cnt:
                # 检查是否包含媒体文件生成关键词（如 .webp, .png, .mp4, imagine_media 等），若是则说明正在产出媒体，绝不能误判为纯文本拒答
                is_media_report = bool(re.search(r"\.(?:webp|png|jpe?g|mp4|webm)|imagine_media|deliverable|generated\s+.*image|verified\s+generated|artifact", cur_txt, re.I))
                if not is_media_report:
                    if cur_txt == last_txt:
                        txt_stable += 1
                    else:
                        last_txt, txt_stable = cur_txt, 0
                    if txt_stable >= 15 and elapsed > 8.0:
                        raise MuseGenerationError(f"模型未生成媒体，仅返回文本: {cur_txt[:120]}")
                else:
                    txt_stable = 0
            else:
                txt_stable = 0
        return None

    # ---------------- 取字节 ----------------
    _EXTRACT_JS = r"""
    (async function(src, expect){
      try{
        var u = src;
        if(!u) return JSON.stringify({ok:false,err:'no-media-src'});
        var r = await fetch(u);
        if(!r.ok) return JSON.stringify({ok:false,err:'media-http-'+r.status});
        var b = await r.blob();
        var ab = await b.arrayBuffer();
        var bytes = new Uint8Array(ab);
        var s = '';
        for(var i=0; i<bytes.length; i+=65536){
          s += String.fromCharCode.apply(null, bytes.subarray(i, i+65536));
        }
        return JSON.stringify({ok:true, mime:b.type||'', size:b.size, url:u, b64:btoa(s)});
      }catch(e){
        return JSON.stringify({ok:false, err:String(e)});
      }
    })(%s, %s)
    """

    # ---------------- 文本 / 代码对话 ----------------
    _AGENT_TEXT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "for(var i=bs.length-1;i>=0;i--){"
        "var cs=bs[i].className||'';"
        "if(/hatch-agent-bubble-bg/.test(cs))return bs[i].innerText||'';}"
        "return '';})()"
    )
    _USER_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/chat-user-bubble/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _AGENT_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/hatch-agent-bubble-bg/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _POLL_CHAT_JS = (
        "(function(){"
        "var els=[...document.querySelectorAll('*')].filter(function(e){"
        "var s=getComputedStyle(e);"
        "return (s.overflowY==='auto'||s.overflowY==='scroll')&&e.scrollHeight>e.clientHeight+100;});"
        "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
        "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
        "var scope=document.querySelector('main,[class*=\"chat-scroll\"],[class*=\"hatch-chat-scroll\"]')||document.body;"
        "var bs=[].slice.call(scope.querySelectorAll('div[class*=\"hatch-chat-groupable-bubble\"]'))"
        ".filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});"
        "var nonEmpty=bs.filter(function(b){return ((b.innerText||'').trim().length)>0;});"
        "var txt=nonEmpty.length?(nonEmpty[nonEmpty.length-1].innerText||'').trim():'';"
        "var stop=!!(document.querySelector('[data-testid=\"hatch-composer-stop-button\"]')"
        "||document.querySelector('button[aria-label*=\"Stop\" i]')"
        "||document.querySelector('button[aria-label*=\"停止\"]'));"
        "return JSON.stringify({cnt:nonEmpty.length,total:bs.length,txt:txt,stop:stop});})()"
    )

    def _agent_text(self) -> str:
        """最后一个助手气泡的文本（取不到就返回空串）。"""
        try:
            return (self.page.js(self._AGENT_TEXT_JS) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _agent_count(self) -> int:
        try:
            return int(self.page.js(self._AGENT_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _user_count(self) -> int:
        try:
            return int(self.page.js(self._USER_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _poll_chat(self) -> tuple[int, str, bool]:
        try:
            raw = self.page.js(self._POLL_CHAT_JS)
            if raw:
                d = json.loads(raw)
                return int(d.get("cnt") or 0), (d.get("txt") or "").strip(), bool(d.get("stop"))
        except Exception:
            pass
        return 0, "", False

    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None,
                    stop_event=None):
        """发一条消息，流式 yield 增量文本。"""
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 300))
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=True)
        base_agent, base_text, _ = self._poll_chat()
        self._send(prompt)

        t_sent = time.time()
        deadline = t_sent + timeout
        first_token_deadline = min(deadline, t_sent + 40.0)
        sent, last, stable = "", None, 0
        got_first = False

        # 1. 等待助手生成并开始吐字（高频 60ms 采样，捕获到首批增量文字瞬间 yield 出去）
        while time.time() < first_token_deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.06)
            cnt, cur, has_stop = self._poll_chat()
            if not cur or (cnt <= base_agent and cur == base_text):
                if time.time() - t_sent > 14.0:
                    try:
                        tail = self.page.js("document.body.innerText.slice(-500)") or ""
                    except Exception:
                        tail = ""
                    if "Still sending" in tail or "Connecting..." in tail:
                        raise MuseGenerationError("云端 VM 连接超时 (Still sending)")
                continue

            delta = cur[len(sent):] if cur.startswith(sent) else cur
            if delta:
                sent = cur
                yield delta
                last = cur
                got_first = True
                break

        if not got_first:
            raise MuseGenerationError("等待助手首字响应超时")

        # 2. 持续捕获增量文本
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.06)
            cnt, cur, has_stop = self._poll_chat()
            if not cur:
                continue

            if cur != last:
                delta = cur[len(sent):] if cur.startswith(sent) else cur
                if delta:
                    sent = cur
                    yield delta
                last, stable = cur, 0
            else:
                stable += 1
                # Stop 按钮消失说明前端生成彻底结束，连续 3 次（约 0.18s）无新文本即正常退出
                if not has_stop and stable >= 3:
                    return
                # Stop 按钮仍在时绝不过早截断（模型思考、代码块或网络抖动），允许等待至 stable >= 80（约 5s）防卡死
                if has_stop and stable >= 80:
                    return

        raise MuseGenerationError("等待助手回复超时")

    def chat(self, cookies: dict, prompt: str, expires: dict | None = None,
             timeout: int | None = None, account_id: str | None = None) -> str:
        """发一条消息，返回完整回复文本（非流式）。"""
        out = ""
        for chunk in self.chat_stream(cookies, prompt, expires, timeout, account_id=account_id):
            out += chunk
        return out

    def extract_bytes(self, src: str, expect: str = "image", retries: int = 4):
        last = "未知"
        for _ in range(retries):
            raw = self.page.js(self._EXTRACT_JS % (json.dumps(src), json.dumps(expect)),
                               await_promise=True, timeout=600)
            try:
                info = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                info = {"ok": False, "err": f"解析失败 {str(raw)[:150]}"}
            if info.get("ok"):
                return base64.b64decode(info["b64"]), info.get("mime", ""), info.get("url", "")
            last = info.get("err", "未知")
            time.sleep(2)
        raise MuseGenerationError(f"未能取回生成结果: {last}")

    # ---------------- 下载兜底 ----------------
    def _download_fallback(self, src: str, timeout: int = 180) -> str | None:
        before = set(os.listdir(self.cfg.download_dir))
        # ponytail: fail closed when the selected result has no local download;
        # never click an unrelated/global button that can return the upload.
        clicked = self.page.js("""(function(src){
            var media=[...document.querySelectorAll('img,video')]
                .find(m=>(m.currentSrc||m.src||'')===src);
            var node=media && media.closest('[data-testid^="hatch-chat-attachment-presentation-"]');
            if(!node) return 'none';
            node=node.closest('[class*="group/widget-presentation"]')||node;
            var b=[...node.querySelectorAll('button,[role=button]')]
                .find(x=>/下载|保存|download/i.test(x.getAttribute('aria-label')||''));
            if(!b) return 'none';
            b.click();return 'ok';
        })(%s)""" % json.dumps(src))
        if clicked == "none":
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            new = [f for f in (set(os.listdir(self.cfg.download_dir)) - before)
                   if not f.endswith(".crdownload")]
            if new:
                p = os.path.join(self.cfg.download_dir, max(
                    new, key=lambda f: os.path.getmtime(os.path.join(self.cfg.download_dir, f))))
                if os.path.getsize(p) > 0:
                    return p
        return None

    @staticmethod
    def _normalize_image(img: str) -> tuple[str, str]:
        """将各种形态的图片输入归一为 (base64_str, mime_type)。"""
        if not img:
            return "", "image/png"
        img = str(img).strip()
        if img.startswith("data:"):
            parts = img.split(",", 1)
            mime = "image/png"
            if ";" in parts[0]:
                mime = parts[0].split(";")[0].replace("data:", "").strip()
            return (parts[1].strip() if len(parts) > 1 else ""), mime
        if img.startswith("http://") or img.startswith("https://"):
            try:
                req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    mime = resp.headers.get_content_type() or "image/png"
                    return base64.b64encode(data).decode("ascii"), mime
            except Exception as e:
                log.warning("下载远程参考图失败: %s", e)
                return "", "image/png"
        if os.path.isfile(img):
            try:
                with open(img, "rb") as f:
                    data = f.read()
                    mime = mimetypes.guess_type(img)[0] or "image/png"
                    return base64.b64encode(data).decode("ascii"), mime
            except Exception as e:
                log.warning("读取本地参考图失败: %s", e)
                return "", "image/png"
        return img, "image/png"

    def _clear_attachments(self):
        """清除聊天输入框里遗留的附件缩略图（兼容中/英文 UI）。"""
        try:
            self.page.js(
                "(function(){"
                "var btns=Array.from(document.querySelectorAll('button[aria-label]')).filter(function(b){"
                "var al=b.getAttribute('aria-label')||'';"
                "return /remove\\s*attachment|移除附件|删除附件|移除|删除/i.test(al);"
                "});"
                "btns.forEach(function(b){b.click();});"
                "Array.from(document.querySelectorAll('input[type=\"file\"]')).forEach(function(inp){inp.value='';});"
                "return btns.length;"
                "})()")
            time.sleep(0.3)
        except Exception:
            pass

    def _attach_images(self, image_data: list[str]):
        """逐张附加参考图，并确认全部进入 composer。"""
        images = []
        for index, value in enumerate(image_data):
            if not value:
                continue
            b64, mime = self._normalize_image(value)
            if not b64:
                raise MuseGenerationError(f"第 {index + 1} 张参考图读取失败，已停止生成")
            images.append({"b64": b64, "mime": mime, "index": index + 1})
        if not images:
            return

        self._clear_attachments()

        _INJECT_JS = """
        (async function(images) {
            try {
                // 现网 composer 里可能有多个 file input；优先取位于 composer 内的那个
                var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                if (!inputs.length) return JSON.stringify({ok: false, err: 'no-file-input'});
                var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
                var composer = ov ? ov.closest('form') : null;
                var input = null;
                if (composer) {
                    input = inputs.find(function(x){ return composer.contains(x); }) || null;
                }
                if (!input) {
                    // 退而求其次：取不在对话气泡里的那个 input
                    input = inputs.find(function(x){
                        return !x.closest('[class*=chat-user-bubble], [class*="group/msg"]');
                    }) || inputs[0];
                }
                // 有些版本 accept 为空，补上 image/* 让页面接受本次 File
                if (!input.getAttribute('accept')) input.setAttribute('accept', 'image/*');
                for (var image of images) {
                    var byteChars = atob(image.b64);
                    var byteNumbers = new Array(byteChars.length);
                    for (var i = 0; i < byteChars.length; i++) {
                        byteNumbers[i] = byteChars.charCodeAt(i);
                    }
                    var mime = image.mime || 'image/png';
                    var byteArray = new Uint8Array(byteNumbers);
                    var blob = new Blob([byteArray], {type: mime});
                    var ext = mime.split('/')[1] || 'png';
                    if (ext === 'jpeg') ext = 'jpg';
                    var dt = new DataTransfer();
                    dt.items.add(new File(
                        [blob], 'reference_image_' + image.index + '.' + ext, {type: mime}));
                    input.files = dt.files;
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    await new Promise(function(resolve){ setTimeout(resolve, 350); });
                }
                return JSON.stringify({ok: true, count: images.length});
            } catch(e) {
                return JSON.stringify({ok: false, err: String(e)});
            }
        })(%s)
        """
        try:
            raw_res = self.page.js(
                _INJECT_JS % json.dumps(images), await_promise=True,
                timeout=max(15, len(images) * 3))
            res_obj = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
            if not res_obj.get("ok") or res_obj.get("count") != len(images):
                raise MuseGenerationError("附加参考图失败，已停止生成")
        except Exception as e:
            raise MuseGenerationError("附加参考图失败，已停止生成") from e

        # 等待输入框附件确认；历史图片不能证明本次上传成功。
        # 现网 UI 已切中文：确认按钮 aria-label = "附加文件"/"移除附件"；
        # 同时兼容英文（Remove attachment / Attach file），并以 composer 内缩略图兜底。
        deadline = time.time() + 15.0
        attached_count = 0
        while time.time() < deadline:
            attached_count = self.page.js(
                """(function(){
                var btns = Array.from(document.querySelectorAll('button[aria-label]'));
                var removeCount = btns.filter(function(b){
                    return /remove\\s*attachment|移除附件|删除附件/i.test(b.getAttribute('aria-label') || '');
                }).length;
                if (removeCount) return removeCount;
                // 兜底：composer 内出现附件缩略图（img/video）
                var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
                var composer = ov ? (ov.closest('form') || ov.parentElement.parentElement.parentElement) : null;
                if (composer) {
                    var media = composer.querySelectorAll('img, video');
                    if (media.length > 0) return media.length;
                }
                return 0;
                })()"""
            )
            if int(attached_count or 0) >= len(images):
                break
            time.sleep(0.3)
        else:
            raise MuseGenerationError(
                f"参考图上传未确认（{int(attached_count or 0)}/{len(images)}），已停止生成")
        time.sleep(0.5)

    def _attach_image(self, image_data: str):
        """Backward-compatible single-reference wrapper."""
        self._attach_images([image_data])
    # ---------------- 主流程 ----------------
    def generate(self, cookies: dict, prompt: str, expect: str = "image",
                 timeout: int = 240, expires: dict | None = None, account_id: str | None = None,
                 on_progress=None, reference_image: str | None = None,
                 reference_images: list[str] | None = None,
                 stop_event=None) -> dict:
        deadline = time.monotonic() + timeout
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=False)
        self._scroll_bottom()
        refs = list(reference_images or ([] if not reference_image else [reference_image]))
        if refs:
            self._attach_images(refs)
        else:
            self._clear_attachments()
        atts_before = self.attachments()
        baseline_sources = {a.get(k) for a in atts_before for k in ("src", "vSrc", "iSrc") if a.get(k)}
        base = atts_before[-1] if atts_before else {}
        baseline_src = base.get("src") or ""
        base_agent_cnt = self._agent_count()
        if time.monotonic() >= deadline:
            raise MuseGenerationError("任务准备超时，已停止发送")
        if self._send(prompt) not in ("clicked", "enter-sent"):
            raise MuseGenerationError("提示词发送未确认，已停止生成")
        att = self._wait_attachment(
            baseline_src, max(0, deadline - time.monotonic()), expect, on_progress=on_progress,
            base_agent_cnt=base_agent_cnt, base_att_cnt=len(atts_before),
            stop_event=stop_event, baseline_sources=baseline_sources
        )
        if not att:
            self._debug_dump("no-attachment")
            raise MuseGenerationError("等待生成超时，未出现新的生成结果")

        os.makedirs(self.cfg.media_dir, exist_ok=True)
        data = mime = url = None
        selected_src = (att.get("vSrc") if expect == "video" else None) or att.get("src") or ""
        try:
            data, mime, url = self.extract_bytes(selected_src, expect=expect)
        except Exception:  # noqa: BLE001
            self._debug_dump("extract-fail")

        if data:
            ext = self._pick_ext(mime, url, expect)
            name = f"{uuid.uuid4().hex}{ext}"
            dst = os.path.join(self.cfg.media_dir, name)
            with open(dst, "wb") as f:
                f.write(data)
            kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
            if expect == "video" and kind != "video":
                raise MuseGenerationError(f"期望生成视频，但提取成品类型为图片 ({ext})，拒绝以图片冒充视频交付")
            return {"path": dst, "filename": name, "size": len(data), "ext": ext, "mime": mime,
                    "kind": kind,
                    "via": "blob", "attachment": att.get("tid"),
                    "w": att.get("w"), "h": att.get("h")}

        path = self._download_fallback(selected_src)
        if not path:
            raise MuseGenerationError("已生成但未能取回文件")
        ext = os.path.splitext(path)[1].lower() or ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
        dst = os.path.join(self.cfg.media_dir, name)
        shutil.move(path, dst)
        kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
        if expect == "video" and kind != "video":
            raise MuseGenerationError(f"期望生成视频，但下载成品类型为图片 ({ext})，拒绝以图片冒充视频交付")
        return {"path": dst, "filename": name, "size": os.path.getsize(dst), "ext": ext, "mime": "",
                "kind": kind,
                "via": "download", "attachment": att.get("tid"),
                "w": att.get("w"), "h": att.get("h")}

    @staticmethod
    def _pick_ext(mime: str, url: str, expect: str) -> str:
        m = (mime or "").lower()
        for key, ext in (("mp4", ".mp4"), ("webm", ".webm"), ("png", ".png"),
                         ("jpeg", ".jpg"), ("jpg", ".jpg"), ("webp", ".webp"),
                         ("gif", ".gif")):
            if key in m:
                return ext
        for e in (".mp4", ".webm", ".png", ".jpg", ".webp"):
            if e in (url or "").lower():
                return e
        return ".mp4" if expect == "video" else ".png"

    def _debug_dump(self, tag: str):
        try:
            info = self.page.js(
                "JSON.stringify({atts:[...document.querySelectorAll('" + ATT_SEL + "')]"
                ".map(function(a){var m=a.querySelector('img,video');return {"
                "tid:a.getAttribute('data-testid'),"
                "src:m?(m.currentSrc||m.src||'').slice(0,60):''};}),"
                "buttons:[...document.querySelectorAll('button,[role=button]')]"
                ".filter(b=>b.offsetParent!==null)"
                ".map(b=>b.getAttribute('aria-label')||b.innerText.trim().slice(0,20))"
                ".filter(Boolean).slice(-40),"
                "tail:document.body.innerText.slice(-500)})")
            with open(os.path.join(self.cfg.data_dir, f"debug-{tag}.json"), "w",
                      encoding="utf-8") as f:
                f.write(str(info))
        except Exception:  # noqa: BLE001
            pass
