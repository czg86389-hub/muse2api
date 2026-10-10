"""Run: python tests/test_admin_base_url.py; uses no live account, no browser."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent.parent / "app.py"


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class Client:
    """Minimal ASGI test client using the standard library, no httpx dependency."""

    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, headers=None):
        hdr = {**self.headers, **(headers or {})}
        hdr["content-length"] = "0"
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
                 "server": ("test", 80), "client": ("test", 1)}
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def build_cmd(base_url):
    """admin.html:886"""
    return re.sub(r"/v1/?$", "", base_url or "http://host:18610").rstrip("/")


async def check(source, home, public_base):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE=public_base,
                      REDIS_URL="memory://", MUSE2API_REDIS_URL="memory://")
    sys.path.insert(0, str(source.parent))
    for name in ("config", "store", "engine", "cdp"):
        sys.modules.pop(name, None)          # config reads MUSE2API_* at import
    spec = importlib.util.spec_from_file_location("admin_base_url_test_target", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    checks = 0

    def expect(condition, message):
        nonlocal checks
        checks += 1
        assert condition, message

    async with Client(module.app, {"Authorization": "Bearer test-only"}) as client:
        status = await client.get("/admin/status")
        expect(status.status_code == 200, "GET /admin/status failed")
        served = status.json()
        apikey = await client.get("/admin/apikey")
        expect(apikey.status_code == 200, "GET /admin/apikey failed")

        # admin.html:1438 only writes #inBase when base_url is truthy
        expect(bool(served["base_url"]) == bool(public_base),
               f"base_url={served['base_url']!r} for PUBLIC_BASE={public_base!r}")
        expect(served["base_url"] == apikey.json()["base_url"],
               "/admin/status and /admin/apikey disagree")
        expect(served["config"]["public_base"] == public_base.rstrip("/"),
               "config.public_base must echo the configured value")

        base_arg = build_cmd(served["base_url"])
        expect(bool(base_arg),
               f"buildCmd() gets an empty --base from {served['base_url']!r}")
        print(f"public_base={public_base or '<unset>'} base_url={served['base_url']!r} "
              f"cmd_base={base_arg} cmd_ok=PASS")
    return checks


if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else SOURCE
    total = 0
    for value in ("", "https://api.example.com"):
        with tempfile.TemporaryDirectory(prefix="muse-admin-base-test-") as home:
            total += asyncio.run(check(source, home, value))
    print(f"PASS {total} checks; no live accounts/network/browser")