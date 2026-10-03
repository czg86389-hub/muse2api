"""深挖 composer 结构：找「加号/附件」按钮、附件缩略图容器、以及发送后生成的媒体节点。

用法：python3 tools/probe_dom2.py
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
    raise SystemExit("没找到 muse.ai 页面")


PROBE_JS = r"""
(function(){
  function txt(el){ return (el.innerText || el.textContent || '').trim().slice(0,80); }
  function cls(el){ return (el.getAttribute('class') || '').slice(0,200); }
  function path(el){
    var seg=[]; var cur=el;
    while(cur && cur.nodeType===1 && seg.length<6){
      var s=cur.tagName.toLowerCase();
      var tid=cur.getAttribute('data-testid'); if(tid) s+='[tid="'+tid+'"]';
      var al=cur.getAttribute('aria-label'); if(al) s+='[al="'+al+'"]';
      seg.unshift(s); cur=cur.parentElement;
    }
    return seg.join(' > ');
  }
  function info(el){
    return { tag: el.tagName.toLowerCase(),
      ariaLabel: el.getAttribute('aria-label'),
      dataTestid: el.getAttribute('data-testid'),
      title: el.getAttribute('title'),
      text: txt(el), cls: cls(el), path: path(el) };
  }

  var out = {};

  // A) composer 区（placeholder overlay 的祖辈）里的全部元素
  var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
  var composer = ov ? (ov.closest('form') || ov.parentElement.parentElement.parentElement) : null;
  out.composerFound = !!composer;
  if (composer) {
    out.composerHtmlSnippet = composer.outerHTML.slice(0, 4000);
    out.composerControls = Array.from(composer.querySelectorAll('button,[role=button],label,input,svg'))
      .map(info).slice(0, 60);
  }

  // B) file input 的祖先链（看它在哪个容器里）
  var fi = document.querySelector('input[type=file]');
  out.fileInput = fi ? info(fi) : null;
  if (fi) {
    var chain = []; var cur = fi.parentElement;
    while (cur && chain.length < 8) { chain.push(info(cur)); cur = cur.parentElement; }
    out.fileInputAncestors = chain;
    out.fileInputSiblings = Array.from(fi.parentElement.children).map(info);
  }

  // C) 所有 svg 图标（可能含 attach/plus 图标）的 title / 父按钮 aria
  out.svgIcons = Array.from(document.querySelectorAll('button svg, [role=button] svg')).map(function(s){
    var b = s.closest('button,[role=button]');
    return { svgClass: cls(s), parent: b ? info(b) : null };
  }).slice(0, 40);

  // D) 图片/视频节点（生成结果）——看包在什么容器里
  out.mediaNodes = Array.from(document.querySelectorAll('img,video')).map(function(m){
    var src = m.currentSrc || m.src || '';
    if (!src || src.startsWith('data:') || src.includes('avatar')) return null;
    var anc = []; var cur = m.parentElement;
    while (cur && anc.length < 5) { anc.push({tag:cur.tagName.toLowerCase(), tid:cur.getAttribute('data-testid'), cls:cls(cur)}); cur=cur.parentElement; }
    return { tag:m.tagName.toLowerCase(), src: src.slice(0,120), w:m.naturalWidth||m.videoWidth||0, h:m.naturalHeight||m.videoHeight||0, ancestors: anc };
  }).filter(Boolean).slice(0, 20);

  return JSON.stringify(out, null, 2);
})()
"""


def main():
    ws, url = find_page_ws()
    print(f"[probe2] page = {url}\n")
    c = CDP(ws)
    try:
        raw = c.js(PROBE_JS)
        data = json.loads(raw) if isinstance(raw, str) else raw
        print("=== composer found:", data.get("composerFound"))
        print("\n=== composer HTML snippet ===")
        print(data.get("composerHtmlSnippet", "")[:3500])
        print("\n=== composer controls ===")
        for x in data.get("composerControls", []):
            print(json.dumps(x, ensure_ascii=False))
        print("\n=== file input ===")
        print(json.dumps(data.get("fileInput"), ensure_ascii=False))
        print("\n=== file input ancestors ===")
        for x in data.get("fileInputAncestors", []):
            print(json.dumps(x, ensure_ascii=False))
        print("\n=== file input siblings ===")
        for x in data.get("fileInputSiblings", []):
            print(json.dumps(x, ensure_ascii=False))
        print("\n=== svg icons ===")
        for x in data.get("svgIcons", []):
            print(json.dumps(x, ensure_ascii=False))
        print("\n=== media nodes ===")
        for x in data.get("mediaNodes", []):
            print(json.dumps(x, ensure_ascii=False))
    finally:
        c.close()


if __name__ == "__main__":
    main()
