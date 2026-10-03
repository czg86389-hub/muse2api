"""实机验证：注入一张小 PNG，观察 composer 里出现的附件缩略图 DOM；随后清理。

用法：python3 tools/probe_attach.py
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from cdp import CDP, http_json  # noqa: E402

CDP_PORT = 19210

# 1x1 红色 PNG
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
           "IQAAAABJRU5ErkJggg==")


def find_page_ws():
    pages = http_json(f"http://127.0.0.1:{CDP_PORT}/json")
    for p in pages:
        if p.get("type") == "page" and "muse.ai" in (p.get("url") or ""):
            return p.get("webSocketDebuggerUrl"), p.get("url")
    raise SystemExit("没找到 muse.ai 页面")


SNAPSHOT_JS = r"""
(function(){
  function info(el){
    return {
      tag: el.tagName.toLowerCase(),
      ariaLabel: el.getAttribute('aria-label'),
      dataTestid: el.getAttribute('data-testid'),
      text: (el.innerText||'').trim().slice(0,40),
      cls: (el.getAttribute('class')||'').slice(0,140)
    };
  }
  var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
  var composer = ov ? (ov.closest('form') || ov.parentElement.parentElement.parentElement) : null;
  var out = {composer: [], attachBtn: null, fileInput: null, imgs: []};
  if (composer) {
    out.composer = Array.from(composer.querySelectorAll('button,[role=button],img,svg,input'))
      .map(info);
    out.imgs = Array.from(composer.querySelectorAll('img')).map(function(m){
      return {src:(m.currentSrc||m.src||'').slice(0,80), cls:(m.getAttribute('class')||'').slice(0,140),
              path: (function(){var s=[],c=m;while(c&&c.nodeType===1&&s.length<5){s.unshift(c.tagName.toLowerCase()+(c.getAttribute('aria-label')?'[al='+c.getAttribute('aria-label')+']':''));c=c.parentElement;}return s.join(' > ');})()};
    });
  }
  var ab = Array.from(document.querySelectorAll('button')).find(function(b){return /附加文件|attach/i.test(b.getAttribute('aria-label')||'');});
  if (ab) out.attachBtn = info(ab);
  var fi = document.querySelector('input[type=file]');
  if (fi) out.fileInput = {accept:fi.accept, multiple:fi.multiple, hidden:fi.hidden, cls:fi.className};
  return JSON.stringify(out);
})()
"""

INJECT_JS = r"""
(function(b64){
  try{
    var bin=atob(b64); var arr=new Uint8Array(bin.length);
    for(var i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
    var blob=new Blob([arr],{type:'image/png'});
    var file=new File([blob],'probe.png',{type:'image/png'});
    var input=document.querySelector('input[type="file"]');
    if(!input) return JSON.stringify({ok:false,err:'no-input'});
    var dt=new DataTransfer(); dt.items.add(file);
    input.files=dt.files;
    input.dispatchEvent(new Event('change',{bubbles:true}));
    input.dispatchEvent(new Event('input',{bubbles:true}));
    return JSON.stringify({ok:true});
  }catch(e){ return JSON.stringify({ok:false,err:String(e)}); }
})(%s)
"""

CLEAR_JS = r"""
(function(){
  var n=0;
  Array.from(document.querySelectorAll('button')).forEach(function(b){
    var al=(b.getAttribute('aria-label')||'');
    if(/移除|删除|remove|删除附件/i.test(al)){ b.click(); n++; }
  });
  var inp=document.querySelector('input[type=file]'); if(inp) inp.value='';
  return n;
})()
"""


def main():
    ws, url = find_page_ws()
    print(f"[attach-probe] page = {url}\n")
    c = CDP(ws)
    try:
        print("--- BEFORE ---")
        print(json.dumps(json.loads(c.js(SNAPSHOT_JS)), ensure_ascii=False, indent=2))
        print("\n--- INJECT ---")
        print(c.js(INJECT_JS % json.dumps(PNG_B64)))
        for i in range(6):
            time.sleep(0.6)
            out = json.loads(c.js(SNAPSHOT_JS))
            imgs = [x for x in out.get("imgs", [])]
            print(f"[t={i*0.6+0.6:.1f}s] composer imgs={len(imgs)}")
            if imgs:
                print(json.dumps(imgs, ensure_ascii=False, indent=2))
                break
        print("\n--- AFTER snapshot composer controls ---")
        print(json.dumps(out.get("composer", []), ensure_ascii=False, indent=2)[:3000])
        print("\n--- CLEAR ---")
        print("cleared:", c.js(CLEAR_JS))
    finally:
        c.close()


if __name__ == "__main__":
    main()
