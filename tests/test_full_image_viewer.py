"""Offline regression for #62 (isolated Chromium, no account or API call): muse.ai shows a ~498px thumbnail in the
chat and loads the full-size picture only when its image viewer opens. Image tasks must return the full-size picture,
fall back to the thumbnail when no viewer opens, and leave large in-chat images and videos alone."""
import importlib.util, json, os, shutil, socket, struct, sys, tempfile, time, zlib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
source = next((a for a in sys.argv[1:] if not a.startswith('--')), str(ROOT / 'engine.py'))
spec = importlib.util.spec_from_file_location('candidate', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
checks = 0


def ok(cond, label):
    global checks
    assert cond, label
    checks += 1
    print('PASS', label)


def png(w, h, rgb):
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    import base64
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


THUMB, FULL, BIG_CHAT = png(498, 664, (200, 30, 30)), png(1344, 1792, (30, 200, 30)), png(1344, 1792, (30, 30, 200))


def page_html(thumb, viewer):
    """A muse-like chat result: the thumbnail sits in a button; clicking it opens a viewer (or does nothing)."""
    return '''document.body.innerHTML=`<div data-testid="hatch-chat-attachment-presentation-image">
      <button aria-label="Open generated image apple" id="open"><img class="outline-media-protection-border" src="%s"></button></div>`;
      window.clicks=0;window.viewerClosed=0;
      document.getElementById('open').addEventListener('click',function(){window.clicks++;
        var v=%s; if(!v) return;
        setTimeout(function(){document.body.insertAdjacentHTML('beforeend','<div role="dialog" id="viewer"><img src="'+v+'"></div>');},700);});
      document.addEventListener('keydown',function(e){if(e.key==='Escape'){var d=document.getElementById('viewer');if(d){d.remove();window.viewerClosed++;}}});
    ''' % (thumb, json.dumps(viewer))


with tempfile.TemporaryDirectory(prefix='muse-viewer-test-', dir=Path.home(), ignore_cleanup_errors=True) as tmp:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    chromium = os.environ.get('MUSE2API_CHROMIUM') or next(
        (shutil.which(n) for n in ('chromium', 'chromium-browser', 'google-chrome') if shutil.which(n)), None)
    if not chromium:
        raise SystemExit('Chromium required; set MUSE2API_CHROMIUM to its executable.')
    cfg = SimpleNamespace(chromium=chromium, cdp_port=port, home_dir=str(Path.home()), extra_path='',
                          profile_dir=tmp + '/profile', data_dir=tmp, download_dir=tmp + '/downloads',
                          media_dir=tmp + '/media')
    Path(cfg.download_dir).mkdir()
    engine = module.MuseEngine(cfg)
    try:
        engine.start()
        page = engine._open_page()
        engine.page = page
        engine._scroll_bottom = lambda: None

        # 1. thumbnail + viewer with the full picture
        page.js(page_html(THUMB, FULL))
        time.sleep(0.3)
        att = engine.attachments()[-1]
        ok(att['src'] == THUMB and (att['w'], att['h']) == (498, 664), 'the chat attachment is the 498x664 thumbnail')
        t0 = time.time()
        full = engine._full_image_src(att, timeout=8)
        ok(full and full['src'] == FULL and (full['w'], full['h']) == (1344, 1792) and full['dialog'],
           'the viewer is opened and its full-size picture is chosen (%.1fs)' % (time.time() - t0))
        data, mime, _ = engine.extract_bytes(full['src'])
        ok(data.startswith(b'\x89PNG') and struct.unpack('>II', data[16:24]) == (1344, 1792), 'the full-size bytes are read')
        engine._close_viewer()
        ok(page.js('window.viewerClosed') == 1 and page.js("!!document.getElementById('viewer')") is False, 'the viewer is closed again')

        # 2. button does nothing -> keep the thumbnail, no long hang
        page.js(page_html(THUMB, None))
        time.sleep(0.3)
        att = engine.attachments()[-1]
        t0 = time.time()
        ok(engine._full_image_src(att, timeout=2) is None and page.js('window.clicks') == 1 and time.time() - t0 < 4,
           'no viewer opens: the thumbnail is kept after the short wait')

        # 3. viewer shows only the same thumbnail -> nothing larger
        page.js(page_html(THUMB, THUMB))
        time.sleep(0.3)
        ok(engine._full_image_src(engine.attachments()[-1], timeout=2) is None, 'a viewer with the same thumbnail is not a full-size picture')

        # 4. an already large picture in the chat is used as it is (no click)
        page.js(page_html(BIG_CHAT, FULL))
        time.sleep(0.3)
        att = engine.attachments()[-1]
        ok(att['w'] == 1344 and engine._full_image_src(att) is None and page.js('window.clicks') == 0,
           'a full-size picture in the chat is not clicked')

        # 5. a thumbnail outside a result card (e.g. an uploaded reference preview) is never clicked
        page.js('''document.body.innerHTML='<div class="hatch-agent-bubble-bg"><button id="b"><img src="%s"></button></div>';
                   window.clicks=0;document.getElementById('b').onclick=function(){window.clicks++;};''' % THUMB)
        time.sleep(0.3)
        ok(engine._full_image_src({'src': THUMB, 'w': 498, 'h': 664}) is None and page.js('window.clicks') == 0,
           'only the result card\'s own button is clicked')

        # 6. whole image task: _generate_attached returns the full picture and its real size
        page.js('document.body.innerHTML="";')
        engine._agent_count = lambda: 0

        def fake_send(prompt):
            page.js(page_html(THUMB, FULL))
            return 'clicked'
        engine._send = fake_send
        res = engine._generate_attached('apple', 'image', time.monotonic() + 30, None, None, set())
        saved = open(res['path'], 'rb').read()
        ok((res['w'], res['h']) == (1344, 1792) and struct.unpack('>II', saved[16:24]) == (1344, 1792),
           'an image task saves the full-size picture (%sx%s)' % (res['w'], res['h']))

        # 7. no viewer -> the task still succeeds with the thumbnail
        def fake_send_no_viewer(prompt):
            page.js(page_html(THUMB, None))
            return 'clicked'
        engine._send = fake_send_no_viewer
        page.js('document.body.innerHTML="";')
        engine._full_image_src = lambda a, timeout=2.0: module.MuseEngine._full_image_src(engine, a, timeout)
        res = engine._generate_attached('apple', 'image', time.monotonic() + 30, None, None, set())
        ok((res['w'], res['h']) == (498, 664) and os.path.getsize(res['path']) > 0, 'without a viewer the thumbnail is still delivered')
    finally:
        try:
            engine.stop()
        except Exception:
            pass
print('FULL_IMAGE_VIEWER %d passed' % checks)
