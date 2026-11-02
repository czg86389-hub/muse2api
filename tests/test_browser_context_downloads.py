"""No-network regression: concurrent browser downloads cannot cross accounts."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("download_engine", ROOT / "engine.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Browser:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_id = 0
    def send(self, method, params=None):
        if method == "Target.createBrowserContext":
            with self.lock:
                self.next_id += 1
                return {"result": {"browserContextId": f"ctx-{self.next_id}"}}
        if method == "Target.createTarget":
            return {"result": {"targetId": params["browserContextId"]}}
        return {"result": {}}


barrier = threading.Barrier(2)


class Page:
    def __init__(self, url, timeout):
        self.id = url.rsplit("/", 1)[1]
        self.directory = None
    def send(self, method, params=None):
        if method == "Browser.setDownloadBehavior":
            self.directory = Path(params["downloadPath"])
        return {}
    def js(self, _script):
        (self.directory / "same-filename.mp4").write_bytes(self.id.encode())
        barrier.wait(timeout=5)
        return "ok"
    def close(self):
        pass


with tempfile.TemporaryDirectory(prefix="muse-download-fixture-") as home:
    cfg = SimpleNamespace(profile_dir=home, download_dir=f"{home}/downloads", cdp_port=1)
    e = mod.MuseEngine(cfg)
    e.browser = Browser()
    assert e.download_dir == cfg.download_dir  # Existing standalone fixtures stay compatible.

    def download(_index):
        page = e._open_page()
        e.page = page
        directory = page.directory
        try:
            path = e._download_fallback("blob:selected", timeout=5)
            assert path is not None
            data = Path(path).read_bytes()
            assert data == page.id.encode(), (page.id, data)
            return str(directory), data
        finally:
            e.close_current_page()
            assert not directory.exists(), "context download directory leaked"

    with patch.object(mod, "CDP", Page), patch.object(
            mod, "time", SimpleNamespace(time=time.time, sleep=lambda _: None)):
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(download, range(2)))
    assert result[0][0] != result[1][0], result
    assert result[0][1] != result[1][1], result
    assert not e._active_contexts and not e._download_dirs

    for proxied in (False, True):
        e.browser = SimpleNamespace(send=lambda *args, **kwargs: {"error": {"message": "fixture"}})
        with patch("requests.put") as direct:
            try:
                e._open_page({"server": "http://127.0.0.1:1"} if proxied else None)
            except mod.MuseGenerationError:
                pass
            else:
                raise AssertionError("missing context id must fail closed")
            direct.assert_not_called()
        assert not e._active_contexts and not e._download_dirs and not e._proxy_relays

print("PASS: two context downloads isolated, cleaned, and context failure never falls back")
