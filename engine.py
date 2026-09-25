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
import time
import uuid

from cdp import CDP, http_json

log = logging.getLogger("muse2api")

ATT_SEL = '[data-testid^="hatch-chat-attachment-presentation-"]'

# 决定账号生死的核心 cookie（缺失或过期 = 会话失效）
ESSENTIAL_COOKIES = ("hatch_sess", "hatch_gw", "hatch_vml",
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
        self.page: CDP | None = None
        self._log = None
        os.makedirs(cfg.profile_dir, exist_ok=True)

    # ---------------- 浏览器生命周期 ----------------
    def _debug_url(self):
        return f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"

    def start(self):
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
        for c in (self.page, self.browser):
            if c:
                c.close()
        self.page = self.browser = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    # ---------------- 页面 ----------------
    def _open_page(self):
        import requests
        try:
            pages = requests.get(f"http://127.0.0.1:{self.cfg.cdp_port}/json/list", timeout=3).json()
            for p in pages:
                if p.get("type") == "page":
                    pid = p.get("id")
                    if pid:
                        requests.get(f"http://127.0.0.1:{self.cfg.cdp_port}/json/close/{pid}", timeout=2)
        except Exception:
            pass

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

    def _apply_cookies(self, page: CDP, cookies: dict, expires: dict | None = None):
        """注入 cookie。带上真实 expires，并在注入前彻底清空旧账号 cookie，保证账号隔离。"""
        try:
            page.send("Network.clearBrowserCookies")
        except Exception:
            pass
        for name, value in cookies.items():
            params = {"name": name, "value": value, "domain": ".muse.ai",
                      "path": "/", "secure": True}
            exp = (expires or {}).get(name)
            try:
                if exp and float(exp) > 0:
                    params["expires"] = float(exp)
            except (TypeError, ValueError):
                pass
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

    def reset_thread(self):
        """关闭残留弹窗并导航到全新空白会话，防止历史任务/弹窗阻塞后续请求。"""
        if not self.page:
            return
        try:
            self.page.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "windowsVirtualKeyCode": 27, "key": "Escape", "code": "Escape"})
            self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 27, "key": "Escape", "code": "Escape"})
            self.page.js("""(function(){
                var d = document.querySelector('[role="dialog"]');
                if (d) {
                    var b = d.querySelector('button[aria-label*="close" i], button');
                    if (b) b.click();
                }
                if (window.location.pathname !== '/thread/new') {
                    window.location.href = 'https://muse.ai/thread/new';
                }
            })()""")
            time.sleep(0.5)
        except Exception:
            pass

    def ensure_page(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        if self.page is not None and (account_id is None or getattr(self, "current_acc_id", None) == account_id):
            try:
                if self.page.js("!!document.querySelector('textarea')"):
                    return self.page
            except Exception:
                pass
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
            self.page = None
        page = self._open_page()
        self._apply_cookies(page, cookies, expires)
        page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
        for _ in range(self.cfg.login_wait):
            time.sleep(0.3)
            try:
                if page.js("!!document.querySelector('textarea')"):
                    self.page = page
                    self.current_acc_id = account_id
                    return page
            except Exception:
                pass
        try:
            if page.js("!!document.querySelector('textarea')"):
                self.page = page
                self.current_acc_id = account_id
                return page
        except Exception:  # noqa: BLE001
            pass
        # 区分「会话失效（被踢回登录页）」和「页面加载卡住」，错误提示才能对症
        try:
            body = (page.js("document.body.innerText.slice(0,1200)") or "").lower()
        except Exception:  # noqa: BLE001
            body = ""
        page.close()
        if re.search(r"log in|sign in|create an account|登录|use another account", body):
            raise MuseAuthError("会话已被 muse.ai 登出（可能被其它登录挤掉或触发风控），"
                                "请用浏览器扩展重新导入 cookie")
        raise MuseAuthError("muse.ai 页面加载超时（未出现聊天输入框），请稍后重试")

    def refresh(self, cookies: dict, expires: dict | None = None):
        if self.page:
            self.page.close()
            self.page = None
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

    def quota(self, cookies: dict, expires: dict | None = None) -> dict:
        """打开 Settings 面板读额度。返回结构化 dict；读不到时 raise。"""
        self.ensure_page(cookies, expires)
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
        "var atts=[...document.querySelectorAll('" + ATT_SEL + "')];"
        "return JSON.stringify(atts.map(function(a){"
        "var v=a.querySelector('video');"
        "var img=a.querySelector('img');"
        "var isVid=(a.getAttribute('data-testid')||'').includes('video')||!!v;"
        "var primary=isVid?(v||img):(img||v);"
        "return {"
        "tid:a.getAttribute('data-testid')||'',"
        "hasVideo:!!v,"
        "hasImg:!!img,"
        "src:primary?(primary.currentSrc||primary.src||''):'',"
        "vSrc:v?(v.currentSrc||v.src||''):'',"
        "iSrc:img?(img.currentSrc||img.src||''):'',"
        "w:primary?(primary.videoWidth||primary.naturalWidth||0):0,"
        "h:primary?(primary.videoHeight||primary.naturalHeight||0):0"
        "};}));})()"
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
        time.sleep(0.15)

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
        time.sleep(0.15)
        self.page.send("Input.insertText", {"text": prompt})

        # 触发 React 18 原型 setter 以及 input/change 事件以同步发送按钮状态
        _SETTER_JS = (
            "(function(t){var ta=document.querySelector('textarea');"
            "if(!ta) return false;"
            "var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(ta,t);"
            "ta.dispatchEvent(new Event('input',{bubbles:true}));"
            "ta.dispatchEvent(new Event('change',{bubbles:true}));"
            "return ta.value;})(%s)")
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
        time.sleep(0.5)
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
                         on_progress=None) -> dict | None:
        deadline = time.time() + timeout
        t_start = time.time()
        stable_src, stable_n = "", 0
        while time.time() < deadline:
            time.sleep(0.8)
            self._scroll_bottom()
            att = self._last_attachment()
            if att:
                src = att.get("src") or ""
                v_src = att.get("vSrc") or ""
                tid = att.get("tid") or ""
                w = att.get("w", 0) or 0
                h = att.get("h", 0) or 0
                has_video = att.get("hasVideo", False)
                if expect == "video":
                    want = has_video or ("video" in tid) or ("video" in src) or ("video" in v_src) or src.endswith((".mp4", ".webm", ".mov"))
                else:
                    want = ("image" in tid) or (not has_video)
                check_src = v_src if (expect == "video" and v_src) else src
                if check_src and check_src != baseline_src and want:
                    if w > 0 and h > 0:
                        return att
                    if check_src == stable_src:
                        stable_n += 1
                    else:
                        stable_src, stable_n = check_src, 0
                    if stable_n >= 1:
                        return att
            if on_progress:
                elapsed = time.time() - t_start
                prog = min(92, int(25 + elapsed * 1.0))
                try:
                    on_progress(prog)
                except Exception:
                    pass
            try:
                tail = self.page.js("document.body.innerText.slice(-700)") or ""
            except Exception:
                tail = ""
            if re.search(r"额度不足|积分不足|out of credits|达到上限|token limit", tail):
                raise MuseGenerationError("账号额度不足")
        return None

    # ---------------- 取字节 ----------------
    _EXTRACT_JS = r"""
    (async function(src, expect){
      try{
        var u = src;
        var a = [...document.querySelectorAll('%s')].pop();
        if(expect === 'video'){
          var v = a ? a.querySelector('video') : document.querySelector('video');
          if (v && (v.currentSrc || v.src)) {
            u = v.currentSrc || v.src;
          }
        } else {
          var img = a ? a.querySelector('img') : null;
          if (img && (img.currentSrc || img.src)) {
            u = img.currentSrc || img.src;
          }
        }
        if(!u && a){
          var m = (expect === 'video') ? (a.querySelector('video') || a.querySelector('img')) : (a.querySelector('img') || a.querySelector('video'));
          if(m) u = m.currentSrc || m.src || '';
        }
        if(!u) return JSON.stringify({ok:false,err:'no-media-src'});
        var r = await fetch(u);
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
    """ % (ATT_SEL, "%s", "%s")

    # ---------------- 文本 / 代码对话 ----------------
    # 实测（2026-09-23）：muse.ai 网页助手（自称 Koda，底层是 Muse 系列语言模型）
    # 支持完整的文本/代码对话，且回复本身就是流式吐字的。
    #
    # DOM 判据（两类气泡都带 hatch-chat-groupable-bubble）：
    #   - 用户消息气泡：class 含 chat-user-bubble，innerText 以 "You:" 开头
    #   - 助手回复气泡：class 含 hatch-agent-bubble-bg
    # 气泡按时间顺序排列 → 「最后一个助手气泡」就是最新回复。
    #
    # 注意：聊天是虚拟列表，必须先 _scroll_bottom()，否则新消息根本不在 DOM 里。
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

    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None):
        """发一条消息，流式 yield 增量文本。"""
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 300))
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread()
        self._scroll_bottom()
        base_text = self._agent_text()
        base_agent = self._agent_count()
        self._send(prompt)

        deadline = time.time() + timeout

        # 等新回复出现：只要助手最新文本变化，或者助手气泡增加且不为空，代表开始吐字
        while time.time() < deadline:
            time.sleep(0.1)
            self._scroll_bottom()
            cur = self._agent_text()
            if cur and cur != base_text:
                break
            if self._agent_count() > base_agent and cur != "":
                break

        # 流式输出增量文本
        sent, last, stable = "", None, 0
        while time.time() < deadline:
            time.sleep(0.15)
            self._scroll_bottom()
            cur = self._agent_text()
            if not cur or cur == base_text:
                continue
            if cur != last:
                delta = cur[len(sent):] if cur.startswith(sent) else cur
                if delta:
                    sent = cur
                    yield delta
                last, stable = cur, 0
            else:
                stable += 1
                if stable >= 8:
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
    def _download_fallback(self, timeout: int = 180) -> str | None:
        before = set(os.listdir(self.cfg.download_dir))
        clicked = self.page.js(
            "(function(){var a=[...document.querySelectorAll('" + ATT_SEL + "')].pop();"
            "var node=a;"
            "for(var i=0;i<12&&node;i++){"
            "var b=[...node.querySelectorAll('button,[role=button]')]"
            ".filter(x=>/下载|保存|download/i.test(x.getAttribute('aria-label')||''));"
            "if(b.length){b[b.length-1].click();return 'ok';}node=node.parentElement;}"
            "var all=[...document.querySelectorAll('button,[role=button]')]"
            ".filter(x=>/下载|保存|download/i.test(x.getAttribute('aria-label')||''));"
            "if(all.length){all[all.length-1].click();return 'global';}"
            "return 'none';})()")
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
        """清除聊天输入框里遗留的附件缩略图。"""
        try:
            self.page.js(
                "(function(){"
                "var btns=Array.from(document.querySelectorAll('button')).filter(function(b){"
                "return /remove attachment|移除|删除/i.test(b.getAttribute('aria-label')||b.innerText||'');"
                "});"
                "btns.forEach(function(b){b.click();});"
                "var inp=document.querySelector('input[type=\"file\"]');"
                "if(inp) inp.value='';"
                "return btns.length;"
                "})()")
            time.sleep(0.3)
        except Exception:
            pass

    def _attach_image(self, image_data: str):
        """将参考图通过 DataTransfer 附加到输入框，杜绝使用旧历史图片。"""
        if not image_data:
            return
        b64, mime = self._normalize_image(image_data)
        if not b64:
            return

        self._clear_attachments()

        _INJECT_JS = """
        (function(b64, mime) {
            try {
                var byteChars = atob(b64);
                var byteNumbers = new Array(byteChars.length);
                for (var i = 0; i < byteChars.length; i++) {
                    byteNumbers[i] = byteChars.charCodeAt(i);
                }
                var byteArray = new Uint8Array(byteNumbers);
                var blob = new Blob([byteArray], {type: mime});
                var ext = mime.split('/')[1] || 'png';
                if (ext === 'jpeg') ext = 'jpg';
                var file = new File([blob], 'reference_image.' + ext, {type: mime});
                var input = document.querySelector('input[type="file"]');
                if (!input) return JSON.stringify({ok: false, err: 'no-file-input'});
                var dt = new DataTransfer();
                dt.items.add(file);
                input.files = dt.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({ok: true});
            } catch(e) {
                return JSON.stringify({ok: false, err: String(e)});
            }
        })(%s, %s)
        """
        try:
            raw_res = self.page.js(_INJECT_JS % (json.dumps(b64), json.dumps(mime)))
            res_obj = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
            if not res_obj.get("ok"):
                log.warning("附加参考图失败: %s", res_obj.get("err"))
        except Exception as e:
            log.warning("附加参考图解析失败: %s", e)

        # 等待缩略图和 Remove attachment 按钮出现，最长等待 4 秒
        deadline = time.time() + 4.0
        while time.time() < deadline:
            has_attached = self.page.js(
                """(function(){
                var hasBtn = document.querySelector('button[aria-label*="Remove attachment" i]');
                var hasImg = document.querySelector('form img, [class*="object-cover"], img[alt*="reference"]');
                return Boolean(hasBtn || hasImg);
                })()"""
            )
            if has_attached:
                break
            time.sleep(0.3)
        time.sleep(0.5)
    # ---------------- 主流程 ----------------
    def generate(self, cookies: dict, prompt: str, expect: str = "image",
                 timeout: int = 240, expires: dict | None = None, account_id: str | None = None,
                 on_progress=None, reference_image: str | None = None) -> dict:
        self.ensure_page(cookies, expires, account_id=account_id)
        self._scroll_bottom()
        base = self._last_attachment() or {}
        baseline_src = base.get("src") or ""
        if reference_image:
            self._attach_image(reference_image)
        else:
            self._clear_attachments()
        self._send(prompt)
        att = self._wait_attachment(baseline_src, timeout, expect, on_progress=on_progress)
        if not att:
            self._debug_dump("no-attachment")
            raise MuseGenerationError("等待生成超时，未出现新的生成结果")

        os.makedirs(self.cfg.media_dir, exist_ok=True)
        data = mime = url = None
        try:
            data, mime, url = self.extract_bytes(att.get("src") or "", expect=expect)
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

        path = self._download_fallback()
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
