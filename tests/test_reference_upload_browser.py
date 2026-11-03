"""Actual Chromium regression: composer scoping and CDP file uploads. No Muse login."""
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument("--probe-only", action="store_true")
args = parser.parse_args()
root = args.root.resolve()
sys.path.insert(0, str(root))
from cdp import CDP

with tempfile.TemporaryDirectory(prefix="muse-upload-check-", dir=Path.home()) as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home)
    spec = importlib.util.spec_from_file_location("reference_engine_fixture", root / "engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Engine = module.MuseEngine
    if hasattr(Engine, "_ATTACHED_COUNT_JS"):
        count_script = Engine._ATTACHED_COUNT_JS
    else:
        tree = ast.parse((root / "engine.py").read_text())
        count_script = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                            and isinstance(n.value, str) and "if (!composer) return 0" in n.value)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = open(Path(home) / "chromium.log", "wb")
    proc = subprocess.Popen([shutil.which("chromium") or shutil.which("google-chrome"),
                             "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                             "--remote-allow-origins=*", "--remote-debugging-port=" + str(port),
                             "--user-data-dir=" + str(Path(home) / "browser"), "about:blank"],
                            stdout=log, stderr=log, start_new_session=True)
    session = requests.Session()
    session.trust_env = False
    page = None
    try:
        for _ in range(150):
            try:
                tabs = session.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()
                tab = next(x for x in tabs if x.get("type") == "page")
                page = CDP(tab["webSocketDebuggerUrl"], timeout=15)
                break
            except (requests.RequestException, StopIteration):
                time.sleep(.1)
        assert page, "fixture Chromium did not start"
        page.send("Page.enable")
        page.send("Runtime.enable")
        def fixture(legacy=False, history_only=False):
            history = '<div class="chat-user-bubble"><button aria-label="Remove attachment"></button><input type="file"><img src="old.png"></div>'
            buttons = "" if history_only else '<button aria-label="Remove attachment"></button>' * 2
            if legacy:
                composer = '<form>' + buttons + '<div><textarea></textarea><input type="file" multiple><div data-testid="hatch-composer-placeholder-overlay"></div></div></form>'
            else:
                composer = '<section>' + buttons + '<div><textarea></textarea><input id="uploader" type="file" multiple></div></section>'
            html = '<main>' + history + composer + '</main>'
            page.js("document.body.innerHTML=" + json.dumps(html))
        fixture()
        observed = page.js(count_script)
        print(json.dumps({"case": "new_no_overlay_no_form", "count": observed}), flush=True)
        assert observed == 2, "new composer attachments not recognized"
        if not args.probe_only:
            fixture(legacy=True)
            assert page.js(count_script) == 2
            fixture(history_only=True)
            assert page.js(count_script) == 0, "history attachments counted as new uploads"
            fixture()
            engine = Engine(SimpleNamespace(profile_dir=home))
            engine.page = page
            upload = Path(home) / "fixture.txt"
            upload.write_text("fixture file visible to the browser")
            engine._set_input_files([str(upload)])
            assert page.js("document.querySelector('#uploader').files.length") == 1
            # Read content through CDP to detect inaccessible host /tmp paths under Snap.
            response = page.send("Runtime.evaluate", {
                "expression": "document.querySelector('#uploader').files[0].text()",
                "awaitPromise": True, "returnByValue": True})
            assert response["result"]["result"]["value"] == upload.read_text(), response
            assert page.js("document.querySelector('.chat-user-bubble input').files.length") == 0
            print("PASS: old/new layouts, history exclusion, correct input and actual file bytes", flush=True)
    finally:
        if page:
            try:
                page.send("Browser.close")
            except Exception:
                pass  # Browser may close the CDP socket before acknowledging.
            page.close()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(.2)
        log.close()
