"""Media retention, size cap, startup orphan sweep and cleanup settings API."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cleanup  # noqa: E402


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body) if self.body else {}


class Client:
    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, body=b"", headers=None):
        hdr = {**self.headers, **(headers or {})}
        hdr["content-length"] = str(len(body))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
            "server": ("test", 80), "client": ("test", 1),
        }
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in messages
                           if m["type"] == "http.response.body")
        return Response(status, payload)

    async def get(self, path):
        return await self.request("GET", path)

    async def post(self, path, obj=None):
        return await self.request("POST", path, json.dumps(obj or {}).encode(),
                                  {"content-type": "application/json"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def make_cfg(tmp):
    media = os.path.join(tmp, "media")
    downloads = os.path.join(tmp, "downloads")
    profile = os.path.join(tmp, "profile")
    for d in (media, downloads, profile):
        os.makedirs(d, exist_ok=True)
    return SimpleNamespace(data_dir=tmp, media_dir=media,
                           download_dir=downloads, profile_dir=profile)


def write(path, size=10, age=0, now=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if age:
        stamp = (now or time.time()) - age
        os.utime(path, (stamp, stamp))


def enabled(days=1, max_gb=0.0):
    return {"media_cleanup_enabled": True, "media_retention_days": days,
            "media_max_gb": max_gb, "cleanup_interval_minutes": 60,
            "debug_retention_days": 7}


def test_age_and_hidden_files(tmp):
    cfg = make_cfg(tmp)
    now = time.time()
    write(os.path.join(cfg.media_dir, "old.webp"), age=2 * 86400, now=now)
    write(os.path.join(cfg.media_dir, "fresh.webp"), age=60, now=now)
    write(os.path.join(cfg.media_dir, ".gitkeep"), age=5 * 86400, now=now)

    result = cleanup.run_media_cleanup(cfg, enabled(days=1), now=now, grace_seconds=0)
    assert result["status"] == "ok", result
    assert result["deleted"] == 1, result
    assert os.path.isfile(os.path.join(cfg.media_dir, "fresh.webp"))
    assert os.path.isfile(os.path.join(cfg.media_dir, ".gitkeep"))
    assert not os.path.isfile(os.path.join(cfg.media_dir, "old.webp"))
    assert result["remaining"]["count"] == 1, result


def test_size_cap_deletes_oldest(tmp):
    cfg = make_cfg(tmp)
    now = time.time()
    names = ["a.webp", "b.webp", "c.webp"]
    for index, name in enumerate(names):
        write(os.path.join(cfg.media_dir, name), size=500, age=1000 - index * 100, now=now)

    # 3 x 500 bytes; cap ~697 bytes => delete the two oldest until one remains.
    result = cleanup.run_media_cleanup(cfg, enabled(days=0, max_gb=0.00000065),
                                       now=now, grace_seconds=0)
    assert result["deleted"] == 2, result
    assert os.path.isfile(os.path.join(cfg.media_dir, "c.webp"))
    assert result["remaining"]["bytes"] <= 700, result


def test_disabled_and_zero_limits(tmp):
    cfg = make_cfg(tmp)
    now = time.time()
    write(os.path.join(cfg.media_dir, "old.webp"), age=10 * 86400, now=now)

    result = cleanup.run_media_cleanup(cfg, enabled(days=1), now=now, grace_seconds=0)
    assert result["deleted"] == 1, result

    write(os.path.join(cfg.media_dir, "old2.webp"), age=10 * 86400, now=now)
    result = cleanup.run_media_cleanup(
        cfg, {**enabled(days=0, max_gb=0), "media_cleanup_enabled": False},
        now=now, grace_seconds=0)
    assert result["status"] == "skipped" and result["deleted"] == 0, result
    assert os.path.isfile(os.path.join(cfg.media_dir, "old2.webp"))

    result = cleanup.run_media_cleanup(
        cfg, {**enabled(days=0, max_gb=0), "media_cleanup_enabled": False},
        now=now, grace_seconds=0, force=True)
    assert result["deleted"] == 0, result
    assert os.path.isfile(os.path.join(cfg.media_dir, "old2.webp"))


def test_grace_and_active_task_protection(tmp):
    cfg = make_cfg(tmp)
    now = time.time()
    write(os.path.join(cfg.media_dir, "busy.webp"), size=800, age=10, now=now)
    write(os.path.join(cfg.media_dir, "protected.webp"), size=800, age=5000, now=now)

    result = cleanup.run_media_cleanup(
        cfg, enabled(days=0, max_gb=0.000001), protected_names={"protected.webp"},
        now=now, grace_seconds=300)
    assert result["deleted"] == 0, result
    assert os.path.isfile(os.path.join(cfg.media_dir, "busy.webp"))
    assert os.path.isfile(os.path.join(cfg.media_dir, "protected.webp"))


def test_startup_sweep(tmp):
    cfg = make_cfg(tmp)
    now = time.time()
    write(os.path.join(cfg.download_dir, "context-a", "f.bin"), size=12, age=0, now=now)
    write(os.path.join(cfg.profile_dir, "muse2api-refs-x", "f.bin"), size=7, age=0, now=now)
    write(os.path.join(cfg.profile_dir, "keep", "f.bin"), size=5, age=0, now=now)
    write(os.path.join(cfg.data_dir, "debug-old.json"), size=3, age=10 * 86400, now=now)
    write(os.path.join(cfg.data_dir, "debug-new.json"), size=4, age=0, now=now)
    write(os.path.join(cfg.media_dir, "keep.webp"), size=6, age=10 * 86400, now=now)
    write(os.path.join(cfg.download_dir, "ordinary.txt"), size=2, age=0, now=now)

    result = cleanup.sweep_orphans(cfg, debug_retention_days=7, now=now)
    assert result["dirs_removed"] == 2, result
    assert result["debug_removed"] == 1, result
    assert not os.path.exists(os.path.join(cfg.download_dir, "context-a"))
    assert not os.path.exists(os.path.join(cfg.profile_dir, "muse2api-refs-x"))
    assert os.path.isdir(os.path.join(cfg.profile_dir, "keep"))
    assert not os.path.exists(os.path.join(cfg.data_dir, "debug-old.json"))
    assert os.path.isfile(os.path.join(cfg.data_dir, "debug-new.json"))
    assert os.path.isfile(os.path.join(cfg.media_dir, "keep.webp"))
    assert os.path.isfile(os.path.join(cfg.download_dir, "ordinary.txt"))


async def test_api(tmp):
    os.environ.update(MUSE2API_HOME=tmp, MUSE2API_PROFILE_ROOT=tmp,
                      MUSE2API_KEY="cleanup-secret")
    spec = importlib.util.spec_from_file_location("app_cleanup_candidate", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    auth = {"Authorization": "Bearer cleanup-secret"}
    async with Client(module.app, {}) as anonymous:
        assert (await anonymous.get("/admin/cleanup")).status_code == 401
        assert (await anonymous.post("/admin/cleanup/run")).status_code == 401

    async with Client(module.app, auth) as client:
        res = await client.get("/admin/cleanup")
        assert res.status_code == 200, res.body[:300]
        data = res.json()
        assert data["enabled"] is True and data["retention_days"] == 1, data
        assert data["max_gb"] == 1.0 and data["max_bytes"] == 1024 ** 3, data
        assert data["interval_minutes"] == 60, data
        assert data["media"]["count"] == 0, data

        res = await client.post("/admin/settings", {
            "media_cleanup_enabled": True, "media_retention_days": 2,
            "media_max_gb": 0.5, "cleanup_interval_minutes": 30})
        assert res.status_code == 200, res.body[:300]
        data = res.json()
        assert data["media_retention_days"] == 2 and data["media_max_gb"] == 0.5, data
        assert data["cleanup_interval_minutes"] == 30, data

        fresh = type(module.store)(module.CFG)
        assert fresh.get_settings()["media_retention_days"] == 2

        for payload in ({"media_retention_days": -1},
                        {"media_max_gb": -1},
                        {"cleanup_interval_minutes": 1},
                        {"debug_retention_days": 0}):
            res = await client.post("/admin/settings", payload)
            assert res.status_code == 400, (payload, res.status_code, res.body[:200])

        # Manual run ignores the enabled flag and removes an old file.
        module.store.save_settings(media_cleanup_enabled=False)
        old = os.path.join(module.CFG.media_dir, "old.webp")
        write(old, age=10 * 86400)
        res = await client.post("/admin/cleanup/run")
        assert res.status_code == 200, res.body[:300]
        result = res.json()
        assert result["deleted"] == 1 and result["remaining"]["count"] == 0, result
        assert not os.path.exists(old)
        assert module.store.get_settings()["media_cleanup_enabled"] is False

        res = await client.get("/admin/cleanup")
        data = res.json()
        assert data["last_run"]["reason"] == "manual", data
        assert data["last_run"]["deleted"] == 1, data


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-cleanup-") as tmp:
        # Unit runs keep their own subdirectory; the API run reuses the temp home.
        for name, fn in (("age", test_age_and_hidden_files),
                         ("size", test_size_cap_deletes_oldest),
                         ("disabled", test_disabled_and_zero_limits),
                         ("grace", test_grace_and_active_task_protection),
                         ("sweep", test_startup_sweep)):
            sub = os.path.join(tmp, name)
            os.makedirs(sub, exist_ok=True)
            fn(sub)
        await test_api(os.path.join(tmp, "api"))
    print("PASS: media retention, size cap, grace/protection, startup sweep and cleanup API")


if __name__ == "__main__":
    asyncio.run(main())
