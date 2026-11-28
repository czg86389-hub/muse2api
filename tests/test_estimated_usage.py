"""Opt-in estimated usage (MUSE2API_ESTIMATE_USAGE=1); offline, no live account.

Run: python tests/test_estimated_usage.py
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
HOME = tempfile.TemporaryDirectory(prefix="muse-usage-test-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="usage-test", MUSE2API_AUTO_UPDATE="0")
import app  # noqa: E402
from fastapi import Response  # noqa: E402

PROMPT = "Say hello"
REPLY = ["Hello ", "world, ", "你好"]


def fake_stream(*args, **kwargs):
    yield from REPLY


async def asgi_post(path: str, payload: dict):
    """One HTTP request through the real ASGI app (auth, injection, serialization)."""
    body = json.dumps(payload).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 18610),
             "headers": [(b"authorization", b"Bearer usage-test"), (b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())]}
    sent, out = [False], {"status": 0, "headers": {}, "body": b""}

    async def receive():
        if sent[0]:
            return {"type": "http.disconnect"}
        sent[0] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await app.app(scope, receive, send)
    return out


async def drain(streaming) -> list[str]:
    out = []
    async for chunk in streaming.body_iterator:
        out.append(chunk if isinstance(chunk, str) else chunk.decode())
    return out


class EstimatedUsageTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts = []
        app.store.tasks = {}
        app.store.add_account({"hatch_sess": "x"}, "a")
        self.patches = [patch.object(app, "safe_chat_stream", side_effect=fake_stream),
                        patch.object(app.CFG, "estimate_usage", False)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def chat(self, stream=False):
        req = app.ChatRequest(model="muse-spark", messages=[{"role": "user", "content": PROMPT}],
                              stream=stream, stream_options={"include_usage": True} if stream else None)
        resp = Response()
        return asyncio.run(app.chat_completions(req, resp)), resp

    def test_estimate_tokens(self):
        self.assertEqual(app.estimate_tokens(""), 0)
        self.assertEqual(app.estimate_tokens(None), 0)
        self.assertEqual(app.estimate_tokens("abcd"), 1)
        self.assertEqual(app.estimate_tokens("abcde"), 2)
        self.assertEqual(app.estimate_tokens("你好"), 2)
        self.assertEqual(app.estimate_tokens("hi 你好"), 3)   # "hi " -> 1, two CJK -> 2

    def test_default_keeps_zero_usage_and_no_header(self):
        body, resp = self.chat()
        self.assertEqual(body["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        self.assertNotIn("x-muse2api-usage", resp.headers)

    def test_opt_in_estimates_non_stream(self):
        app.CFG.estimate_usage = True
        body, resp = self.chat()
        u = body["usage"]
        self.assertEqual(body["choices"][0]["message"]["content"], "".join(REPLY))
        self.assertEqual(u["completion_tokens"], app.estimate_tokens("".join(REPLY)))
        self.assertGreater(u["prompt_tokens"], 0)
        self.assertEqual(u["total_tokens"], u["prompt_tokens"] + u["completion_tokens"])
        self.assertEqual(resp.headers.get("x-muse2api-usage"), "estimated")

    def test_opt_in_estimates_stream_usage_chunk(self):
        app.CFG.estimate_usage = True
        streaming, _ = self.chat(stream=True)
        self.assertEqual(streaming.headers.get("x-muse2api-usage"), "estimated")
        chunks = asyncio.run(drain(streaming))
        usage = [json.loads(c[6:])["usage"] for c in chunks
                 if c.startswith("data: {") and '"usage"' in c]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["completion_tokens"], app.estimate_tokens("".join(REPLY)))
        self.assertTrue(chunks[-1].startswith("data: [DONE]"))

    def test_stream_default_usage_chunk_is_zero(self):
        streaming, _ = self.chat(stream=True)
        self.assertNotIn("x-muse2api-usage", streaming.headers)
        chunks = asyncio.run(drain(streaming))
        usage = [json.loads(c[6:])["usage"] for c in chunks if c.startswith("data: {") and '"usage"' in c]
        self.assertEqual(usage, [{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}])

    def test_http_round_trip_sets_header(self):
        payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": PROMPT}]}
        res = asyncio.run(asgi_post("/v1/chat/completions", payload))
        self.assertEqual(res["status"], 200)
        self.assertNotIn("x-muse2api-usage", res["headers"])
        self.assertEqual(json.loads(res["body"])["usage"]["total_tokens"], 0)
        app.CFG.estimate_usage = True
        res = asyncio.run(asgi_post("/v1/chat/completions", payload))
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["headers"].get("x-muse2api-usage"), "estimated")
        self.assertGreater(json.loads(res["body"])["usage"]["total_tokens"], 0)

    def test_responses_api(self):
        req = app.ResponsesRequest(model="muse-spark", input=PROMPT)
        body = asyncio.run(app.responses_api(req, Response()))
        self.assertEqual(body["usage"], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        app.CFG.estimate_usage = True
        resp = Response()
        body = asyncio.run(app.responses_api(req, resp))
        self.assertEqual(body["usage"]["output_tokens"], app.estimate_tokens("".join(REPLY)))
        self.assertEqual(body["usage"]["total_tokens"],
                         body["usage"]["input_tokens"] + body["usage"]["output_tokens"])
        self.assertEqual(resp.headers.get("x-muse2api-usage"), "estimated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
