"""Offline /v1/audio/speech contract checks. No account, network or Chromium."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x0f" + b"\xff\xfb\x90\x00" * 8


class Response:
    def __init__(self, status, body, headers):
        self.status_code, self.body, self.headers = status, body, headers

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
        start = next(m for m in messages if m["type"] == "http.response.start")
        headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
        return Response(start["status"], b"".join(m.get("body", b"") for m in messages), headers)

    async def post(self, path, json_data=None, headers=None):
        body = json.dumps(json_data).encode() if json_data is not None else b""
        return await self.request("POST", path, body=body,
                                  headers={"content-type": "application/json", **(headers or {})})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-audio-api-") as tmp:
        os.environ.update(MUSE2API_HOME=tmp, MUSE2API_PROFILE_ROOT=tmp,
                          MUSE2API_KEY="test-secret", MUSE2API_AUDIO_TIMEOUT="45")
        spec = importlib.util.spec_from_file_location("app_audio_candidate", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        media_dir = module.CFG.media_dir
        os.makedirs(media_dir, exist_ok=True)
        native = os.path.join(media_dir, "native.mp3")
        with open(native, "wb") as f:
            f.write(MP3)

        calls = []

        def fake_run(prompt, kind, timeout, **kwargs):
            calls.append({"prompt": prompt, "kind": kind, "timeout": timeout})
            return ({"path": native, "filename": "native.mp3", "size": len(MP3),
                     "ext": ".mp3", "mime": "audio/mpeg", "kind": "audio"}, "acc-test")

        module._run_generation = fake_run
        auth = {"Authorization": "Bearer test-secret"}

        # Prompt construction keeps the text, voice and speed explicit.
        req = module.AudioSpeechRequest(input="Xin chào", voice="giọng nam trầm",
                                        response_format="mp3", speed=1.25)
        prompt = module.build_audio_prompt(req)
        assert "Xin chào" in prompt and "giọng nam trầm" in prompt and "1.25" in prompt
        assert 'Voice: choose the voice corresponding to "giọng nam trầm"' in prompt
        assert "return only the audio attachment" in prompt
        assert module.build_audio_prompt(
            module.AudioSpeechRequest(input="hi", voice="alloy")).find("alloy") < 0
        unnamed = module.build_audio_prompt(module.AudioSpeechRequest(input="hi"))
        assert "choose the most suitable voice" in unnamed and "female" not in unnamed

        async with Client(module.app, {}) as no_auth:
            res = await no_auth.post("/v1/audio/speech", {"input": "hi"})
            assert res.status_code == 401, res.status_code

        async with Client(module.app, auth) as client:
            res = await client.post("/v1/audio/speech", {"input": "  "})
            assert res.status_code == 400, (res.status_code, res.body)

            res = await client.post("/v1/audio/speech",
                                    {"input": "hi", "response_format": "exe"})
            assert res.status_code == 400, (res.status_code, res.body)

            res = await client.post("/v1/audio/speech", {
                "model": "tts-1", "input": "Xin chào", "voice": "giọng nữ",
                "response_format": "mp3", "speed": 1.0, "timeout": 30})
            assert res.status_code == 200, (res.status_code, res.body[:200])
            assert res.body == MP3
            assert res.headers.get("content-type", "").startswith("audio/mpeg")
            assert res.headers.get("x-muse-audio-format") == "mp3"
            assert res.headers.get("x-muse-media-name") == "native.mp3"
            assert "speech-" in res.headers.get("content-disposition", "")
            assert calls[-1]["kind"] == "audio" and calls[-1]["timeout"] == 30
            assert "Xin chào" in calls[-1]["prompt"]

            # Without ffmpeg the native format is returned instead of a fake conversion.
            original_which = module.shutil.which
            module.shutil.which = lambda name: None
            try:
                res = await client.post("/v1/audio/speech",
                                        {"input": "hi", "response_format": "wav"})
            finally:
                module.shutil.which = original_which
            assert res.status_code == 200, (res.status_code, res.body[:200])
            assert res.body == MP3
            assert res.headers.get("x-muse-audio-format") == "mp3"

            # 同步返回的语音同样写入任务记录（admin「生成任务记录」）。
            audio_tasks = [t for t in module.store.list_tasks(20)
                           if t.get("kind") == "audio"]
            assert len(audio_tasks) == 2, audio_tasks
            by_prompt = {t["prompt"]: t for t in audio_tasks}
            done = by_prompt["Xin chào"]
            assert done["status"] == "completed" and done["progress"] == 100
            assert done["account"] == "acc-test"
            assert "Xin chào" in done["api_prompt"]
            assert done["result"]["kind"] == "audio"
            assert done["result"]["url"].endswith("/v1/media/native.mp3")
            assert done["result"]["bytes"] == len(MP3)
            assert isinstance(done["elapsed"], (int, float))

            # 生成失败时记录 failed 与错误原因，而不是静默丢弃。
            def failing_run(prompt, kind, timeout, **kwargs):
                raise module.MuseGenerationError("upstream boom")

            module._run_generation = failing_run
            try:
                res = await client.post("/v1/audio/speech", {"input": "sẽ lỗi"})
            finally:
                module._run_generation = fake_run
            assert res.status_code == 502, (res.status_code, res.body[:200])
            failed = [t for t in module.store.list_tasks(20)
                      if t.get("kind") == "audio" and t["prompt"] == "sẽ lỗi"]
            assert failed and failed[0]["status"] == "failed"
            assert "upstream boom" in failed[0]["error"]

        models = module.MODELS
        assert any(m["id"] == "muse-voice" for m in models)
        assert module.resolve_model("tts-1") == "muse-voice"
        assert module.resolve_model("gpt-4o-mini-tts") == "muse-voice"

    print("PASS: audio prompt, auth, validation, binary response, format fallback and task records")


if __name__ == "__main__":
    asyncio.run(main())
