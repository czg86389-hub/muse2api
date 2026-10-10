"""Test admin media deletion endpoints without live network or browser."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "app.py"


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
            "server": ("test", 80), "client": ("test", 1)
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
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, json_data=None, **kwargs):
        body = json.dumps(json_data).encode() if json_data is not None else b""
        headers = {"content-type": "application/json"}
        return await self.request("POST", path, body=body, headers=headers, **kwargs)

    async def delete(self, path, **kwargs):
        return await self.request("DELETE", path, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-media-del-test-") as tmp:
        os.environ["MUSE2API_HOME"] = tmp
        os.environ["MUSE2API_KEY"] = "test-secret"
        os.environ["REDIS_URL"] = "memory://"
        os.environ["MUSE2API_REDIS_URL"] = "memory://"
        media_dir = os.path.join(tmp, "data", "media")
        os.makedirs(media_dir, exist_ok=True)

        spec = importlib.util.spec_from_file_location("app_candidate", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Create sample files
        f1 = os.path.join(media_dir, "sample1.webp")
        f2 = os.path.join(media_dir, "sample2.mp4")
        with open(f1, "w") as f:
            f.write("content1")
        with open(f2, "w") as f:
            f.write("content2")

        async with Client(module.app, {"Authorization": "Bearer test-secret"}) as client:
            # 1. List media
            res = await client.get("/admin/media")
            assert res.status_code == 200, f"GET /admin/media failed: {res.status_code}"
            data = res.json()
            names = [m["name"] for m in data.get("media", [])]
            assert "sample1.webp" in names and "sample2.mp4" in names

            # 2. Delete single file via POST /admin/media/delete
            res = await client.post("/admin/media/delete", json_data={"names": ["sample1.webp"]})
            assert res.status_code == 200
            assert res.json().get("removed") == 1
            assert not os.path.exists(f1)

            # 3. Path traversal attack blocked
            res = await client.post("/admin/media/delete", json_data={"names": ["../app.py", "..\\app.py"]})
            assert res.status_code == 200
            assert res.json().get("removed") == 0

            # 4. Delete via DELETE /admin/media/{name}
            res = await client.delete("/admin/media/sample2.mp4")
            assert res.status_code == 200
            assert res.json().get("status") == "ok"
            assert not os.path.exists(f2)

            # 5. Non-existent returns 404
            res = await client.delete("/admin/media/nonexistent.webp")
            assert res.status_code == 404

    print("PASS: all media deletion tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
