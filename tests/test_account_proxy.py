"""Run: PYTHONPATH=. venv/bin/python tests/test_account_proxy.py [--browser].

Uses temporary accounts and local authenticated proxy fixtures only.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-proxy-test-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="test-only")
import app
from config import Config
from engine import MuseEngine, MuseGenerationError
from proxy import HTTPProxyRelay, normalize_server, request_proxies
from store import Store


async def request(method, path, body=None):
    payload = json.dumps(body).encode() if body is not None else b""
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("test", 80), "client": ("test", 1),
             "headers": [(b"authorization", b"Bearer test-only"),
                         (b"content-type", b"application/json"),
                         (b"content-length", str(len(payload)).encode())]}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)

    await app.app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in messages)
    return status, json.loads(data)


class FixtureProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def authenticate(self):
        token = "Basic " + base64.b64encode(self.server.credentials.encode()).decode()
        self.server.seen.append((self.command, self.path, self.headers.get("Proxy-Authorization")))
        if self.headers.get("Proxy-Authorization") != token:
            self.send_response(407)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return False
        return True

    def do_GET(self):
        if not self.authenticate():
            return
        data = ("<html><body>" + self.server.label + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_CONNECT(self):
        if not self.authenticate():
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        if self.server.tls is not None:
            with self.server.tls.wrap_socket(self.connection, server_side=True) as tls:
                tls.settimeout(5)
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    byte = tls.recv(1)
                    if not byte:
                        return
                    headers.extend(byte)
                body = self.server.label.encode()
                tls.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                            + str(len(body)).encode() + b"\r\n\r\n" + body)
        else:
            data = self.rfile.read(4)
            self.wfile.write(data.upper())
            self.wfile.flush()
        self.close_connection = True


class ProxyFixture:
    def __init__(self, label="proxy-one", tls=None):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureProxy)
        # Chromium may abandon speculative TLS connections to the fixture.
        self.server.handle_error = lambda *args: None
        self.server.daemon_threads = True
        self.server.label = label
        self.server.credentials = label + ":p@ss:word"
        self.server.seen = []
        self.server.tls = tls
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"server": f"http://127.0.0.1:{self.server.server_port}",
                       "username": label, "password": "p@ss:word"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class AccountProxyTests(unittest.TestCase):
    def test_validation_and_secret_preservation(self):
        for bad in ("socks5://host:1080", "http://u:secret@host:80", "http://host:0",
                    "http://host:65536", "http://host/path", "http://host/?x=secret", "http://host\n"):
            with self.subTest(server=bad), self.assertRaises(ValueError):
                normalize_server(bad)
        self.assertEqual(normalize_server("http://[::1]:8080/"), "http://[::1]:8080")
        status, created = asyncio.run(request("POST", "/admin/accounts", {
            "cookies": {"hatch_sess": "fake-cookie"},
            "proxy": {"server": "http://proxy.example:80", "username": "user", "password": "secret-marker"}}))
        self.assertEqual(status, 200)
        aid = created["added"][0]["id"]
        path = "/admin/accounts/" + aid
        for url in ("/admin/accounts", "/admin/status"):
            status, result = asyncio.run(request("GET", url))
            self.assertEqual(status, 200)
            self.assertNotIn("secret-marker", json.dumps(result))
            self.assertNotIn("fake-cookie", json.dumps(result))
        status, result = asyncio.run(request("PATCH", path, {"label": "changed"}))
        self.assertEqual(status, 200)
        self.assertTrue(result["proxy"]["has_password"])
        status, _ = asyncio.run(request("PATCH", path, {"proxy": {"server": "http://proxy.example:80", "username": "user"}}))
        self.assertEqual(status, 200)
        self.assertEqual(app.store.get_account(aid)["proxy"]["password"], "secret-marker")
        reloaded = Store(app.CFG)
        self.assertEqual(reloaded.get_account(aid)["proxy"]["password"], "secret-marker")
        status, result = asyncio.run(request("PATCH", path, {"proxy": {"server": "http://other:80", "username": "user"}}))
        self.assertEqual(status, 400)
        status, result = asyncio.run(request("PATCH", path, {"proxy": {"server": "http://u:secret-marker@host"}}))
        self.assertEqual(status, 422)
        self.assertNotIn("secret-marker", json.dumps(result))
        self.assertTrue(app.store.get_account(aid)["proxy"])
        status, result = asyncio.run(request("PATCH", path, {"proxy": None}))
        self.assertEqual(status, 200)
        self.assertIsNone(result["proxy"])

    def test_keepalive_and_pre_renew_use_account_proxy(self):
        proxy = {"server": "http://proxy.example:80", "username": "user", "password": "p@ss:word"}
        account = app.store.add_account({"hatch_sess": "fake-cookie"})
        app.store.update_account(account["id"], proxy=proxy)
        reply = SimpleNamespace(status_code=200, cookies=[], json=lambda: {
            "status": "assigned", "vm_id": "fixture", "vm_state": "RUNNING"})
        with patch("requests.get", return_value=reply) as get, patch("requests.post", return_value=reply) as post:
            self.assertTrue(app._probe_account_sync(account["id"])["ok"])
            app._renew_and_persist(account["id"], force=True)
            for call in get.call_args_list + post.call_args_list:
                self.assertEqual(call.kwargs["proxies"], request_proxies(proxy)["proxies"])
                self.assertFalse(call.kwargs["allow_redirects"])
        error = __import__("requests").exceptions.ProxyError("http://user:secret-marker@host")
        with patch("requests.get", side_effect=error):
            with self.assertRaises(MuseGenerationError) as caught:
                MuseEngine.renew_session_http({}, proxy=proxy)
            self.assertNotIn("secret-marker", str(caught.exception))

    def test_context_creation_failure_never_falls_back_direct(self):
        engine = MuseEngine(Config(base_dir=HOME.name, profile_root=HOME.name))
        engine.browser = SimpleNamespace(send=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("failed")))
        with patch("requests.put") as fallback:
            with self.assertRaises(MuseGenerationError):
                engine._open_page({"server": "http://127.0.0.1:1"})
            fallback.assert_not_called()
        self.assertFalse(engine._proxy_relays)

    def test_proxy_changes_replace_warm_sessions(self):
        engine = MuseEngine(Config(base_dir=HOME.name, profile_root=HOME.name))
        old_proxy = {"server": "http://one.example:80"}
        new_proxy = {"server": "http://two.example:80"}
        engine.current_acc_id = "fixture"
        engine._ensure_local().proxy_config = old_proxy
        engine._last_http_renew["fixture"] = time.time()
        engine.proxy_resolver = lambda aid: new_proxy
        from unittest.mock import Mock
        old_page, new_page = Mock(), Mock()
        engine.page = old_page
        with patch.object(engine, "_open_page", return_value=new_page) as open_page, \
                patch.object(engine, "_apply_cookies"), patch.object(engine, "_wait_ws_ready", return_value=True):
            self.assertIs(engine.ensure_page({}, account_id="fixture"), new_page)
            old_page.close.assert_called_once()
            open_page.assert_called_once_with(proxy=new_proxy)
        engine.close_current_page()

    def test_authenticated_http_and_connect_and_shutdown(self):
        import requests
        fixture = ProxyFixture()
        relay = HTTPProxyRelay(fixture.config)
        try:
            direct_to_proxy = requests.get("http://target.invalid/python", timeout=5,
                                           **request_proxies(fixture.config))
            self.assertEqual(direct_to_proxy.status_code, 200)
            response = requests.get("http://target.invalid/hello", proxies={"http": relay.url}, timeout=5)
            self.assertEqual(response.status_code, 200)
            self.assertIn("proxy-one", response.text)
            port = int(relay.url.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(b"CONNECT target.invalid:443 HTTP/1.1\r\nHost: target.invalid:443\r\n\r\n")
                response = bytearray()
                while not response.endswith(b"\r\n\r\n"):
                    response.extend(sock.recv(1))
                self.assertIn(b"200", response)
                sock.sendall(b"ping")
                self.assertEqual(sock.recv(4), b"PING")
            self.assertTrue(all(row[2].startswith("Basic ") for row in fixture.server.seen))
            bad_relay = HTTPProxyRelay({**fixture.config, "password": "wrong-password"})
            try:
                rejected = requests.get("http://target.invalid/rejected", proxies={"http": bad_relay.url}, timeout=5)
                self.assertEqual(rejected.status_code, 407)
            finally:
                bad_relay.close()
        finally:
            relay.close()
            fixture.close()
        self.assertFalse(relay.thread.is_alive())
        self.assertFalse(relay._sockets)


def browser_checks():
    """Exercise actual Chromium contexts, HTTPS tunnels, and UI against fixtures."""
    cert = Path(HOME.name) / "cert.pem"
    key = Path(HOME.name) / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=fixture.invalid", "-keyout", str(key), "-out", str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    fixtures = [ProxyFixture("proxy-one", tls), ProxyFixture("proxy-two", tls)]
    cfg = Config(base_dir=HOME.name, profile_root=HOME.name + "/browser", cdp_port=19311)
    engine = MuseEngine(cfg)
    try:
        engine.start()

        def check(fixture):
            page = engine._open_page(fixture.config)
            engine.page = page
            try:
                page.send("Security.setIgnoreCertificateErrors", {"ignore": True})
                for scheme in ("http", "https"):
                    url = f"{scheme}://fixture.invalid/hello"
                    result = page.send("Page.navigate", {"url": url})
                    assert not result.get("result", {}).get("errorText"), result
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        state = page.js("({url: location.href, body: document.body && document.body.innerText})")
                        if state.get("url") == url and state.get("body") == fixture.server.label:
                            break
                        time.sleep(0.1)
                    else:
                        raise AssertionError("Proxied browser navigation did not reach fixture")
            finally:
                engine.close_current_page()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(check, fixtures))
        for fixture in fixtures:
            assert any(row[0] == "CONNECT" and row[1] == "fixture.invalid:443" for row in fixture.server.seen)
        assert not engine._proxy_relays, "Proxy relays leaked after closing contexts"
        print("PASS: actual Chrome used distinct authenticated HTTP proxies and HTTPS tunnels concurrently")
        ui_checks(engine)
    finally:
        engine.stop()
        for fixture in fixtures:
            fixture.close()


def ui_checks(engine):
    """Test the actual dashboard form without touching the running installation."""
    env = dict(os.environ, MUSE2API_CDP_PORT="19312")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                               "--port", "18611", "--lifespan", "off"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        import urllib.request
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:18611/healthz", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("Temporary UI server did not start")
        page = engine._open_page()
        engine.page = page
        page.send("Page.addScriptToEvaluateOnNewDocument", {"source":
                  "window.__errors=[];window.addEventListener('error',e=>__errors.push(e.message));"})
        page.send("Page.navigate", {"url": "http://127.0.0.1:18611/admin?key=test-only"})

        def wait(expression):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if page.js(expression):
                    return
                time.sleep(0.1)
            raise AssertionError("UI condition failed: " + expression)

        wait("typeof accounts !== 'undefined' && accounts.length > 0")
        page.js("window.testId=accounts.find(a=>!a.proxy).id;openProxy(testId)")
        for width in (320, 768, 1440):
            page.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900,
                      "deviceScaleFactor": 1, "mobile": width == 320})
            assert page.js("$('proxyDialog').open && $('proxyDialog').getBoundingClientRect().width <= innerWidth-30")
        # Screenshot contains fixture fields only, never real account credentials.
        screenshot = page.send("Page.captureScreenshot", {"format": "png"})
        Path("/tmp/muse2api-proxy-dialog.png").write_bytes(base64.b64decode(screenshot["result"]["data"]))
        page.js("$('proxyServer').value='http://proxy.example:8080';$('proxyUsername').value='user';"
                "$('proxyPassword').value='ui-secret-marker';$('proxySave').click()")
        wait("!$('proxyDialog').open && accounts.find(a=>a.id===testId)?.proxy?.has_password")
        page.js("openProxy(testId)")
        assert page.js("$('proxyPassword').value === '' && $('proxyUsername').value === 'user'")
        page.js("$('proxySave').click()")
        wait("!$('proxyDialog').open")
        page.js("openProxy(testId);$('proxyServer').value='socks5://bad:1080';$('proxySave').click()")
        wait("$('proxyError').textContent.includes('http://host:port')")
        page.js("$('proxyDialog').close();openProxy(testId);$('proxyRemove').click()")
        wait("!$('proxyDialog').open && !accounts.find(a=>a.id===testId).proxy")
        assert page.js("window.__errors.length === 0"), page.js("window.__errors")
        print("PASS: dashboard save, password preservation, validation, removal, and modal widths 320/768/1440")
    finally:
        if page:
            engine.close_current_page()
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    browser = "--browser" in sys.argv
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AccountProxyTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful() and browser:
        browser_checks()
    HOME.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
