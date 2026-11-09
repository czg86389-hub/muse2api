"""Global system prompt: defaults, chat formatting, settings API and media injection."""
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
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def get(self, path):
        return await self.request("GET", path)

    async def post(self, path, data):
        return await self.request(
            "POST", path, body=json.dumps(data).encode(),
            headers={"content-type": "application/json"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def main():
    with tempfile.TemporaryDirectory(prefix="muse-prompt-") as tmp:
        os.environ.update(MUSE2API_HOME=tmp, MUSE2API_PROFILE_ROOT=tmp,
                          MUSE2API_KEY="prompt-secret")
        spec = importlib.util.spec_from_file_location("app_prompt_candidate", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        assert module.DEFAULT_SYSTEM_PROMPT.startswith("Session isolation\n")
        settings = module.store.get_settings()
        assert settings["system_prompt_enabled"] is False
        assert settings["system_prompt"] == module.DEFAULT_SYSTEM_PROMPT

        msg = module.ChatMessage(role="user", content="hello")
        assert module.build_chat_prompt([msg]) == "<user>\nhello\n</user>"
        assert module.build_chat_prompt([msg], module._active_system_prompt()) == "<user>\nhello\n</user>"
        assert module.build_chat_prompt([], "SESSION RULE") == ""
        assert module._with_system_prompt("PROMPT") == "PROMPT"

        extra = module.build_chat_prompt([msg], "SESSION RULE")
        assert extra.startswith("<system>\nSESSION RULE\n</system>\n\n"), extra
        assert extra.count("<system>") == 1 and extra.count("</system>") == 1, extra
        assert "<user>\nhello\n</user>" in extra and not extra.startswith("hello")

        auth = {"Authorization": "Bearer prompt-secret"}
        async with Client(module.app, {}) as anonymous:
            res = await anonymous.get("/admin/settings")
            assert res.status_code == 401, res.status_code

        async with Client(module.app, auth) as client:
            res = await client.get("/admin/settings")
            assert res.status_code == 200, res.body[:300]
            data = res.json()
            assert data["system_prompt_enabled"] is False
            assert data["default_system_prompt"] == module.DEFAULT_SYSTEM_PROMPT

            res = await client.post("/admin/settings", {
                "system_prompt_enabled": True, "system_prompt": "CUSTOM RULE"})
            assert res.status_code == 200, res.body[:300]
            assert res.json()["system_prompt_enabled"] is True
            assert res.json()["system_prompt"] == "CUSTOM RULE"

            res = await client.get("/admin/settings")
            assert res.json()["system_prompt"] == "CUSTOM RULE"

            assert module._active_system_prompt() == "CUSTOM RULE"
            assert module._with_system_prompt("PROMPT") == "<system>\nCUSTOM RULE\n</system>\n\nPROMPT"
            merged = module._with_system_prompt(
                "<system>\nINSTRUCTION\n</system>\n\n<user>\nhello\n</user>")
            assert merged == "<system>\nCUSTOM RULE\n\nINSTRUCTION\n</system>\n\n<user>\nhello\n</user>", merged
            chat = module.build_chat_prompt([msg], module._active_system_prompt())
            assert chat.startswith("<system>\nCUSTOM RULE\n</system>\n\n"), chat
            assert "<user>\nhello\n</user>" in chat

            # The media path goes through _run_generation -> _run_generation_locked.
            captured = {}
            original = module._run_generation_locked

            def fake_locked(prompt, *args, **kwargs):
                captured["prompt"] = prompt
                return {"ok": True}, None

            module._run_generation_locked = fake_locked
            try:
                module._run_generation("MEDIA PROMPT", "image", 5)
            finally:
                module._run_generation_locked = original
            assert captured["prompt"] == "<system>\nCUSTOM RULE\n</system>\n\nMEDIA PROMPT", captured

            # The chat endpoint must pass the active prompt into safe_chat_stream.
            module.store.add_account({"hatch_sess": "fixture"}, "prompt-test")
            captured_chat = {}
            original_stream = module.safe_chat_stream

            def fake_stream(cookies, prompt, expires, timeout, account_id=None):
                captured_chat["prompt"] = prompt
                return iter(["ok"])

            module.safe_chat_stream = fake_stream
            try:
                res = await client.post("/v1/chat/completions", {
                    "model": "muse-spark",
                    "messages": [{"role": "user", "content": "hello"}],
                })
            finally:
                module.safe_chat_stream = original_stream
            assert res.status_code == 200, res.body[:300]
            assert "CUSTOM RULE" in captured_chat["prompt"], captured_chat
            assert "<user>\nhello\n</user>" in captured_chat["prompt"], captured_chat

            # Top-level prompt fallback must still receive the active system prompt.
            module.safe_chat_stream = fake_stream
            try:
                res = await client.post("/v1/chat/completions", {
                    "model": "muse-spark", "messages": [], "prompt": "FALLBACK"})
            finally:
                module.safe_chat_stream = original_stream
            assert res.status_code == 200, res.body[:300]
            assert "CUSTOM RULE" in captured_chat["prompt"], captured_chat
            assert "FALLBACK" in captured_chat["prompt"], captured_chat

            # A fresh Store must see the persisted settings file.
            fresh = type(module.store)(module.CFG)
            assert fresh.get_settings()["system_prompt"] == "CUSTOM RULE"
            assert os.path.isfile(module.CFG.settings_file)

            res = await client.post("/admin/settings", {
                "system_prompt_enabled": True, "system_prompt": "   "})
            assert res.status_code == 400, res.body[:300]

            res = await client.post("/admin/settings", {
                "system_prompt_enabled": True, "system_prompt": "x" * 20001})
            assert res.status_code == 400, res.body[:300]

            res = await client.post("/admin/settings", {
                "system_prompt_enabled": False, "system_prompt": ""})
            assert res.status_code == 200, res.body[:300]
            assert module._active_system_prompt() == ""
            assert module.build_chat_prompt([msg], module._active_system_prompt()) == "<user>\nhello\n</user>"

    print("PASS: system prompt defaults, chat formatting, settings API and media injection")


if __name__ == "__main__":
    asyncio.run(main())
