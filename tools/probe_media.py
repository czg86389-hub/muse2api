"""监控生成结果媒体节点：发送一条极简文本，轮询 DOM 里新出现的 img/video 及其容器特征。

用法：python3 tools/probe_media.py "一条极简提示词"
注意：会真的发起一次生成（消耗额度）。仅在需要确认媒体容器选择器时使用。
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
    while(cur && cur.nodeType===1 && seg.length<6){
      var s=cur.tagName.toLowerCase();
      var tid=cur.getAttribute('data-testid'); if(tid) s+='[tid="'+tid+'"]';
      var al=cur.getAttribute('aria-label'); if(al) s+='[al="'+al+'"]';
      var cl=(cur.getAttribute('class')||'').split(' ').slice(0,2).join('.');
      if(cl) s+='.'+cl;
      seg.unshift(s); cur=cur.parentElement;
    }
    return seg.join(' > ');
  }
  var out=[];
  Array.from(document.querySelectorAll('img,video')).forEach(function(m){
    var src=m.currentSrc||m.src||'';
    if(!src || src.startsWith('data:') || /avatar|emoji|icon/i.test(src)) return;
    var w=m.naturalWidth||m.videoWidth||0, h=m.naturalHeight||m.videoHeight||0;
    if(w<64||h<64) return;
    out.push({ tag:m.tagName.toLowerCase(), src:src.slice(0,90), w:w, h:h, path:path(m) });
  });
  // 也列出所有 data-testid（看有无新前缀）
  var tids={};
  Array.from(document.querySelectorAll('[data-testid]')).forEach(function(el){
    tids[el.getAttribute('data-testid')]=(tids[el.getAttribute('data-testid')]||0)+1;
  });
  return JSON.stringify({media:out, tids:tids});
})()
"""


def find_page_ws():
    pages = http_json(f"http://127.0.0.1:{CDP_PORT}/json")
    for p in pages:
        if p.get("type") == "page" and "muse.ai" in (p.get("url") or ""):
            return p.get("webSocketDebuggerUrl"), p.get("url")
    raise SystemExit("没找到 muse.ai 页面")


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "纯白背景中央一个红色圆点，极简"
    ws, url = find_page_ws()
    print(f"[media-probe] page = {url}")
    print(f"[media-probe] prompt = {prompt}\n")
    c = CDP(ws)
    try:
        before = json.loads(c.js(MEDIA_JS))
        base_srcs = {m["src"] for m in before["media"]}
        print(f"baseline media = {len(before['media'])}")
        for m in before["media"][:6]:
            print("  ", json.dumps(m, ensure_ascii=False))

        print("\n--- 发送 ---")
        c.js("(function(){var t=document.querySelector('textarea');if(t){t.focus();}return true;})()")
        c.send("Input.insertText", {"text": prompt})
        time.sleep(0.6)
        sent = c.js("""(function(){
            var b=[...document.querySelectorAll('button,[role=button]')].find(function(x){return /send|发送/i.test((x.getAttribute('aria-label')||''));});
            if(b && !b.disabled){ b.click(); return 'clicked'; }
            return 'no-button:'+(b?('disabled='+b.disabled):'not-found');
        })()""")
        print("send:", sent)

        for i in range(90):
            time.sleep(2)
            snap = json.loads(c.js(MEDIA_JS))
            cur = {m["src"] for m in snap["media"]}
            new = cur - base_srcs
            if i % 5 == 0 or new:
                print(f"[t={ (i+1)*2 }s] media={len(snap['media'])} new={len(new)}")
            if new:
                print("\n=== NEW MEDIA NODE ===")
                for m in snap["media"]:
                    if m["src"] in new:
                        print(json.dumps(m, ensure_ascii=False, indent=2))
                print("\n=== current testids ===")
                print(json.dumps(snap["tids"], ensure_ascii=False, indent=2))
                return
        print("\n(90 轮内没出新媒体)")
        print(json.dumps(json.loads(c.js(MEDIA_JS))["tids"], ensure_ascii=False, indent=2))
    finally:
        c.close()


if __name__ == "__main__":
    main()
