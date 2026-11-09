"""Admin media library recognises audio files. No browser or network."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

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

    async def request(self, method, path):
        hdr = {**self.headers, "content-length": "0"}
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
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def get(self, path):
        return await self.request("GET", path)

    async def delete(self, path):
        return await self.request("DELETE", path)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-media-audio-") as tmp:
        os.environ.update(MUSE2API_HOME=tmp, MUSE2API_PROFILE_ROOT=tmp,
                          MUSE2API_KEY="test-secret")
        spec = importlib.util.spec_from_file_location("app_media_audio_candidate", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        media = module.CFG.media_dir
        os.makedirs(media, exist_ok=True)
        for name in ("voice.mp3", "clip.mp4", "pic.webp"):
            with open(os.path.join(media, name), "wb") as f:
                f.write(b"fixture")

        async with Client(module.app, {"Authorization": "Bearer test-secret"}) as client:
            res = await client.get("/admin/media")
            assert res.status_code == 200
            kinds = {m["name"]: m["kind"] for m in res.json()["media"]}
            assert kinds == {"voice.mp3": "audio", "clip.mp4": "video", "pic.webp": "image"}, kinds

            models = (await client.get("/v1/models")).json()["data"]
            assert any(m["id"] == "muse-voice" for m in models)

            res = await client.delete("/admin/media/voice.mp3")
            assert res.status_code == 200
            assert not os.path.exists(os.path.join(media, "voice.mp3"))

    print("PASS: admin media audio kind and muse-voice model listing")


if __name__ == "__main__":
    asyncio.run(main())
