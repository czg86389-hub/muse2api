"""Redis 队列语义：满则拒绝，超出账号并发的任务留在队列里。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body) if self.body else {}


class Client:
    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, json_data=None, headers=None):
        hdr = {**self.headers, **(headers or {})}
        body = b""
        if json_data is not None:
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


def wait_until(pred, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def memory_fifo():
    from taskqueue import QueueFull, TaskQueue
    queue = TaskQueue("memory://")
    assert queue.enqueue("a", {"id": "a", "prompt": "one"}, 2) == 1
    assert queue.enqueue("b", {"id": "b", "prompt": "two"}, 2) == 2
    try:
        queue.enqueue("c", {"id": "c"}, 2)
        raise AssertionError("limit accepted a third job")
    except QueueFull:
        pass
    assert queue.queued_ids() == ["a", "b"]
    assert queue.pop()["id"] == "a"
    queue.remove("b")
    assert queue.pop() is None
    assert queue.length() == 0
    print("memory_fifo_limit_remove=PASS")


def load(home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="",
                      REDIS_URL="memory://", MUSE2API_REDIS_URL="memory://")
    spec = importlib.util.spec_from_file_location("task_queue_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.DISPATCHER.pause()
    time.sleep(0.45)
    return module


def add_account(module, label):
    return module.store.add_account(
        {"hatch_sess": label, "hatch_vml": label, "hatch_native_auth_device": label},
        label)


def main():
    memory_fifo()
    with tempfile.TemporaryDirectory(prefix="muse-queue-") as home:
        module = load(home)
        assert module.TASK_QUEUE.url.startswith("memory:")
        assert module.store.queue_limit() == 20
        for bad in (0, 501):
            try:
                module.store.set_queue_limit(bad)
                raise AssertionError(f"limit {bad} accepted")
            except ValueError:
                pass
        print("queue_limit_range=PASS")

        started = []

        def generation(prompt, kind, timeout, **kwargs):
            event = threading.Event()
            started.append({"prompt": prompt, "event": event})
            assert event.wait(5), "generation was not released"
            return {"filename": "x.png", "size": 4, "kind": kind,
                    "url": "https://example.com/x.png"}, "acc"

        module._run_generation = generation
        module.publish_generated_media = lambda res, include_b64=False: {
            **res, "url": res.get("url") or "https://example.com/x.png", "path": None}

        first = add_account(module, "a")
        second = add_account(module, "b")
        assert module.ACCOUNT_GATE.try_acquire(first["id"])
        assert module.ACCOUNT_GATE.try_acquire(second["id"])
        module.DISPATCHER.resume()
        held, _ = module._enqueue_task("image", "held", "held", 5)
        time.sleep(0.6)
        assert held["id"] in module.TASK_QUEUE.queued_ids()
        assert started == []
        module.ACCOUNT_GATE.release(first["id"])
        module.ACCOUNT_GATE.release(second["id"])
        assert wait_until(lambda: len(started) == 1)
        started[0]["event"].set()
        assert wait_until(lambda: (module.store.get_task(held["id"]) or {}).get("status") == "completed")
        print("busy_accounts_keep_generation_queued=PASS")

        module.DISPATCHER.pause()
        time.sleep(0.45)
        module.store.set_queue_limit(3)
        module.TASK_QUEUE.clear()
        one, pos1 = module._enqueue_task("image", "keep-1", "keep-1", 5)
        two, pos2 = module._enqueue_task("video", "keep-2", "keep-2", 5)
        assert (pos1, pos2) == (1, 2)
        module.store.set_queue_limit(1)
        assert module.TASK_QUEUE.queued_ids() == [one["id"], two["id"]]
        try:
            module._enqueue_task("image", "overflow", "overflow", 5)
            raise AssertionError("queue below the new limit still accepted a job")
        except module.HTTPException as exc:
            assert exc.status_code == 429
        assert module.TASK_QUEUE.queued_ids() == [one["id"], two["id"]]
        print("limit_shrink_keeps_existing=PASS queue_full_429=PASS")

        module.TASK_QUEUE.clear()
        for task in (one, two):
            module.store.update_task(task["id"], status="failed", error="cleared")
        module.store.set_queue_limit(20)
        before = len(started)
        module.DISPATCHER.resume()
        jobs = []
        for name in ("job-1", "job-2", "job-3"):
            task, _ = module._enqueue_task("image", name, name, 5)
            jobs.append(task)
        assert wait_until(lambda: len(started) == before + 2), started
        time.sleep(0.4)
        assert len(started) == before + 2
        assert {item["prompt"] for item in started[before:]} == {"job-1", "job-2"}
        assert module.TASK_QUEUE.queued_ids() == [jobs[2]["id"]]
        snap = module.queue_snapshot()
        assert snap["capacity"] == 2
        assert snap["running_count"] == 2
        assert snap["queued_count"] == 1
        assert snap["queued"][0]["position"] == 1
        assert snap["queued"][0]["id"] == jobs[2]["id"]
        assert "reference_image" not in snap["running"][0]
        next(item["event"] for item in started if item["prompt"] == "job-1").set()
        assert wait_until(lambda: len(started) == before + 3), [item["prompt"] for item in started]
        assert started[-1]["prompt"] == "job-3"
        for item in started[before:]:
            item["event"].set()
        assert wait_until(lambda: all(
            (module.store.get_task(task["id"]) or {}).get("status") == "completed"
            for task in jobs))
        print("two_accounts_run_third_waits=PASS")

        asyncio.run(admin_http(module))

        module.DISPATCHER.pause()
        time.sleep(0.45)
        module.TASK_QUEUE.clear()
        queued, _ = module._enqueue_task("image", "stay", "stay", 5)
        orphan = module.store.create_task("image", "orphan")
        running = module.store.create_task("image", "running")
        module.store.update_task(running["id"], status="processing")
        module._reconcile_queue()
        assert module.store.get_task(queued["id"])["status"] == "queued"
        assert queued["id"] in module.TASK_QUEUE.queued_ids()
        orphan_task = module.store.get_task(orphan["id"])
        assert orphan_task["status"] == "failed"
        assert "队列里已经没有" in orphan_task["error"]
        running_task = module.store.get_task(running["id"])
        assert running_task["status"] == "failed"
        assert "正在执行" in running_task["error"]
        print("restart_reconcile=PASS")
        for item in started:
            item["event"].set()
        module.DISPATCHER.stop()
    print("PASS task queue")


async def admin_http(module):
    headers = {"Authorization": "Bearer test-only"}
    client = Client(module.app, headers)
    page = await client.request("GET", "/")
    assert page.status_code == 200
    html = page.body.decode()
    for marker in ("id=\"pQueue\"", "id=\"qLimit\"", "正在执行", "正在排队", "/admin/queue"):
        assert marker in html, marker
    module.DISPATCHER.pause()
    time.sleep(0.45)
    module.TASK_QUEUE.clear()
    module.store.set_queue_limit(5)
    queued, _ = module._enqueue_task("video", "visible", "visible prompt", 5)
    running = module.store.create_task("image", "running prompt")
    module.store.update_task(
        running["id"], status="processing", progress=40, account=module.store.accounts[0]["id"],
        started_at=int(time.time()))
    snap = await client.request("GET", "/admin/queue")
    assert snap.status_code == 200, snap.body
    body = snap.json()
    assert body["queued_count"] == 1 and body["running_count"] >= 1
    assert body["queued"][0]["id"] == queued["id"]
    assert body["queued"][0]["prompt"] == "visible"
    assert body["queued"][0]["kind"] == "video"
    assert body["redis_ok"] is True
    saved = await client.request("PUT", "/admin/queue", json_data={"limit": 8})
    assert saved.status_code == 200 and saved.json()["limit"] == 8
    assert module.store.queue_limit() == 8
    rejected = await client.request("PUT", "/admin/queue", json_data={"limit": 0})
    assert rejected.status_code == 422
    status = await client.request("GET", "/admin/status")
    assert status.status_code == 200 and status.json()["queue"]["limit"] == 8
    denied = await client.request("DELETE", "/admin/tasks/" + running["id"])
    assert denied.status_code == 409 and "不能删除" in denied.body.decode()
    removed = await client.request("DELETE", "/admin/tasks/" + queued["id"])
    assert removed.status_code == 200
    assert queued["id"] not in module.TASK_QUEUE.queued_ids()
    print("admin_queue_page=PASS")


if __name__ == "__main__":
    main()
