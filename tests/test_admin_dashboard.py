"""Aggregate /admin/dashboard contract. No browser, network or live account."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body) if self.body else {}


class Client:
    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def get(self, path):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in self.headers.items()],
            "server": ("test", 80), "client": ("test", 1),
        }
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-dash-") as tmp:
        os.environ.update(MUSE2API_HOME=tmp, MUSE2API_PROFILE_ROOT=tmp,
                          MUSE2API_KEY="dash-secret", MUSE2API_IMAGES="x")
        spec = importlib.util.spec_from_file_location("app_dash_candidate", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        now = int(time.time())
        ok = module.store.add_account({"hatch_sess": "ok"}, "main")
        module.store.mark(ok["id"], True, "session ok")
        module.store.update_account(ok["id"], quota={
            "found": True, "plan": "Free plan", "weekly_used_pct": 30,
            "extra_left": "1B", "checked_at": now})

        bad = module.store.add_account({"hatch_sess": "bad"}, "backup")
        module.store.mark(bad["id"], False, "HTTP 401")
        module.store.update_account(bad["id"], quota={
            "found": True, "plan": "Free plan", "weekly_used_pct": 85,
            "weekly_reset": "2026-11-10", "checked_at": now})

        expired = module.store.add_account({"hatch_sess": "old"}, "old")
        module.store.update_account(expired["id"], expires_at=now - 60)

        t1 = module.store.create_task("image", "a red apple")
        module.store.update_task(t1["id"], status="completed", elapsed=12.5,
                                 result={"url": "/v1/media/a.webp", "bytes": 10})
        t2 = module.store.create_task("video", "a walking cat")
        module.store.update_task(t2["id"], status="failed", elapsed=9.0, error="boom")
        t3 = module.store.create_task("audio", "hello")
        module.store.update_task(t3["id"], status="processing")

        media = module.CFG.media_dir
        os.makedirs(media, exist_ok=True)
        for name, data in (("a.webp", b"x" * 10), ("b.mp4", b"y" * 20), ("c.mp3", b"z" * 30)):
            with open(os.path.join(media, name), "wb") as f:
                f.write(data)

        async with Client(module.app, {}) as anonymous:
            res = await anonymous.get("/admin/dashboard")
            assert res.status_code == 401, res.status_code

        async with Client(module.app, {"Authorization": "Bearer dash-secret"}) as client:
            page = await client.get("/dashboard")
            assert page.status_code == 200 and b'id="tab-dash"' in page.body, page.status_code
            res = await client.get("/admin/dashboard")
            assert res.status_code == 200, (res.status_code, res.body[:300])
            data = res.json()

        a = data["accounts"]
        assert (a["total"], a["enabled"], a["healthy"], a["error"], a["unknown"]) == (3, 3, 1, 1, 1), a
        assert a["expired"] == 1, a

        t = data["tasks"]
        assert (t["total"], t["success"], t["failed"], t["active"]) == (3, 1, 1, 1), t
        assert t["success_rate"] == 50.0, t
        assert t["by_kind"] == {"image": 1, "video": 1, "audio": 1}, t
        assert sum(day["total"] for day in t["daily"]) == 3, t["daily"]
        assert len(t["recent"]) == 3, t["recent"]

        m = data["media"]
        assert (m["count"], m["bytes"]) == (3, 60), m
        assert m["by_kind"] == {"image": 1, "video": 1, "audio": 1}, m
        assert len(m["recent"]) == 3, m

        q = data["quota"]
        assert q["tracked"] == 2 and q["high"] == 1, q
        assert q["average_weekly_used"] == 57.5, q
        assert [row["weekly_used_pct"] for row in q["accounts"]] == [85.0, 30.0], q

        codes = {alert["code"] for alert in data["alerts"]}
        assert {"account_errors", "expired", "quota_high"} <= codes, data["alerts"]
        assert data["uptime"] >= 0 and data["started_at"] <= data["now"]
        assert data["config"]["concurrency"] >= 1
        assert data["browser_running"] in (True, False)

    print("PASS: dashboard aggregates accounts, tasks, media, quota and alerts")


if __name__ == "__main__":
    asyncio.run(main())
