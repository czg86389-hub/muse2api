"""等待并抓取生成结果媒体节点（排除 avatar / 状态头像）。

用法：python3 tools/probe_media2.py [--wait-only]
--wait-only：不发送，只是等当前正在跑的那次生成结束并抓结果。
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from cdp import CDP, http_json  # noqa: E402

CDP_PORT = 19210

MEDIA_JS = r"""
(function(){
  function path(el){
    var seg=[]; var cur=el;
    while(cur && cur.nodeType===1 && seg.length<7){
      var s=cur.tagName.toLowerCase();
      var tid=cur.getAttribute('data-testid'); if(tid) s+='[tid="'+tid+'"]';
      var al=cur.getAttribute('aria-label'); if(al) s+='[al="'+al+'"]';
      seg.unshift(s); cur=cur.parentElement;
    }
    return seg.join(' > ');
  }
  var out=[];
  Array.from(document.querySelectorAll('img,video')).forEach(function(m){
    var src=m.currentSrc||m.src||'';
    if(!src || src.startsWith('data:')) return;
    // 排除状态头像 / 虚拟形象：其祖先含 status-avatar 或 aria-label 含 虚拟形象/Muse
    var anc = m.closest('[class*=status-avatar], [aria-label*="虚拟形象"]');
    if (anc) return;
    var w=m.naturalWidth||m.videoWidth||0, h=m.naturalHeight||m.videoHeight||0;
    out.push({ tag:m.tagName.toLowerCase(), src:src.slice(0,90), w:w, h:h, path:path(m),
               cls:(m.getAttribute('class')||'').slice(0,120) });
  });
  var hasStop = !!document.querySelector('[data-testid="hatch-composer-stop-button"]');
  var typing = !!document.querySelector('[data-testid="hatch-chat-typing-indicator"]');
  var bodyTxt = (document.body.innerText||'').slice(-600);
  return JSON.stringify({media:out, hasStop:hasStop, typing:typing, tail:bodyTxt});
})()
"""


def find_page_ws():
    pages = http_json(f"http://127.0.0.1:{CDP_PORT}/json")
    for p in pages:
        if p.get("type") == "page" and "muse.ai" in (p.get("url") or ""):
            return p.get("webSocketDebuggerUrl"), p.get("url")
    raise SystemExit("没找到 muse.ai 页面")


def main():
    ws, url = find_page_ws()
    print(f"[media2] page = {url}\n")
    c = CDP(ws)
    try:
        for i in range(100):
            snap = json.loads(c.js(MEDIA_JS))
            print(f"[t={i*3}s] media={len(snap['media'])} stop={snap['hasStop']} typing={snap['typing']}")
            for m in snap["media"]:
                print("   ", json.dumps(m, ensure_ascii=False))
            if snap["media"] and not snap["hasStop"]:
                print("\n=== RESULT FOUND ===")
                for m in snap["media"]:
                    if m["w"] >= 200 or m["h"] >= 200:
                        print(json.dumps(m, ensure_ascii=False, indent=2))
                print("\ntail text:", snap["tail"][-300:])
                return
            time.sleep(3)
        print("done / timeout")
    finally:
        c.close()


if __name__ == "__main__":
    main()
