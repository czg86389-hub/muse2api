"""Run: python tests/test_async_images.py [path/to/app.py]; uses no live account."""
import asyncio
import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

import json


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class Client:
    """Minimal ASGI test client using the standard library, no httpx dependency."""
    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, json_data=None, data=None, files=None, headers=None):
        hdr = {**self.headers, **(headers or {})}
        body = b""
        if files:
            boundary = "muse-test-boundary"
            chunks = []
            for key, value in (data or {}).items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            for key, (filename, payload, mime) in files.items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + payload + b"\r\n")
            body = b"".join(chunks) + f"--{boundary}--\r\n".encode()
            hdr["content-type"] = "multipart/form-data; boundary=" + boundary
        elif json_data is not None:
            body = json.dumps(json_data).encode()
            hdr["content-type"] = "application/json"
        hdr["content-length"] = str(len(body))
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
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def post(self, path, json=None, **kwargs):
        return await self.request("POST", path, json_data=json, **kwargs)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def check(source, home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="",
                      REDIS_URL="memory://", MUSE2API_REDIS_URL="memory://")
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location("image_api_test_target", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    calls = []
    release = threading.Event()
    media = Path(module.CFG.media_dir) / "fixture.png"
    media.write_bytes(b"image fixture bytes")

    def generation(prompt, kind, timeout, **kwargs):
        calls.append((prompt, kind, timeout, kwargs.get("reference_image")))
        assert release.wait(2), "test worker not released"
        if "FAIL_FIXTURE" in prompt:
            raise module.MuseGenerationError("fixture generation failed")
        return {"filename": media.name, "size": media.stat().st_size,
                "kind": "image", "path": str(media)}, "fixture-account"

    locked_impl = module._run_generation_locked if hasattr(module, "_run_generation_locked") else None
    if locked_impl:
        module.store.add_account(
            {"hatch_sess": "s", "hatch_vml": "v", "hatch_native_auth_device": "d"}, "lock")
        lock_id = module.store.accounts[-1]["id"]

        def assert_owned(*args, **kwargs):
            assert kwargs.get("account_id") in module.ACCOUNT_GATE.busy_ids(), "account slot must cover retry/cleanup"
            assert kwargs.get("deadline") is not None
            return {"fixture": True}, kwargs.get("account_id")
        module._run_generation_locked = assert_owned
        module._run_generation("lock fixture", "image", 1)
        module._run_generation_locked = locked_impl
        assert lock_id not in module.ACCOUNT_GATE.busy_ids()
        assert module.ACCOUNT_GATE.try_acquire(lock_id)
        try:
            module._run_generation("queue fixture", "image", 1)
            raise AssertionError("busy account must time out")
        except module.MuseGenerationError:
            pass
        finally:
            module.ACCOUNT_GATE.release(lock_id)
        print("generation_lock_covers_retry_cleanup=PASS queue_wait_bounded=PASS")
    def publish_generated_media(res, include_b64=False):
        out = dict(res)
        out["url"] = "https://source.openclaw-token.shop/uploads/" + res["filename"]
        out["path"] = None
        if include_b64:
            out["b64_json"] = base64.b64encode(Path(res["path"]).read_bytes()).decode()
        return out

    module.publish_generated_media = publish_generated_media
    module._run_generation = generation
    headers = {"Authorization": "Bearer test-only"}
    async with Client(module.app, headers) as client:
        # Old endpoint blocks until result; the new endpoint must acknowledge before release.
        timer = threading.Timer(0.25, release.set)
        timer.start()
        t0 = time.monotonic()
        response = await client.post("/v1/images/generations", json={
            "prompt": "change background", "async": True, "response_format": "b64_json"})
        elapsed = time.monotonic() - t0
        timer.join()
        print(f"async_generation_http={response.status_code} returned_before_completion={elapsed < 0.2}")
        assert response.status_code == 202 and elapsed < 0.2, "image endpoint still synchronous"
        tid = response.json()["task_id"]

        async def finish(task_id):
            for _ in range(200):
                result = await client.get("/v1/images/tasks/" + task_id)
                assert result.status_code == 200
                data = result.json()
                if data["status"] in ("completed", "failed"):
                    return data
                await asyncio.sleep(0.02)
            raise AssertionError("task never finished")

        done = await finish(tid)
        assert done["status"] == "completed" and done["progress"] == 100
        assert base64.b64decode(done["data"][0]["b64_json"]) == media.read_bytes()
        assert len(calls) == 1
        await client.get("/v1/images/tasks/" + tid)
        assert len(calls) == 1, "polling must not rerun generation"
        print("completed_b64_roundtrip=PASS polling_does_not_regenerate=PASS")

        response = await client.post("/v1/images/edits", data={
            "prompt": "edit reference", "async": "true"},
            files={"image": ("reference.png", b"fixture reference", "image/png")})
        assert response.status_code == 202
        done = await finish(response.json()["task_id"])
        assert done["status"] == "completed"
        assert done["data"][0]["url"].endswith("fixture.png")
        assert calls[-1][3] == "data:image/png;base64," + base64.b64encode(b"fixture reference").decode()
        response = await client.post("/v1/images/edits", json={
            "prompt": "edit JSON reference", "async": True, "reference_image": "data:image/png;base64,eA=="})
        assert response.status_code == 202
        assert (await finish(response.json()["task_id"]))["status"] == "completed"
        assert calls[-1][3] == "data:image/png;base64,eA=="
        print("multipart_and_json_reference_forwarding=PASS")

        body = {"prompt": "dedicated async request", "reference_image": "data:image/png;base64,eA=="}
        response = await client.post("/v1/images/tasks", json=body, headers={"Idempotency-Key": "fixture-key"})
        assert response.status_code == 202
        dedicated_id = response.json()["id"]
        done = await finish(dedicated_id)
        assert done["result"]["url"] == done["url"] and done["status"] == "completed"
        count = len(calls)
        duplicate = await client.post("/v1/images/tasks", json=body, headers={"Idempotency-Key": "fixture-key"})
        assert duplicate.status_code == 202 and duplicate.json()["id"] == dedicated_id
        assert len(calls) == count
        conflict = await client.post("/v1/images/tasks", json={"prompt": "different"}, headers={"Idempotency-Key": "fixture-key"})
        assert conflict.status_code == 409
        print("dedicated_task_endpoint=PASS idempotency_reuse=PASS conflict_rejected=PASS")

        response = await client.post("/v1/images/generations", json={"prompt": "sync compatible"})
        assert response.status_code == 200 and response.json()["data"]
        response = await client.post("/v1/images/edits", json={"prompt": "sync compatible", "async": False})
        assert response.status_code == 200 and response.json()["data"]
        print("sync_compatibility=PASS")

        response = await client.post("/v1/images/generations", json={"prompt": "FAIL_FIXTURE", "async": True})
        done = await finish(response.json()["task_id"])
        assert done["status"] == "failed" and done["error"] == "fixture generation failed"
        assert (await client.get("/v1/images/tasks/missing")).status_code == 404
        assert (await client.get("/v1/images/tasks/" + tid,
                                 headers={"Authorization": "Bearer wrong"})).status_code == 401
        print("failure_terminal=PASS authentication=PASS missing_task=PASS")

        module.DISPATCHER.pause()
        time.sleep(0.45)
        module.store.set_queue_limit(1)
        held = await client.post("/v1/images/tasks", json={"prompt": "queue holds one"})
        assert held.status_code == 202, held.body
        full = await client.post("/v1/images/tasks", json={"prompt": "queue full"})
        assert full.status_code == 429, full.body
        module.DISPATCHER.resume()
        done = await finish(held.json()["id"])
        assert done["status"] == "completed"
        print("bounded_queue=PASS")
        pending = module.store.create_task("image", "interrupted fixture")
        await module._startup()
        assert module.store.get_task(pending["id"])["status"] == "failed"
        print("restart_marks_interrupted_tasks_failed=PASS")
    print("PASS async image API regression")


if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "app.py"
    with tempfile.TemporaryDirectory(prefix="muse-image-api-test-") as home:
        asyncio.run(check(source, home))
