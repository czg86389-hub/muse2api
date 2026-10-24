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
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="")
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
        references = kwargs.get("reference_images")
        if references is None and kwargs.get("reference_image"):
            references = [kwargs["reference_image"]]
        calls.append((prompt, kind, timeout, references or []))
        assert release.wait(2), "test worker not released"
        if "FAIL_FIXTURE" in prompt:
            raise module.MuseGenerationError("fixture generation failed")
        return {"filename": media.name, "size": media.stat().st_size,
                "kind": "image", "path": str(media)}, "fixture-account"

    locked_impl = module._run_generation_locked if hasattr(module, "_run_generation_locked") else None
    if locked_impl:
        def assert_owned(*args, **kwargs):
            assert module.GEN_LOCK.locked(), "browser lock must cover retry/cleanup"
            assert kwargs.get("deadline") is not None
            return {"fixture": True}, None
        module._run_generation_locked = assert_owned
        module._run_generation("lock fixture", "image", 1)
        module._run_generation_locked = locked_impl
        assert not module.GEN_LOCK.locked()
        real_lock = module.GEN_LOCK
        class BusyLock:
            def acquire(self, timeout):
                assert timeout == 1
                return False
        module.GEN_LOCK = BusyLock()
        try:
            module._run_generation("queue fixture", "image", 1)
            raise AssertionError("busy browser must time out")
        except module.MuseGenerationError:
            pass
        finally:
            module.GEN_LOCK = real_lock
        print("generation_lock_covers_retry_cleanup=PASS queue_wait_bounded=PASS")
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
            for _ in range(100):
                result = await client.get("/v1/images/tasks/" + task_id)
                assert result.status_code == 200
                data = result.json()
                if data["status"] in ("completed", "failed"):
                    return data
                await asyncio.sleep(0.01)
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
        assert calls[-1][3] == ["data:image/png;base64," + base64.b64encode(b"fixture reference").decode()]
        response = await client.post("/v1/images/edits", json={
            "prompt": "edit JSON reference", "async": True, "reference_image": "data:image/png;base64,eA=="})
        assert response.status_code == 202
        assert (await finish(response.json()["task_id"]))["status"] == "completed"
        assert calls[-1][3] == ["data:image/png;base64,eA=="]
        response = await client.post("/v1/images/edits", json={
            "prompt": "edit two references", "async": True,
            "images": [
                {"url": "data:image/png;base64,Zmlyc3Q="},
                {"image_url": "data:image/png;base64,c2Vjb25k"},
            ]})
        assert response.status_code == 202
        assert (await finish(response.json()["task_id"]))["status"] == "completed"
        assert calls[-1][3] == [
            "data:image/png;base64,Zmlyc3Q=", "data:image/png;base64,c2Vjb25k"]
        response = await client.post("/v1/images/generations", json={
            "prompt": "generate from two references", "async": True,
            "images": ["https://example.com/first.png", "https://example.com/second.png"]})
        assert response.status_code == 202
        assert (await finish(response.json()["task_id"]))["status"] == "completed"
        assert calls[-1][3] == [
            "https://example.com/first.png", "https://example.com/second.png"]
        print("multipart_and_json_reference_forwarding=PASS")

        response = await client.post("/v1/videos", json={
            "prompt": "animate two references", "duration": 6,
            "images": ["https://example.com/start.png", "https://example.com/style.png"]})
        assert response.status_code == 200
        video_id = response.json()["task_id"]
        for _ in range(100):
            video = await client.get("/v1/videos/" + video_id)
            assert video.status_code == 200
            if video.json()["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.01)
        assert video.json()["status"] == "completed"
        assert calls[-1][1] == "video"
        assert calls[-1][3] == [
            "https://example.com/start.png", "https://example.com/style.png"]
        assert "2 张参考图片" in calls[-1][0]
        print("multi_reference_video_forwarding=PASS")

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

        for _ in range(8):
            module.store.create_task("image", "queued fixture")
        full = await client.post("/v1/images/tasks", json={"prompt": "queue full"})
        assert full.status_code == 429
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
