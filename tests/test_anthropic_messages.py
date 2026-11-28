"""Offline unit tests for Anthropic /v1/messages endpoint; no live account or network.

Run: python tests/test_anthropic_messages.py
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-anthropic-test-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="anthropic-test-key", MUSE2API_AUTO_UPDATE="0")
import app  # noqa: E402

PROMPT = "Say hello"
REPLY = ["Hello ", "world, ", "你好"]


def parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    events = []
    for block in raw.decode("utf-8").strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
    return events


async def asgi_post(path: str, payload: dict, headers: dict[str, str] | None = None):
    """Send one HTTP POST through the real ASGI app (auth, validation, serialization)."""
    body = json.dumps(payload).encode("utf-8")
    req_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for k, v in (headers or {}).items():
        req_headers.append((k.lower().encode("ascii"), v.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 18610),
        "headers": req_headers,
    }
    sent, out = [False], {"status": 0, "headers": {}, "body": b""}
    done = asyncio.Event()

    async def receive():
        if sent[0]:
            await done.wait()
            return {"type": "http.disconnect"}
        sent[0] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
            if not msg.get("more_body", False):
                done.set()

    await app.app(scope, receive, send)
    return out


class AnthropicMessagesTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts = []
        app.store.tasks = {}
        app.store.add_account({"hatch_sess": "x"}, "a")
        self.captured_prompts: list[str] = []

        def fake_stream(cookies, prompt, expires, timeout, account_id=None):
            self.captured_prompts.append(prompt)
            yield from REPLY

        self.patches = [
            patch.object(app, "safe_chat_stream", side_effect=fake_stream),
            patch.object(app.CFG, "estimate_usage", False),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def _payload(self, stream: bool = False, **extra) -> dict:
        base = {
            "model": "claude-sonnet-4",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": PROMPT}],
            "stream": stream,
        }
        base.update(extra)
        return base

    # ---- Auth tests ----

    def test_auth_missing_and_invalid_rejected(self):
        res_missing = asyncio.run(asgi_post("/v1/messages", self._payload()))
        self.assertEqual(res_missing["status"], 401)

        res_bad = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "wrong"}))
        self.assertEqual(res_bad["status"], 401)

        res_bad_bearer = asyncio.run(
            asgi_post("/v1/messages", self._payload(), {"Authorization": "Bearer wrong"})
        )
        self.assertEqual(res_bad_bearer["status"], 401)

        res_bad_key_with_valid_bearer = asyncio.run(
            asgi_post(
                "/v1/messages",
                self._payload(),
                {"x-api-key": "wrong", "Authorization": "Bearer anthropic-test-key"},
            )
        )
        self.assertEqual(res_bad_key_with_valid_bearer["status"], 401)

    def test_auth_accepts_x_api_key_and_bearer(self):
        res_key = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res_key["status"], 200)

        res_bearer = asyncio.run(
            asgi_post("/v1/messages", self._payload(), {"Authorization": "Bearer anthropic-test-key"})
        )
        self.assertEqual(res_bearer["status"], 200)

        res_blank_key_fallback = asyncio.run(
            asgi_post(
                "/v1/messages",
                self._payload(),
                {"x-api-key": "   ", "Authorization": "Bearer anthropic-test-key"},
            )
        )
        self.assertEqual(res_blank_key_fallback["status"], 200)

    def test_auth_bypassed_when_api_key_unset(self):
        with patch.object(app.CFG, "api_key", ""):
            res = asyncio.run(asgi_post("/v1/messages", self._payload()))
        self.assertEqual(res["status"], 200)

    # ---- Non-streaming tests ----

    def test_non_stream_default_zero_usage_and_no_header(self):
        res = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        self.assertNotIn("x-muse2api-usage", res["headers"])
        body = json.loads(res["body"])
        self.assertTrue(body["id"].startswith("msg_"))
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["role"], "assistant")
        self.assertEqual(body["model"], "muse-spark")
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(body["content"], [{"type": "text", "text": "".join(REPLY)}])
        self.assertEqual(body["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_non_stream_opt_in_estimated_usage(self):
        app.CFG.estimate_usage = True
        res = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["headers"].get("x-muse2api-usage"), "estimated")
        body = json.loads(res["body"])
        self.assertEqual(body["usage"]["input_tokens"], app.estimate_tokens(self.captured_prompts[0]))
        self.assertEqual(body["usage"]["output_tokens"], app.estimate_tokens("".join(REPLY)))

    def test_system_and_history_blocks_flattened_to_plain_text(self):
        payload = self._payload(
            system=[{"type": "text", "text": "Be concise."}],
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "Check weather"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tu_1", "name": "weather", "input": {"city": "NYC"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "Sunny 22C"}],
                },
            ],
            tools=[{"name": "weather", "description": "Get weather", "input_schema": {"type": "object"}}],
        )
        res = asyncio.run(asgi_post("/v1/messages", payload, {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        prompt = self.captured_prompts[0]
        self.assertIn("Be concise.", prompt)
        self.assertIn("【调用工具 weather】", prompt)
        self.assertIn("【工具结果】", prompt)
        self.assertIn("Sunny 22C", prompt)
        body = json.loads(res["body"])
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(body["content"][0]["type"], "text")

    def test_system_string_list_tool_result_and_null_fields(self):
        payload = self._payload(
            system="Reply in Chinese.",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": None},
                        {"type": "text", "text": "Hi"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_2",
                            "content": [{"type": "text", "text": None}, {"type": "text", "text": "part1"}],
                        },
                        {"type": "tool_result", "tool_use_id": "tu_3", "content": None},
                    ],
                }
            ],
        )
        res = asyncio.run(asgi_post("/v1/messages", payload, {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        prompt = self.captured_prompts[0]
        self.assertIn("Reply in Chinese.", prompt)
        self.assertIn("part1", prompt)
        self.assertNotIn("None", prompt)

    def test_pick_account_uses_pure_rotation(self):
        with patch.object(app.store, "pick_account", wraps=app.store.pick_account) as mock_pick:
            res = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        mock_pick.assert_called_once_with(rotate=True)

    # ---- Streaming tests ----

    def test_stream_default_zero_usage_and_sse_sequence(self):
        res = asyncio.run(asgi_post("/v1/messages", self._payload(stream=True), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        self.assertIn("text/event-stream", res["headers"].get("content-type", ""))
        self.assertNotIn("x-muse2api-usage", res["headers"])
        events = parse_sse(res["body"])
        names = [name for name, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[1], "content_block_start")
        self.assertTrue(len(names[2:-3]) >= 1 and all(n == "content_block_delta" for n in names[2:-3]))
        self.assertEqual(names[-3:], ["content_block_stop", "message_delta", "message_stop"])

        start_msg = events[0][1]["message"]
        self.assertEqual(start_msg["usage"], {"input_tokens": 0, "output_tokens": 0})
        text = "".join(data["delta"]["text"] for name, data in events if name == "content_block_delta")
        self.assertEqual(text, "".join(REPLY))
        delta_ev = events[-2][1]
        self.assertEqual(delta_ev["delta"]["stop_reason"], "end_turn")
        self.assertEqual(delta_ev["usage"], {"output_tokens": 0})

    def test_stream_opt_in_estimated_usage(self):
        app.CFG.estimate_usage = True
        res = asyncio.run(asgi_post("/v1/messages", self._payload(stream=True), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["headers"].get("x-muse2api-usage"), "estimated")
        events = parse_sse(res["body"])
        start_usage = events[0][1]["message"]["usage"]
        self.assertEqual(start_usage, {
            "input_tokens": app.estimate_tokens(self.captured_prompts[0]),
            "output_tokens": 0,
        })
        delta_usage = events[-2][1]["usage"]
        self.assertEqual(delta_usage, {"output_tokens": app.estimate_tokens("".join(REPLY))})

    # ---- Error handling tests ----

    def test_empty_messages_returns_400(self):
        res = asyncio.run(
            asgi_post("/v1/messages", self._payload(messages=[]), {"x-api-key": "anthropic-test-key"})
        )
        self.assertEqual(res["status"], 400)
        self.assertIn("messages 为空", json.loads(res["body"])["error"]["message"])

        res_with_sys = asyncio.run(
            asgi_post(
                "/v1/messages",
                self._payload(system="System only", messages=[]),
                {"x-api-key": "anthropic-test-key"},
            )
        )
        self.assertEqual(res_with_sys["status"], 400)
        self.assertIn("messages 为空", json.loads(res_with_sys["body"])["error"]["message"])

    def test_no_available_account_returns_400(self):
        app.store.accounts = []
        res = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 400)
        self.assertIn("没有可用账号", json.loads(res["body"])["error"]["message"])

    def test_non_stream_upstream_errors(self):
        with patch.object(app, "safe_chat_stream", side_effect=app.MuseAuthError("cookie expired")):
            res = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 401)
        self.assertIn("cookie expired", json.loads(res["body"])["error"]["message"])

        with patch.object(app, "safe_chat_stream", side_effect=app.MuseGenerationError("upstream 503")):
            res = asyncio.run(asgi_post("/v1/messages", self._payload(), {"x-api-key": "anthropic-test-key"}))
        self.assertEqual(res["status"], 502)
        self.assertIn("upstream 503", json.loads(res["body"])["error"]["message"])

    def test_stream_upstream_errors_emit_sse_error_event(self):
        with patch.object(app, "safe_chat_stream", side_effect=app.MuseAuthError("session invalid")):
            res = asyncio.run(
                asgi_post("/v1/messages", self._payload(stream=True), {"x-api-key": "anthropic-test-key"})
            )
        self.assertEqual(res["status"], 200)
        events = parse_sse(res["body"])
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"]["type"], "authentication_error")
        self.assertIn("session invalid", events[-1][1]["error"]["message"])

        with patch.object(app, "safe_chat_stream", side_effect=app.MuseGenerationError("overloaded")):
            res = asyncio.run(
                asgi_post("/v1/messages", self._payload(stream=True), {"x-api-key": "anthropic-test-key"})
            )
        events = parse_sse(res["body"])
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"]["type"], "api_error")
        self.assertIn("overloaded", events[-1][1]["error"]["message"])

        with patch.object(app, "safe_chat_stream", side_effect=RuntimeError("boom")):
            res = asyncio.run(
                asgi_post("/v1/messages", self._payload(stream=True), {"x-api-key": "anthropic-test-key"})
            )
        events = parse_sse(res["body"])
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"]["type"], "api_error")
        self.assertIn("boom", events[-1][1]["error"]["message"])

        def partial_then_fail(*args, **kwargs):
            yield "part1"
            raise app.MuseGenerationError("mid-stream failure")

        with patch.object(app, "safe_chat_stream", side_effect=partial_then_fail):
            res = asyncio.run(
                asgi_post("/v1/messages", self._payload(stream=True), {"x-api-key": "anthropic-test-key"})
            )
        events = parse_sse(res["body"])
        names = [name for name, _ in events]
        self.assertIn("content_block_delta", names)
        self.assertNotIn("message_stop", names)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"]["type"], "api_error")
        self.assertIn("mid-stream failure", events[-1][1]["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
