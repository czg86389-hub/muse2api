"""Real Chromium regression for send-button scoping. No Muse account or network.

Run: python tests/test_composer_send_browser.py [--root /path/to/candidate]
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
root = args.root.resolve()
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('composer_send_candidate', root / 'engine.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix='muse-send-test-', dir=Path.home()) as home:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    cfg = SimpleNamespace(
        chromium=shutil.which('chromium') or shutil.which('google-chrome'),
        cdp_port=port, home_dir=str(Path.home()), extra_path='',
        profile_dir=home + '/profile', data_dir=home, download_dir=home + '/downloads')
    Path(cfg.download_dir).mkdir()
    engine = module.MuseEngine(cfg)
    try:
        engine.start()
        engine.page = engine._open_page()
        page = engine.page

        def fixture(*, legacy=False, attachment=False, label='Send', text='',
                    test_id='', disabled=False, aria_disabled=False, missing=False):
            tag = 'form' if legacy else 'section'
            attrs = (' disabled' if disabled else '')
            attrs += ' aria-disabled="true"' if aria_disabled else ''
            send = '' if missing else (
                f'<button type="button" id="actual" class="bg-chat-user-bubble text-chat-user-text" aria-label="{label}" '
                f'data-testid="{test_id}"{attrs}>{text}</button>')
            remove = '<button type="button" aria-label="Remove attachment"></button>' if attachment else ''
            overlay = '<div data-testid="hatch-composer-placeholder-overlay"></div>' if legacy else ''
            markup = (
                '<aside><button id="history">Asked whether to send the receipt</button>'
                '<button id="outside" aria-label="Send">Send</button></aside>'
                '<main><div class="chat-user-bubble"><button>Send</button></div>'
                f'<{tag} id="composer"><div><textarea style="width:400px;height:70px"></textarea>'
                f'{overlay}</div>{remove}'
                '<button type="button" id="feedback">Send feedback</button>'
                '<button type="button" id="resend" aria-label="Resend">Resend</button>'
                '<button type="button" style="display:none" aria-label="Send">Send</button>'
                f'{send}</{tag}></main>')
            page.js('document.body.innerHTML=' + json.dumps(markup))
            page.js('''(() => {
                window.wrongClicks=0; window.accepted=[];
                document.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
                    if(b.id!=='actual'){window.wrongClicks++;return;}
                    const ta=document.querySelector('textarea');
                    window.accepted.push(ta.value);ta.value='';
                    document.querySelectorAll('#composer [aria-label="Remove attachment"]').forEach(e=>e.remove());
                }));
                document.querySelector('form')?.addEventListener('submit',e=>e.preventDefault());
            })()''')

        def assert_accepted(prompt):
            state = page.js('({wrong:window.wrongClicks,accepted:window.accepted,text:document.querySelector("textarea").value})')
            assert state['wrong'] == 0, f'clicked a history/feedback button: {state}'
            assert state['accepted'] == [prompt] and state['text'] == '', state

        for legacy in (False, True):
            for attachment in (False, True):
                for prompt in ('short prompt', 'long prompt ' + 'x' * 523):
                    fixture(legacy=legacy, attachment=attachment)
                    result = engine._send(prompt)
                    assert result == 'clicked', result
                    assert_accepted(prompt)
                    assert page.js(engine._ATTACHED_COUNT_JS) == 0
        print('PASS: short/long prompts with/without attachments, modern and legacy composers; no sidebar or feedback clicks')

        fixture(attachment=True)
        page.js("document.querySelector('textarea').value='retry prompt'")
        assert engine._click_send() == 'clicked'
        assert_accepted('retry prompt')
        print('PASS: retry uses the same scoped send control')

        for label, text, test_id in (
            ('发送', '', ''), ('傳送', '', ''), ('', 'Send', ''),
            ('', '', 'hatch-composer-send-button'), ('Send message', '', '')):
            fixture(label=label, text=text, test_id=test_id)
            page.js("document.querySelector('textarea').value='localized prompt'")
            assert engine._click_send() == 'clicked'
            assert_accepted('localized prompt')
        print('PASS: English/Chinese labels, exact text and composer test IDs')

        for options, expected in (({'disabled': True}, 'disabled'),
                                  ({'aria_disabled': True}, 'disabled'),
                                  ({'missing': True}, 'no-button')):
            fixture(**options)
            page.js("document.querySelector('textarea').value='pending prompt'")
            state = json.loads(page.js(engine._SEND_STATE_JS))
            assert state['btn'] == (0 if expected == 'no-button' else 2), state
            assert engine._click_send() == expected
            assert page.js('window.wrongClicks===0 && window.accepted.length===0')
        print('PASS: missing, disabled and aria-disabled send controls never fall back to an outside button')
    finally:
        engine.stop()
