"""Muse video-continuation safety rules.

The source identification and prompt approach are adapted from Muse4AI
Copyright (c) 2026 Andrew Babo, MIT license; see LICENSES/muse4ai.txt.
"""
from __future__ import annotations
import json
import re

MAX_EXTEND_STEPS = 6
MAX_EXTEND_TARGET_SECONDS = 60
MAX_VIDEO_BYTES = 200 * 1024 * 1024
THREAD_RE = re.compile(r'^/thread/[0-9a-f][0-9a-f-]{7,}$', re.I)

def valid_thread(value):
    return isinstance(value, str) and bool(THREAD_RE.fullmatch(value))

def valid_locator(value):
    if not isinstance(value, str) or not value.startswith('sandbox://workspace/') or not value.lower().endswith('.mp4'):
        return False
    if re.search(r'[\\\\?#%\x00-\x1f\x7f]', value):
        return False
    return all(x not in ('', '.', '..') for x in value[len('sandbox://'):].split('/'))

def extend_prompt(locator, instruction=''):
    if not valid_locator(locator):
        raise ValueError('视频来源无效')
    instruction = str(instruction or '').strip()
    if len(instruction) > 1000:
        raise ValueError('续写要求不能超过 1000 字')
    text = ('Continue the exact video ' + json.dumps(locator, ensure_ascii=True) +
            ' in this conversation for about 10 more seconds. Extract its actual final frame using your workspace tools, '
            'use that frame to begin the continuation, and maintain the subject, scene, motion, camera and audio without a cut. '
            'Keep the original complete, concatenate the continuation in the workspace, and return one cumulative MP4.')
    return text + (' Next-scene direction: ' + instruction if instruction else '')

# A source must be attached to the latest assistant video message, with unambiguous
# React presentation metadata tied to that message. Missing DOM metadata fails closed.
VIDEO_SNAPSHOT_JS = r'''(() => {
 const nodes = [...document.querySelectorAll('[data-message-item="true"][data-message-id]')];
 const messages=[];
 for (const node of nodes) {
  if (node.getAttribute('data-message-role') !== 'assistant') continue;
  const mid=node.getAttribute('data-message-id'), videos=[];
  for(const w of node.querySelectorAll('[data-testid^="hatch-chat-attachment-presentation-"]')) {
   if (!/video/i.test(w.getAttribute('data-testid')||'')) continue;
   let f=w[Object.keys(w).find(k=>k.startsWith('__reactFiber'))], p=null;
   for(let i=0;f&&i<8;i++,f=f.return) if(f.memoizedProps?.presentation){p=f.memoizedProps.presentation;break;}
   const a=p?.data?.videos, file=Array.isArray(a)&&a.length===1?a[0]:null;
   const bound=!!(p?.presentationId && p.hostMessageId===mid && p.presentationMessageId===mid);
   const v=w.querySelector('video');
   videos.push({output:bound&&file?.missing===false&&file?.mime==='video/mp4'&&Number.isFinite(file?.byteLen)&&file.byteLen>0?file.path:'',
     size:file?.byteLen||0,src:v?(v.currentSrc||v.src||v.querySelector('source')?.src||''):'',message:mid});
  }
  if(videos.length) messages.push({id:mid,videos});
 }
 return {path:location.pathname, host:location.hostname,
   ready:document.readyState==='complete' && !!document.querySelector('textarea'),
   busy:!!document.querySelector('[data-testid="hatch-composer-stop-button"],button[aria-label*="Stop" i],button[aria-label*="停止"]'),messages};
})()'''
