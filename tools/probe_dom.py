"""对现网 muse.ai 页面做 DOM 探测：找上传入口、附件缩略图、媒体容器。

用法：
    python3 tools/probe_dom.py
会连到 CDP（默认 127.0.0.1:19210）里的 muse.ai 标签页，打印：
  1) input[type=file] 全量（数量 / accept / hidden / 外层容器）
  2) 上传/附件相关按钮/标签的真值（aria-label / data-testid / 文本 / class）
  3) data-testid 前缀分布（确认附件容器命名是否还叫 hatch-chat-attachment-presentation-）
  4) 页面里所有含 "attachment" / "attach" / "upload" / "file" 的 data-testid 值
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from cdp import CDP, http_json  # noqa: E402

CDP_PORT = 19210


def find_page_ws():
    pages = http_json(f"http://127.0.0.1:{CDP_PORT}/json")
    for p in pages:
        if p.get("type") == "page" and "muse.ai" in (p.get("url") or ""):
            return p.get("webSocketDebuggerUrl"), p.get("url")
    raise SystemExit("没找到 muse.ai 页面，检查 CDP 端口 / 页面是否开着")


PROBE_JS = r"""
(function(){
  function txt(el){ return (el.innerText || el.textContent || '').trim().slice(0,60); }
  function cls(el){ return (el.getAttribute('class') || '').slice(0,160); }
  function path(el){
    var seg=[]; var cur=el;
    while(cur && cur.nodeType===1 && seg.length<4){
      var s=cur.tagName.toLowerCase();
      var tid=cur.getAttribute('data-testid'); if(tid) s+='[data-testid="'+tid+'"]';
      var al=cur.getAttribute('aria-label'); if(al) s+='[aria-label="'+al+'"]';
      seg.unshift(s); cur=cur.parentElement;
    }
    return seg.join(' > ');
  }

  // 1) file inputs
  var inputs = Array.from(document.querySelectorAll('input[type="file"]')).map(function(el){
    var cs = getComputedStyle(el);
    return {
      accept: el.getAttribute('accept'),
      multiple: el.multiple,
      hidden: el.hidden,
      display: cs.display,
      visibility: cs.visibility,
      opacity: cs.opacity,
      hasStyleAttr: el.hasAttribute('style'),
      path: path(el),
      parentCls: el.parentElement ? cls(el.parentElement) : null
    };
  });

  // 2) 候选上传/附件按钮
  var btns = Array.from(document.querySelectorAll('button,[role=button],label,[aria-label],[title]'))
    .filter(function(el){
      var s = ((el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('data-testid')||'') + ' ' +
               (el.getAttribute('title')||'') + ' ' + txt(el)).toLowerCase();
      return /attach|upload|file|image|photo|media|附件|上传|图片|视频|remove|plus|add/.test(s);
    })
    .map(function(el){
      return {
        tag: el.tagName.toLowerCase(),
        ariaLabel: el.getAttribute('aria-label'),
        dataTestid: el.getAttribute('data-testid'),
        title: el.getAttribute('title'),
        text: txt(el),
        cls: cls(el),
        path: path(el)
      };
    });

  // 3) data-testid 前缀分布
  var tidMap = {};
  Array.from(document.querySelectorAll('[data-testid]')).forEach(function(el){
    var t = el.getAttribute('data-testid') || '';
    var pfx = t.split('-').slice(0,4).join('-');
    tidMap[pfx] = (tidMap[pfx]||0)+1;
  });

  // 4) 所有含 attachment/attach/upload/file 的 testid 值
  var attachTids = {};
  Array.from(document.querySelectorAll('[data-testid]')).forEach(function(el){
    var t = el.getAttribute('data-testid') || '';
    if(/attach|upload|file/i.test(t)) attachTids[t] = (attachTids[t]||0)+1;
  });

  // 5) 输入框附近（form 区）的按钮
  var ta = document.querySelector('textarea');
  var nearForm = [];
  if(ta){
    var form = ta.closest('form') || ta.closest('[class*=chat-input]') || ta.parentElement;
    if(form){
      nearForm = Array.from(form.querySelectorAll('button,[role=button],label,input')).map(function(el){
        return {
          tag: el.tagName.toLowerCase(),
          type: el.getAttribute('type'),
          ariaLabel: el.getAttribute('aria-label'),
          dataTestid: el.getAttribute('data-testid'),
          text: txt(el),
          cls: cls(el)
        };
      });
    }
  }

  return JSON.stringify({
    url: location.href,
    fileInputs: inputs,
    candidateBtns: btns,
    tidPrefixes: tidMap,
    attachTids: attachTids,
    nearForm: nearForm
  }, null, 2);
})()
"""


def main():
    ws, url = find_page_ws()
    print(f"[probe] page = {url}")
    print(f"[probe] ws   = {ws}\n")
    c = CDP(ws)
    try:
        raw = c.js(PROBE_JS)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            print("原始返回：", raw)
            return
        print("=== file inputs ===")
        for i in data.get("fileInputs", []):
            print(json.dumps(i, ensure_ascii=False, indent=2))
        print("\n=== candidate attach/upload buttons ===")
        for b in data.get("candidateBtns", []):
            print(json.dumps(b, ensure_ascii=False))
        print("\n=== data-testid prefixes ===")
        print(json.dumps(data.get("tidPrefixes", {}), ensure_ascii=False, indent=2))
        print("\n=== testids containing attach/upload/file ===")
        print(json.dumps(data.get("attachTids", {}), ensure_ascii=False, indent=2))
        print("\n=== near input form ===")
        for b in data.get("nearForm", []):
            print(json.dumps(b, ensure_ascii=False))
    finally:
        c.close()


if __name__ == "__main__":
    main()
