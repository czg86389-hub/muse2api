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

from cdp import CDP, http_json

log = logging.getLogger("muse2api")

ATT_SEL = '[data-testid^="hatch-chat-attachment-presentation-"]'

# 决定账号生死的核心 cookie（缺失或过期 = 会话失效）
# hatch_gw 当前站点经常不下发，缺了不影响登录和生成。
ESSENTIAL_COOKIES = ("hatch_sess", "hatch_vml", "hatch_native_auth_device")


class MuseAuthError(RuntimeError):
    pass


class MuseGenerationError(RuntimeError):
    pass


def send_wait_budget(ref_count: int | None) -> float:
    """参考图还在上传时，发送按钮会一直禁用。张数越多，等多久。"""
    try:
        n = int(ref_count or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 12.0
    return min(45.0, max(12.0, 8.0 + 6.0 * n))


def send_failure_message(code: str | None) -> str:
    reason = {
        "disabled": "发送按钮仍不可用",
        "missing": "未找到发送按钮",
        "no-button": "未找到发送按钮",
        "hidden": "未找到发送按钮",
        "no-text": "提示词没有写进输入框",
    }.get(code or "", "发送未确认")
    return f"提示词发送未确认，已停止生成（{reason}）"


class MuseEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.browser: CDP | None = None
        # 页面按线程绑定。每个账号有自己的浏览器上下文，任务线程互不覆盖。
        self._tls = threading.local()
        self._sessions: dict[str, dict] = {}
        self._state_lock = threading.Lock()
        self._browser_lock = threading.RLock()
        self.page = None
        self.current_acc_id = None
        self._last_http_renew: dict[str, float] = {}
        self._log = None
        os.makedirs(cfg.profile_dir, exist_ok=True)

    @property
    def page(self):
        tls = getattr(self, "_tls", None)
        if tls is not None and hasattr(tls, "page"):
            return tls.page
        return self.__dict__.get("_page_fallback")

    @page.setter
    def page(self, value):
        tls = getattr(self, "_tls", None)
        if tls is None:
            self.__dict__["_page_fallback"] = value
            return
        tls.page = value

    @property
    def current_acc_id(self):
        tls = getattr(self, "_tls", None)
        if tls is not None and hasattr(tls, "current_acc_id"):
            return tls.current_acc_id
        return self.__dict__.get("_acc_fallback")

    @current_acc_id.setter
    def current_acc_id(self, value):
        tls = getattr(self, "_tls", None)
        if tls is None:
            self.__dict__["_acc_fallback"] = value
            return
        tls.current_acc_id = value

    def has_page(self, account_id: str | None = None) -> bool:
        with self._state_lock:
            if account_id:
                sess = self._sessions.get(account_id)
                return bool(sess and sess.get("page") and not sess.get("stale"))
            return any(s.get("page") and not s.get("stale") for s in self._sessions.values())

    def invalidate(self, account_id: str | None):
        """cookie 更新后，下次进入该账号时重建页面。不打断正在跑的任务。"""
        key = account_id or "_default"
        with self._state_lock:
            sess = self._sessions.get(key)
            if sess:
                sess["stale"] = True

    # ---------------- 浏览器生命周期 ----------------
    def _debug_url(self):
        return f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"

    def start(self):
        if self.proc and self.proc.poll() is None and self.browser:
            return
        with self._browser_lock:
            if self.proc and self.proc.poll() is None and self.browser:
                return
            self._start_locked()

    def _start_locked(self):
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
        with self._state_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            page = sess.get("page")
            if page:
                try:
                    page.close()
                except Exception:
                    pass
        if self.browser:
            for sess in sessions:
                ctx = sess.get("context_id")
                if not ctx:
                    continue
                try:
                    self.browser.send("Target.disposeBrowserContext",
                                      {"browserContextId": ctx})
                except Exception:
                    pass
            try:
                self.browser.close()
            except Exception:
                pass
        self.browser = None
        self.page = None
        self.current_acc_id = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    # ---------------- 页面 ----------------
    def _browser_call(self, method: str, params: dict | None = None) -> dict:
        with self._browser_lock:
            if not self.browser:
                raise MuseGenerationError("浏览器未启动")
            msg = self.browser.send(method, params or {})
        return msg.get("result") or {}

    def _session_key(self, account_id: str | None) -> str:
        return account_id or "_default"

    def _bind(self, key: str):
        with self._state_lock:
            sess = self._sessions.get(key)
            page = sess.get("page") if sess else None
        if getattr(self, "_tls", None) is not None:
            self._tls.key = key
        self.page = page
        self.current_acc_id = None if key == "_default" else key

    def _save_page(self, key: str, page):
        with self._state_lock:
            sess = self._sessions.setdefault(key, {})
            sess["page"] = page
            sess["stale"] = False
            target_id = getattr(page, "_muse_target_id", None)
            if target_id:
                sess["target_id"] = target_id

    def _close_target(self, target_id: str | None):
        if not target_id or not self.browser:
            return
        try:
            self._browser_call("Target.closeTarget", {"targetId": target_id})
        except Exception:
            log.warning("关闭标签页失败")

    def _discard_page(self, page):
        target_id = getattr(page, "_muse_target_id", None)
        try:
            page.close()
        except Exception:
            pass
        self._close_target(target_id)

    def _close_page_only(self, key: str):
        with self._state_lock:
            sess = self._sessions.get(key)
            page = sess.get("page") if sess else None
            target_id = (sess or {}).get("target_id") or getattr(page, "_muse_target_id", None)
            if sess:
                sess["page"] = None
                sess["target_id"] = None
        if page:
            try:
                page.close()
            except Exception:
                pass
        self._close_target(target_id)
        tls = getattr(self, "_tls", None)
        if tls is not None and getattr(tls, "key", None) == key:
            self.page = None

    def drop_session(self, account_id: str | None):
        """关掉一个账号的页面和独立 cookie 环境，不影响其他账号。"""
        key = self._session_key(account_id)
        with self._state_lock:
            sess = self._sessions.pop(key, None)
        tls = getattr(self, "_tls", None)
        if tls is not None and getattr(tls, "key", None) == key:
            self.page = None
            self.current_acc_id = None
        if not sess:
            return
        page = sess.get("page")
        target_id = sess.get("target_id") or getattr(page, "_muse_target_id", None)
        if page:
            try:
                page.close()
            except Exception:
                pass
        self._close_target(target_id)
        ctx = sess.get("context_id")
        if ctx and self.browser:
            try:
                self._browser_call("Target.disposeBrowserContext",
                                   {"browserContextId": ctx})
            except Exception:
                log.warning("关闭账号 %s 的浏览器会话失败", key)

    def _ensure_context(self, key: str) -> str:
        with self._state_lock:
            sess = self._sessions.get(key)
            ctx = sess.get("context_id") if sess else None
        if ctx:
            return ctx
        try:
            result = self._browser_call("Target.createBrowserContext", {})
        except Exception as exc:
            raise MuseGenerationError(f"无法为账号创建独立浏览器会话: {exc}") from exc
        ctx = result.get("browserContextId")
        if not ctx:
            raise MuseGenerationError("浏览器没有返回独立会话编号")
        extra = None
        with self._state_lock:
            sess = self._sessions.setdefault(key, {})
            existing = sess.get("context_id")
            if existing:
                extra = ctx
                ctx = existing
            else:
                sess["context_id"] = ctx
                log.info("【账号并发】为 %s 创建独立浏览器会话", key)
        if extra:
            try:
                self._browser_call("Target.disposeBrowserContext",
                                   {"browserContextId": extra})
            except Exception:
                log.warning("关闭重复的浏览器会话失败")
        return ctx

    def _wait_target_ws(self, target_id: str) -> str:
        import requests
        last = None
        for _ in range(30):
            try:
                pages = requests.get(
                    f"http://127.0.0.1:{self.cfg.cdp_port}/json/list", timeout=3).json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.1)
                continue
            for p in pages:
                if p.get("id") == target_id and p.get("webSocketDebuggerUrl"):
                    return p["webSocketDebuggerUrl"]
            time.sleep(0.1)
        raise MuseGenerationError(f"新标签页未就绪: {last}")

    def _open_page(self, account_key: str | None = None):
        """在指定账号的独立上下文里开一个标签。不关闭其他账号的页面。"""
        self.start()
        params = {"url": "about:blank"}
        if account_key:
            params["browserContextId"] = self._ensure_context(account_key)
        try:
            result = self._browser_call("Target.createTarget", params)
        except Exception as exc:
            raise MuseGenerationError(f"无法打开账号标签页: {exc}") from exc
        target_id = result.get("targetId")
        if not target_id:
            raise MuseGenerationError("浏览器没有返回新标签页")
        page = CDP(self._wait_target_ws(target_id), timeout=180)
        page._muse_target_id = target_id
        page._muse_download_dir = self._download_dir(account_key)
        page.send("Network.enable")
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Browser.setDownloadBehavior",
                  {"behavior": "allow", "downloadPath": page._muse_download_dir})
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
        if wake_vm and vm_id and vm_state != "DISABLED":
            try:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v)
                rw = requests.post("https://muse.ai/api/hatch/vm/wake", headers=headers, json={
                    "vm_id": vm_id,
                    "retry_count": 0,
                    "connect_attempt_id": str(uuid.uuid4()),
                }, timeout=8)
                wake_ok = rw.status_code == 200
            except Exception:
                pass
        return {
            "ok": sj.get("status") == "assigned",
            "status": sj.get("status"),
            "vm_id": vm_id,
            "vm_state": vm_state,
            "wake_ok": wake_ok,
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
                    return 'ready';
                })()""")
                if st == "ready":
                    return True
            except Exception:
                pass
            time.sleep(0.08)
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
                var ta = document.querySelector('textarea');
                var dirtyDraft = !!(ta && (ta.value || '').replace(/\\s/g, '').length);
                var dirtyFiles = Array.prototype.some.call(document.querySelectorAll('button[aria-label]'), function(el){
                    return /^remove attachment$|^移除附件$|^删除附件$/i.test((el.getAttribute('aria-label') || '').trim());
                });
                if (hasStop || hasStuck || hasAtts || dirtyDraft || dirtyFiles) return true;
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
        key = self._session_key(account_id)
        with self._state_lock:
            sess = self._sessions.get(key)
            stale = bool(sess and sess.get("stale"))
        if stale:
            self.drop_session(account_id)
        self._bind(key)
        if self.page is not None:
            try:
                if self.page.js("!!document.querySelector('textarea')"):
                    return self.page
            except Exception:
                pass
            self._close_page_only(key)
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

        page = self._open_page(key)
        self._apply_cookies(page, cookies, expires)
        page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
        for _ in range(self.cfg.login_wait * 2):
            time.sleep(0.15)
            try:
                if page.js("!!document.querySelector('textarea')"):
                    self._save_page(key, page)
                    self._bind(key)
                    self._wait_ws_ready(page, timeout=15.0)
                    return page
            except Exception:
                pass
        try:
            if page.js("!!document.querySelector('textarea')"):
                self._save_page(key, page)
                self._bind(key)
                self._wait_ws_ready(page, timeout=15.0)
                return page
        except Exception:  # noqa: BLE001
            pass
        # 区分「会话失效（被踢回登录页）」和「页面加载卡住」，错误提示才能对症
        try:
            body = (page.js("document.body.innerText.slice(0,1200)") or "").lower()
        except Exception:  # noqa: BLE001
            body = ""
        self._discard_page(page)
        if re.search(r"log in|sign in|create an account|登录|use another account", body):
            raise MuseAuthError("会话已被 muse.ai 登出（可能被其它登录挤掉或触发风控），"
                                "请用浏览器扩展重新导入 cookie")
        raise MuseGenerationError("muse.ai 页面加载超时（未出现聊天输入框），请检查服务器网络后重试；未确认会话失效")

    def refresh(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        key = self._session_key(account_id)
        self._close_page_only(key)
        return self.ensure_page(cookies, expires, account_id=account_id)

    # ---------------- 额度查询（Settings 面板） ----------------
    # muse.ai 的额度在底部 Settings 菜单 → Settings 项 → 设置面板的
    # General → Usage 区块里，形如：
    #   Free plan
    #   Weekly limit resets on Sep 30
    #   1% used
    #   Additional tokens / Never expires / 0% used (2B tokens left)
    def _click_point(self, x: int, y: int):
        self.page.send("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": x, "y": y})
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

    def quota(self, cookies: dict, expires: dict | None = None, account_id: str | None = None) -> dict:
        """打开 Settings 面板读额度。返回结构化 dict；读不到时 raise。"""
        self.ensure_page(cookies, expires, account_id=account_id)
        p = self.page
        time.sleep(1)

        # 1) 点左下角 Settings 按钮（aria-label=Settings）
        raw = p.js(self._CLICK_JS % json.dumps('button[aria-label="Settings"]'))
        if not raw:
            raise MuseGenerationError("找不到 Settings 按钮")
        pt = json.loads(raw)
        self._click_point(pt["x"], pt["y"])
        time.sleep(1.6)

        # 2) 点弹出的菜单里文本为 Settings 的项
        raw = p.js(
            "(function(){"
            "var els=[...document.querySelectorAll('div,span,li,[role=menuitem],button')]"
            ".filter(function(e){return e.offsetParent!==null"
            "&&(e.textContent||'').trim()==='Settings'"
            "&&e.getAttribute('aria-label')!=='Settings'"
            "&&e.children.length<=3;});"
            "if(!els.length)return null;"
            "var el=els[els.length-1];var r=el.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.x+r.width/2),"
            "y:Math.round(r.y+r.height/2)});})()")
        if not raw:
            raise MuseGenerationError("Settings 菜单未弹出")
        pt = json.loads(raw)
        self._click_point(pt["x"], pt["y"])
        time.sleep(3.0)

        # 3) 读设置面板文本
        txt = ""
        for _ in range(6):
            txt = p.js(
                "(function(){var d=document.querySelector('[role=dialog],[aria-modal=true]');"
                "return d?(d.innerText||''):'';})()") or ""
            if "Usage" in txt or "used" in txt:
                break
            time.sleep(1.2)

        # 4) 关闭面板（Escape）
        for t in ("keyDown", "keyUp"):
            p.send("Input.dispatchKeyEvent",
                   {"type": t, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
        time.sleep(0.5)

        return self._parse_quota(txt)

    @staticmethod
    def _parse_quota(txt: str) -> dict:
        """从设置面板文本解析额度字段。"""
        lines = [ln.strip() for ln in (txt or "").split("\n") if ln.strip()]
        out: dict = {"raw": "\n".join(lines[:40])}
        # 计划名：Free plan / xxx plan
        for ln in lines:
            m = re.match(r"^(.+?)\s*plan$", ln, re.I)
            if m:
                out["plan"] = ln
                break
        # Weekly limit resets on Sep 30
        m = re.search(r"Weekly limit resets? on (.+)", txt or "")
        if m:
            out["weekly_reset"] = m.group(1).strip()
        # 周用量：第一个 "N% used"（出现在 plan 行之后）
        m = re.search(r"(\d+)%\s*used", txt or "")
        if m:
            out["weekly_used_pct"] = int(m.group(1))
        # 额外代币："0% used (2B tokens left)"
        m = re.search(r"(\d+)%\s*used\s*\(([^)]+)\)", txt or "")
        if m:
            out["extra_used_pct"] = int(m.group(1))
            out["extra_left"] = m.group(2).strip()
        if "Never expires" in (txt or ""):
            out["extra_expires"] = "never"
        out["found"] = bool(out.get("plan") or "weekly_used_pct" in out)
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
    # 只认 aria-label 恰好是 Send/发送，或 composer 自己的 data-testid。
    # 导航按钮里只要出现 send 字样就不能点。innerText 含 send 也不算。
    _COMPOSER_JS = r"""(function(){
        function visible(el){
            if(!el) return false;
            var r = el.getBoundingClientRect();
            if(r.width < 2 || r.height < 2) return false;
            var s = window.getComputedStyle(el);
            if(s.display === 'none' || s.visibility === 'hidden') return false;
            return true;
        }
        function blocked(el){
            if(el.disabled || el.getAttribute('aria-disabled') === 'true') return true;
            return window.getComputedStyle(el).pointerEvents === 'none';
        }
        var ta = document.querySelector('textarea');
        var nodes = Array.prototype.slice.call(document.querySelectorAll('button,[role="button"]'));
        function isSend(el){
            var al = (el.getAttribute('aria-label') || '').trim().toLowerCase();
            var id = (el.getAttribute('data-testid') || '').trim().toLowerCase();
            return al === 'send' || al === '发送'
                || id === 'hatch-composer-send' || id === 'hatch-composer-send-button';
        }
        var send = null;
        for(var i = 0; i < nodes.length; i++){
            if(isSend(nodes[i])){ send = nodes[i]; break; }
        }
        var stopEl = document.querySelector('[data-testid="hatch-composer-stop-button"]');
        if(!stopEl){
            for(var j = 0; j < nodes.length; j++){
                var al2 = (nodes[j].getAttribute('aria-label') || '').trim().toLowerCase();
                if(al2 === 'stop' || al2 === '停止' || al2 === 'stop generating'){
                    stopEl = nodes[j]; break;
                }
            }
        }
        var removes = 0;
        var fileBtns = document.querySelectorAll('button[aria-label]');
        for(var k = 0; k < fileBtns.length; k++){
            var al3 = (fileBtns[k].getAttribute('aria-label') || '').trim();
            if(/^remove attachment$|^移除附件$|^删除附件$/i.test(al3)) removes++;
        }
        var state = 'missing';
        var x = 0, y = 0;
        if(send){
            var r = send.getBoundingClientRect();
            x = Math.round(r.left + r.width / 2);
            y = Math.round(r.top + r.height / 2);
            if(!visible(send)) state = 'hidden';
            else if(blocked(send)) state = 'disabled';
            else state = 'ready';
        }
        return JSON.stringify({
            ta: ta ? (ta.value || '').length : -1,
            state: state, x: x, y: y,
            stop: !!(stopEl && visible(stopEl)),
            removes: removes
        });
    })()"""

    def _composer_state(self) -> dict:
        try:
            raw = self.page.js(self._COMPOSER_JS)
            if isinstance(raw, str) and raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001
            pass
        return {"state": "missing", "ta": -1, "x": 0, "y": 0, "stop": False, "removes": 0}

    def _set_textarea(self, text: str) -> int:
        """写入输入框，并清掉 React _valueTracker，让发送按钮跟着文字出现。"""
        payload = json.dumps(text, ensure_ascii=False)
        payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        js = (
            "(function(t){"
            "var ta=document.querySelector('textarea');"
            "if(!ta) return 0;"
            "ta.focus();"
            "var desc=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');"
            "if(!desc||!desc.set) return 0;"
            "var tracker=ta._valueTracker;"
            "if(tracker&&tracker.setValue){"
            "try{tracker.setValue(t?'':' ');}catch(e){}"
            "}"
            "desc.set.call(ta,t);"
            "try{ta.dispatchEvent(new InputEvent('input',{bubbles:true,cancelable:true,inputType:'insertText'}));}"
            "catch(e){ta.dispatchEvent(new Event('input',{bubbles:true}));}"
            "ta.dispatchEvent(new Event('change',{bubbles:true}));"
            "return (ta.value||'').length;"
            "})(" + payload + ")"
        )
        try:
            return int(self.page.js(js) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _wait_submit(self, seconds: float) -> bool:
        """点过发送之后，输入框被清空或出现停止按钮，才算真正发出。"""
        deadline = time.time() + max(0.0, seconds)
        empty_hits = 0
        while True:
            st = self._composer_state()
            if st.get("stop"):
                return True
            if st.get("ta") == 0:
                empty_hits += 1
                if empty_hits >= 2:
                    return True
            else:
                empty_hits = 0
            if time.time() >= deadline:
                return False
            time.sleep(0.2)

    def _press_enter(self):
        self.page.js("(function(){var ta=document.querySelector('textarea');if(ta)ta.focus();})()")
        for modifiers in (0, 2):
            for kind in ("keyDown", "char", "keyUp"):
                params = {
                    "type": kind, "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                    "modifiers": modifiers,
                }
                if kind == "char":
                    params["text"] = "\r"
                    params["unmodifiedText"] = "\r"
                self.page.send("Input.dispatchKeyEvent", params)
            time.sleep(0.15)

    def _send(self, prompt: str, wait: float | None = None):
        budget = 12.0 if wait is None else max(3.0, float(wait))
        started = time.time()
        deadline = started + budget
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
        self._click_point(int(c["x"]), int(c["y"]))
        time.sleep(0.1)

        # 超长提示词走原型 setter。提示词里可能有 %，不能再用 % 拼接脚本。
        val_len = self._set_textarea(prompt)
        if not val_len and prompt and len(prompt) < 500:
            self.page.send("Input.insertText", {"text": prompt})
            val_len = self._set_textarea(prompt)
        if prompt and not val_len:
            log.warning("提示词没有写进输入框（%s 字）", len(prompt))
            return "no-text"

        last = {"state": "missing", "ta": val_len, "removes": 0}
        clicks = 0
        entered = False
        empty_hits = 0
        next_click = 0.0
        next_nudge = time.time() + 2.0
        while time.time() < deadline:
            st = self._composer_state()
            last = st
            if clicks or entered:
                if st.get("stop"):
                    empty_hits = 2
                elif st.get("ta") == 0:
                    empty_hits += 1
                else:
                    empty_hits = 0
                if empty_hits >= 2:
                    how = "enter-sent" if entered and not clicks else "clicked"
                    if time.time() - started > 5:
                        log.info("提示词已送出（等待 %.1f 秒，方式 %s）", time.time() - started, how)
                    return how
            # 上传未完成时按钮是 disabled，文字还在，不要重写。按钮还没出现才再写一次。
            if st.get("state") in ("missing", "hidden") and not st.get("stop") and time.time() >= next_nudge:
                self._set_textarea(prompt)
                next_nudge = time.time() + 2.0
                time.sleep(0.15)
                continue
            if (
                st.get("state") == "ready"
                and clicks < 2
                and time.time() >= next_click
                and ((st.get("x") or 0) or (st.get("y") or 0))
            ):
                self._click_point(int(st.get("x") or 0), int(st.get("y") or 0))
                clicks += 1
                next_click = time.time() + 2.0
                if self._wait_submit(min(2.0, max(0.0, deadline - time.time()))):
                    if time.time() - started > 5:
                        log.info("提示词已送出（等待 %.1f 秒，方式 clicked）", time.time() - started)
                    return "clicked"
                continue
            if st.get("state") == "ready" and clicks and not entered:
                self._press_enter()
                entered = True
                if self._wait_submit(min(2.0, max(0.0, deadline - time.time()))):
                    if time.time() - started > 5:
                        log.info("提示词已送出（等待 %.1f 秒，方式 enter-sent）", time.time() - started)
                    return "enter-sent"
            time.sleep(0.2)

        log.warning(
            "提示词发送未确认 state=%s ta=%s removes=%s clicks=%s wait=%.0f",
            last.get("state"), last.get("ta"), last.get("removes"), clicks, budget,
        )
        return last.get("state") or "no-button"

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
        fallback_since = 0.0
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("客户端已断开连接，终止生成任务")
            time.sleep(0.6)
            self._scroll_bottom()
            atts = self.attachments()
            # 按期望类型挑选候选：视频请求优先挑带 video 的附件；
            # 若无匹配（muse 有时先渲染封面静帧），先记下图片兜底，
            # 再给视频节点一段宽限期，避免把封面 img 误当成品返回。
            att = None
            fallback_att = None
            if atts:
                def _score(a):
                    tid = (a.get("tid") or "").lower()
                    has_video = bool(a.get("hasVideo"))
                    if expect == "video":
                        return 2 if (has_video or "video" in tid) else 1
                    return 2 if (("image" in tid) or (not has_video)) else 1
                best = max(atts, key=_score)
                if _score(best) >= 2:
                    att = best
                else:
                    fallback_att = atts[-1]
            if att is None and fallback_att is not None and expect == "video":
                # 图片兜底：只有宽限期内仍未出现视频节点才接受
                if fallback_since == 0.0:
                    fallback_since = time.time()
                if time.time() - fallback_since >= 15.0:
                    att = fallback_att
            if att:
                src = att.get("src") or ""
                v_src = att.get("vSrc") or ""
                tid = att.get("tid") or ""
                w = att.get("w", 0) or 0
                h = att.get("h", 0) or 0
                has_video = att.get("hasVideo", False)
                is_fallback = att is fallback_att and att is not None and not (
                    has_video or "video" in (tid or "").lower()
                ) and expect == "video"
                if is_fallback:
                    want = True  # 宽限期已过，接受图片兜底结果
                elif expect == "video":
                    want = has_video or ("video" in tid) or ("video" in src) or ("video" in v_src) or src.endswith((".mp4", ".webm", ".mov"))
                else:
                    want = ("image" in tid) or (not has_video)
                check_src = v_src if (expect == "video" and v_src) else src
                if check_src and check_src not in baseline_sources and want:
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
            # Sidebar/stale connection text does not prove this generation failed.
            # The caller's generation deadline remains the bounded timeout.
            # 快速失败：如果助手已经完成了纯文字回复（无 Stop 按钮且无新附件），且并非正在生成媒体的报告
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
        sent = self._send(prompt)
        if sent not in ("clicked", "enter-sent"):
            raise MuseGenerationError(send_failure_message(sent))

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
    def _download_dir(self, account_key: str | None = None) -> str:
        """每个账号单独的下载目录，避免并发兜底下载拿错文件。"""
        key = account_key or self._session_key(self.current_acc_id)
        folder = os.path.join(self.cfg.download_dir, key)
        os.makedirs(folder, exist_ok=True)
        return folder

    def _download_fallback(self, src: str, timeout: int = 180) -> str | None:
        folder = getattr(self.page, "_muse_download_dir", None) or self._download_dir()
        before = set(os.listdir(folder))
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
            new = [f for f in (set(os.listdir(folder)) - before)
                   if not f.endswith(".crdownload")]
            if new:
                p = os.path.join(folder, max(
                    new, key=lambda f: os.path.getmtime(os.path.join(folder, f))))
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
        """去掉输入框里残留的参考图。只点「移除附件」，避免误点别的删除按钮。"""
        try:
            for _ in range(12):
                raw = self.page.js(
                    "(function(){"
                    "var b=Array.prototype.find.call(document.querySelectorAll('button[aria-label]'),function(el){"
                    "var al=(el.getAttribute('aria-label')||'').trim();"
                    "if(!/^remove attachment$|^移除附件$|^删除附件$/i.test(al)) return false;"
                    "var r=el.getBoundingClientRect();"
                    "return r.width>1&&r.height>1;"
                    "});"
                    "if(!b){"
                    "Array.prototype.forEach.call(document.querySelectorAll('input[type=\"file\"]'),function(inp){inp.value='';});"
                    "return null;"
                    "}"
                    "var r=b.getBoundingClientRect();"
                    "return JSON.stringify({x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)});"
                    "})()")
                if not raw:
                    break
                pos = json.loads(raw) if isinstance(raw, str) else raw
                self._click_point(int(pos["x"]), int(pos["y"]))
                time.sleep(0.25)
            self._set_textarea("")
        except Exception:
            pass

    def _attach_image(self, image_data: str):
        """将一张参考图附加到输入框。"""
        self._attach_images([image_data] if image_data else [])

    def _attach_images(self, images: list[str]):
        """一次把多张参考图放进输入框。Muse 的 file input 带 multiple，单次最多 10 张。"""
        prepared = []
        for index, image_data in enumerate(images or [], 1):
            if not image_data:
                continue
            b64, mime = self._normalize_image(image_data)
            if not b64:
                raise MuseGenerationError(f"第 {index} 张参考图读取失败，已停止生成")
            ext = ((mime or "image/png").split("/")[-1] or "png").split(";")[0]
            if ext == "jpeg":
                ext = "jpg"
            prepared.append({
                "b64": b64,
                "mime": mime or "image/png",
                "name": f"reference_image_{index}.{ext}",
            })
        if not prepared:
            return
        if len(prepared) > 10:
            raise MuseGenerationError("参考图最多 10 张")

        self._clear_attachments()

        _INJECT_JS = """
        (function(files) {
            try {
                var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                if (!inputs.length) return JSON.stringify({ok: false, err: 'no-file-input'});
                var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
                var composer = ov ? ov.closest('form') : null;
                var input = null;
                if (composer) {
                    input = inputs.find(function(x){ return composer.contains(x); }) || null;
                }
                if (!input) {
                    input = inputs.find(function(x){
                        return !x.closest('[class*=chat-user-bubble], [class*="group/msg"]');
                    }) || inputs[0];
                }
                if (!input.getAttribute('accept')) input.setAttribute('accept', 'image/*');
                var dt = new DataTransfer();
                files.forEach(function(f) {
                    var byteChars = atob(f.b64);
                    var byteNumbers = new Array(byteChars.length);
                    for (var i = 0; i < byteChars.length; i++) byteNumbers[i] = byteChars.charCodeAt(i);
                    var blob = new Blob([new Uint8Array(byteNumbers)], {type: f.mime});
                    dt.items.add(new File([blob], f.name, {type: f.mime}));
                });
                input.files = dt.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({ok: true, count: files.length});
            } catch(e) {
                return JSON.stringify({ok: false, err: String(e)});
            }
        })(%s)
        """
        try:
            raw_res = self.page.js(_INJECT_JS % json.dumps(prepared))
            res_obj = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
            if not res_obj.get("ok"):
                raise MuseGenerationError("附加参考图失败，已停止生成")
        except MuseGenerationError:
            raise
        except Exception as e:
            raise MuseGenerationError("附加参考图失败，已停止生成") from e

        expect = len(prepared)
        # 现网确认按钮是「移除附件」，每张图一个；缩略图用 blob 地址，避免把历史图片算进去。
        deadline = time.time() + max(15.0, 4.0 * expect)
        while time.time() < deadline:
            raw_count = self.page.js(
                """(function(){
                var removes = Array.from(document.querySelectorAll('button[aria-label]')).filter(function(b){
                    return /remove\\s*attachment|移除附件|删除附件/i.test(b.getAttribute('aria-label') || '');
                }).length;
                var root = document.querySelector('textarea');
                var blobs = 0;
                for (var i = 0; i < 6 && root; i++) {
                    blobs = root.querySelectorAll('img[src^="blob:"]').length;
                    if (blobs) break;
                    root = root.parentElement;
                }
                return JSON.stringify({removes: removes, blobs: blobs});
                })()"""
            )
            try:
                counts = json.loads(raw_count) if isinstance(raw_count, str) else (raw_count or {})
            except (TypeError, ValueError):
                counts = {}
            if counts.get("removes", 0) >= expect or counts.get("blobs", 0) >= expect:
                break
            time.sleep(0.3)
        else:
            raise MuseGenerationError(f"参考图上传未确认，期望 {expect} 张，已停止生成")
        time.sleep(0.5)
    # ---------------- 主流程 ----------------
    def generate(self, cookies: dict, prompt: str, expect: str = "image",
                 timeout: int = 240, expires: dict | None = None, account_id: str | None = None,
                 on_progress=None, reference_image: str | None = None,
                 reference_images: list | None = None,
                 stop_event=None) -> dict:
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=False)
        self._scroll_bottom()
        refs = [item for item in (reference_images or []) if item]
        if not refs and reference_image:
            refs = [reference_image]
        if refs:
            self._attach_images(refs)
        else:
            self._clear_attachments()
        atts_before = self.attachments()
        baseline_sources = {a.get(k) for a in atts_before for k in ("src", "vSrc", "iSrc") if a.get(k)}
        base = atts_before[-1] if atts_before else {}
        baseline_src = base.get("src") or ""
        base_agent_cnt = self._agent_count()
        sent = self._send(prompt, wait=send_wait_budget(len(refs)))
        if sent not in ("clicked", "enter-sent"):
            raise MuseGenerationError(send_failure_message(sent))
        att = self._wait_attachment(
            baseline_src, timeout, expect, on_progress=on_progress,
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
            return {"path": dst, "filename": name, "size": len(data), "ext": ext, "mime": mime,
                    "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image",
                    "via": "blob", "attachment": att.get("tid"),
                    "w": att.get("w"), "h": att.get("h")}

        path = self._download_fallback(selected_src)
        if not path:
            raise MuseGenerationError("已生成但未能取回文件")
        ext = os.path.splitext(path)[1].lower() or ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
        dst = os.path.join(self.cfg.media_dir, name)
        shutil.move(path, dst)
        return {"path": dst, "filename": name, "size": os.path.getsize(dst), "ext": ext, "mime": "",
                "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image",
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
