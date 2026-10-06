"""muse2api — reverse-proxies chat/image/video quotas of free Muse (muse.ai) web accounts into APIs.

OpenAI-compatible:
  POST /v1/chat/completions          (text/code chat, streaming supported, Codex-compatible)
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

Admin (account-pool admin UI at GET /):
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            (single / batch text / batch array)
  PATCH  /admin/accounts/{id}       (edit label, enable/disable)
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  (actually opens muse.ai to verify the session)
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               (task records)
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
1: GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           (browser extension zip, for grabbing cookies)
  GET    /admin/cookie-helper       (CLI cookie-fetch script, advanced)
"""
from __future__ import annotations

from typing import Any

import asyncio
import base64
import io
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field

from config import CFG
from engine import ESSENTIAL_COOKIES, MuseAuthError, MuseEngine, MuseGenerationError, _is_elevated
from store import Store, account_expiry, min_expiry
from jev_dispatch import jev_tool_dispatch

import sys
log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

CFG.ensure_dirs()
app = FastAPI(title="muse2api", version="1.6.1")

# The cookie helper script submits import requests from muse.ai pages, so that origin must be allowed;
# browser extensions posting from chrome-extension:// are allowed too.
#
# All origins are allowed here: this service authenticates with a Bearer key, not cookies,
# so allowing origins adds no privilege-escalation risk; restricting origins would instead
# break browser-based agents (Open WebUI / LobeChat / web clients) via CORS.
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

store = Store(CFG)
engine = MuseEngine(CFG)
GEN_LOCK = threading.Lock()
IMAGE_TASK_LOCK = threading.Lock()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ------------------------- OpenAI-style error responses -------------------------
# Most agents read the error message from OpenAI-style {"error": {"message": ...}};
# FastAPI returns {"detail": ...} by default, which clients cannot read and show as "unknown error".
# So /v1/* is normalized to the OpenAI shape; admin APIs stay as-is (the frontend relies on detail).
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    path = request.url.path
    if path.startswith("/v1/messages") or path.startswith("/messages"):
        err_type = (
            "authentication_error" if exc.status_code == 401
            else "not_found_error" if exc.status_code == 404
            else "invalid_request_error" if exc.status_code == 400
            else "api_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": "error", "error": {"type": err_type, "message": str(exc.detail)}},
            headers=getattr(exc, "headers", None),
        )
    if path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    path = request.url.path
    if path.startswith("/v1/messages") or path.startswith("/messages"):
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "Request validation failed: " + str(exc.errors())[:400]
            }},
        )
    if path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "Request validation failed: " + str(exc.errors())[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

MODELS = [
    {"id": "muse-spark", "object": "model", "type": "model", "display_name": "Muse Spark", "owned_by": "muse",
     "description": "Muse Spark -- text/code chat (free web quota, streaming supported)"},
    {"id": "muse-image", "object": "model", "type": "model", "display_name": "Muse Image", "owned_by": "muse",
     "description": "Muse Image -- text-to-image / image editing (free web quota)"},
    {"id": "muse-video", "object": "model", "type": "model", "display_name": "Muse Video", "owned_by": "muse",
     "description": "Muse Video -- text-to-video / image-to-video (free web quota)"},
    # Anthropic current & flagship models (official platform catalog)
    {"id": "claude-opus-5-5", "object": "model", "type": "model", "display_name": "Claude Opus 5.5", "owned_by": "anthropic",
     "description": "Claude Opus 5.5 -- flagship for agentic coding and knowledge work (mapped to Muse Spark)"},
    {"id": "claude-sonnet-5-5", "object": "model", "type": "model", "display_name": "Claude Sonnet 5.5", "owned_by": "anthropic",
     "description": "Claude Sonnet 5.5 -- speed and intelligence balance (mapped to Muse Spark)"},
    {"id": "claude-fable-5-1", "object": "model", "type": "model", "display_name": "Claude Fable 5.1", "owned_by": "anthropic",
     "description": "Claude Fable 5.1 -- demanding reasoning and long-horizon agentic work (mapped to Muse Spark)"},
    {"id": "claude-haiku-4-5", "object": "model", "type": "model", "display_name": "Claude Haiku 4.5", "owned_by": "anthropic",
     "description": "Claude Haiku 4.5 -- fast near-frontier intelligence (mapped to Muse Spark)"},
    {"id": "claude-opus-5", "object": "model", "type": "model", "display_name": "Claude Opus 5", "owned_by": "anthropic",
     "description": "Claude Opus 5 (mapped to Muse Spark)"},
    {"id": "claude-sonnet-5", "object": "model", "type": "model", "display_name": "Claude Sonnet 5", "owned_by": "anthropic",
     "description": "Claude Sonnet 5 (mapped to Muse Spark)"},
    {"id": "claude-opus-4-8", "object": "model", "type": "model", "display_name": "Claude Opus 4.8", "owned_by": "anthropic",
     "description": "Claude Opus 4.8 (mapped to Muse Spark)"},
    {"id": "claude-opus-4-7", "object": "model", "type": "model", "display_name": "Claude Opus 4.7", "owned_by": "anthropic",
     "description": "Claude Opus 4.7 (mapped to Muse Spark)"},
    {"id": "claude-opus-4-6", "object": "model", "type": "model", "display_name": "Claude Opus 4.6", "owned_by": "anthropic",
     "description": "Claude Opus 4.6 (mapped to Muse Spark)"},
    {"id": "claude-sonnet-4-6", "object": "model", "type": "model", "display_name": "Claude Sonnet 4.6", "owned_by": "anthropic",
     "description": "Claude Sonnet 4.6 (mapped to Muse Spark)"},
    {"id": "claude-3-7-sonnet", "object": "model", "type": "model", "display_name": "Claude 3.7 Sonnet", "owned_by": "anthropic",
     "description": "Claude 3.7 Sonnet (mapped to Muse Spark)"},
    {"id": "claude-3-5-sonnet", "object": "model", "type": "model", "display_name": "Claude 3.5 Sonnet", "owned_by": "anthropic",
     "description": "Claude 3.5 Sonnet (mapped to Muse Spark)"},
    # OpenAI & Codex current & flagship models (official platform catalog)
    {"id": "gpt-6-astra", "object": "model", "type": "model", "display_name": "GPT-6 Astra", "owned_by": "openai",
     "description": "GPT-6 Astra -- flagship for complex reasoning and coding (mapped to Muse Spark)"},
    {"id": "gpt-6.1-sol", "object": "model", "type": "model", "display_name": "GPT-6.1 Sol", "owned_by": "openai",
     "description": "GPT-6.1 Sol -- near-Astra performance for complex work (mapped to Muse Spark)"},
    {"id": "gpt-6-sol", "object": "model", "type": "model", "display_name": "GPT-6 Sol", "owned_by": "openai",
     "description": "GPT-6 Sol -- complex coding and agentic workflows (mapped to Muse Spark)"},
    {"id": "gpt-6-luna", "object": "model", "type": "model", "display_name": "GPT-6 Luna", "owned_by": "openai",
     "description": "GPT-6 Luna -- high-volume efficient workloads (mapped to Muse Spark)"},
    {"id": "gpt-5.4", "object": "model", "type": "model", "display_name": "GPT-5.4", "owned_by": "openai",
     "description": "GPT-5.4 -- coding and professional work (mapped to Muse Spark)"},
    {"id": "gpt-5.4-mini", "object": "model", "type": "model", "display_name": "GPT-5.4 Mini", "owned_by": "openai",
     "description": "GPT-5.4 Mini -- coding and subagents (mapped to Muse Spark)"},
    {"id": "gpt-5.3-codex", "object": "model", "type": "model", "display_name": "GPT-5.3 Codex", "owned_by": "openai",
     "description": "GPT-5.3 Codex -- agentic coding model in Codex (mapped to Muse Spark)"},
    {"id": "gpt-5.2-codex", "object": "model", "type": "model", "display_name": "GPT-5.2 Codex", "owned_by": "openai",
     "description": "GPT-5.2 Codex -- long-horizon agentic coding (mapped to Muse Spark)"},
    {"id": "gpt-5.1-codex", "object": "model", "type": "model", "display_name": "GPT-5.1 Codex", "owned_by": "openai",
     "description": "GPT-5.1 Codex (mapped to Muse Spark)"},
    {"id": "codex-mini-latest", "object": "model", "type": "model", "display_name": "Codex Mini Latest", "owned_by": "openai",
     "description": "Codex Mini Latest (mapped to Muse Spark)"},
    {"id": "gpt-5.2", "object": "model", "type": "model", "display_name": "GPT-5.2", "owned_by": "openai",
     "description": "GPT-5.2 -- professional reasoning work (mapped to Muse Spark)"},
    {"id": "gpt-5.1", "object": "model", "type": "model", "display_name": "GPT-5.1", "owned_by": "openai",
     "description": "GPT-5.1 -- coding and agentic tasks (mapped to Muse Spark)"},
    {"id": "gpt-5", "object": "model", "type": "model", "display_name": "GPT-5", "owned_by": "openai",
     "description": "GPT-5 (mapped to Muse Spark)"},
    {"id": "gpt-4.1", "object": "model", "type": "model", "display_name": "GPT-4.1", "owned_by": "openai",
     "description": "GPT-4.1 (mapped to Muse Spark)"},
    {"id": "gpt-4.1-mini", "object": "model", "type": "model", "display_name": "GPT-4.1 Mini", "owned_by": "openai",
     "description": "GPT-4.1 Mini (mapped to Muse Spark)"},
    {"id": "gpt-4o", "object": "model", "type": "model", "display_name": "GPT-4o", "owned_by": "openai",
     "description": "GPT-4o (mapped to Muse Spark)"},
    {"id": "gpt-4o-mini", "object": "model", "type": "model", "display_name": "GPT-4o Mini", "owned_by": "openai",
     "description": "GPT-4o Mini (mapped to Muse Spark)"},
    {"id": "o4-mini", "object": "model", "type": "model", "display_name": "o4 Mini", "owned_by": "openai",
     "description": "o4 Mini (reasoning model, mapped to Muse Spark)"},
    {"id": "o3", "object": "model", "type": "model", "display_name": "o3", "owned_by": "openai",
     "description": "o3 (reasoning model, mapped to Muse Spark)"},
    {"id": "o3-mini", "object": "model", "type": "model", "display_name": "o3 Mini", "owned_by": "openai",
     "description": "o3 Mini (mapped to Muse Spark)"},
    {"id": "o1", "object": "model", "type": "model", "display_name": "o1", "owned_by": "openai",
     "description": "o1 (mapped to Muse Spark)"},
    {"id": "dall-e-3", "object": "model", "type": "model", "display_name": "DALL-E 3", "owned_by": "openai",
     "description": "DALL-E 3 (mapped to Muse Image)"},
    {"id": "gpt-image-2.5-sunburst", "object": "model", "type": "model", "display_name": "GPT-Image 2.5 Sunburst", "owned_by": "openai",
     "description": "GPT-Image 2.5 Sunburst (mapped to Muse Image)"},
    {"id": "sora", "object": "model", "type": "model", "display_name": "Sora", "owned_by": "openai",
     "description": "Sora (mapped to Muse Video)"},
]

# Downstream clients (Codex / Cline / others) pass OpenAI- or Anthropic-style model names,
# which are all mapped onto muse's real capabilities here.
#
# Note: the muse.ai web page is an auto-routing agent with **no enumerable model list**;
# the only reliably callable capabilities are the three real ones -- Muse Spark (language/code),
# Muse Image (image generation), Muse Video (video generation). Aliases just spare downstream config changes.
MODEL_ALIASES = {
    # ---- text / code -> muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    # ---- OpenAI GPT & Codex models ----
    "gpt-6-astra": "muse-spark", "gpt-6.1-sol": "muse-spark", "gpt-6-sol": "muse-spark",
    "gpt-6-luna": "muse-spark", "gpt-5.4": "muse-spark", "gpt-5.4-mini": "muse-spark",
    "gpt-5.4-pro": "muse-spark", "gpt-5.3-codex": "muse-spark", "gpt-5.2-codex": "muse-spark",
    "gpt-5.1-codex": "muse-spark", "gpt-5-codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "codex": "muse-spark", "gpt-5.2": "muse-spark", "gpt-5.1": "muse-spark", "gpt-5": "muse-spark",
    "gpt-4.1": "muse-spark", "gpt-4.1-mini": "muse-spark", "gpt-4.1-nano": "muse-spark",
    "gpt-4.5": "muse-spark", "gpt-4.5-preview": "muse-spark", "chatgpt-4o-latest": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-3.5-turbo": "muse-spark",
    # ---- OpenAI o-series reasoning models ----
    "o4-mini": "muse-spark", "o3": "muse-spark", "o3-mini": "muse-spark",
    "o3-mini-high": "muse-spark", "o3-mini-medium": "muse-spark", "o3-mini-low": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o1-preview": "muse-spark", "o1-pro": "muse-spark",
    # ---- Anthropic Claude models (current & flagship) ----
    "claude-opus-5-5": "muse-spark", "claude-sonnet-5-5": "muse-spark", "claude-fable-5-1": "muse-spark",
    "claude-haiku-4-5": "muse-spark", "claude-fable-5": "muse-spark", "claude-opus-5": "muse-spark",
    "claude-sonnet-5": "muse-spark", "claude-opus-4-8": "muse-spark", "claude-opus-4-7": "muse-spark",
    "claude-opus-4-6": "muse-spark", "claude-sonnet-4-6": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-7-sonnet": "muse-spark", "claude-3-5-sonnet": "muse-spark",
    "claude-3-5-sonnet-latest": "muse-spark", "claude-3-5-sonnet-20241022": "muse-spark",
    "claude-3-5-haiku": "muse-spark", "claude-3-5-haiku-20241022": "muse-spark",
    "claude-3-opus": "muse-spark", "claude-3-haiku": "muse-spark",
    "claude-instant-1.2": "muse-spark", "claude-2.1": "muse-spark", "claude-2.0": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-5-sonnet-20241022": "muse-spark", "claude-3-5-sonnet-20240620": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark", "claude-3-opus-20240229": "muse-spark",
    "claude-3-haiku": "muse-spark", "claude-3-haiku-20240307": "muse-spark",
    "claude-3-5-haiku": "muse-spark", "claude-3-5-haiku-20241022": "muse-spark",
    "claude-instant-1.2": "muse-spark", "claude-2.1": "muse-spark", "claude-2.0": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- image generation -> muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image", "dall-e-2": "muse-image",
    "gpt-image-1": "muse-image", "gpt-image-2": "muse-image",
    "gpt-image-2.5-flare": "muse-image", "gpt-image-2.5-sunburst": "muse-image",
    "chatgpt-image-latest": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    if not n:
        return default
    if n in MODEL_ALIASES:
        return MODEL_ALIASES[n]
    # Wildcard prefix matching for all current and future Claude models
    if n.startswith("claude") or n.startswith("anthropic"):
        return "muse-spark"
    # Wildcard prefix matching for image models (must precede general gpt- check)
    if any(n.startswith(p) for p in ("dall-e", "flux", "midjourney", "image", "gpt-image", "chatgpt-image")):
        return "muse-image"
    # Wildcard prefix matching for video models
    if any(n.startswith(p) for p in ("sora", "veo", "kling", "runway", "video")):
        return "muse-video"
    # Wildcard prefix matching for OpenAI language & reasoning models
    if any(n.startswith(p) for p in ("gpt-", "o1", "o2", "o3", "o4", "o5", "o6", "chatgpt", "codex")):
        return "muse-spark"
    return n or default


# ------------------------- Auth -------------------------
def auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    if not CFG.api_key:
        return True
    token = ""
    if x_api_key:
        token = x_api_key.strip()
    elif authorization:
        if authorization.lower().startswith("bearer "):
            parts = authorization.split(None, 1)
            token = parts[1].strip() if len(parts) > 1 else ""
        else:
            token = authorization.strip()
    if not token or token != CFG.api_key:
        raise HTTPException(401, "Missing or invalid API key (pass Bearer token or x-api-key header)")
    return True


# ------------------------- Request models -------------------------

def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """Renew account cookies via /api/session, persist to the store, and return the fresh account dict.
    Accounts renewed within the last 10 minutes with healthy status are reused directly, avoiding a 2-3s HTTP round trip per request."""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm)
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"Session healthy (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except Exception as exc:
        log.warning("HTTP pre-renewal for account %s failed: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """Run chat_stream on a dedicated thread while holding GEN_LOCK, streaming chunks out via a Queue.
    No matter when the downstream client disconnects, errors, or times out, stop_event + the finally block guarantee GEN_LOCK is released immediately -- never deadlocks.
    If a single-account VM stall or session error hits before the first token, automatically fail over to the next healthy account and retry once."""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        try:
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    alt = store.pick_account(rotate=True, force_rotate=True, exclude_id=cur_id)
                    if not alt or alt["id"] == cur_id:
                        break
                    cur_id = alt["id"]
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("[chat auto-failover] Switching to standby account %s (%s), retrying...", alt.get("label"), cur_id)
                yielded = False
                try:
                    if cur_id:
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    with GEN_LOCK:
                        if stop_event.is_set():
                            return
                        engine.start()
                        for chunk in engine.chat_stream(
                            cur_cookies, prompt, cur_exp, timeout,
                            account_id=cur_id, stop_event=stop_event
                        ):
                            yielded = True
                            q.put(("data", chunk))
                            if stop_event.is_set():
                                return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                q.put(("error", last_exc))
        finally:
            q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    duration: int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = None
    extra: str | None = None
    image: Any = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions request.

    Be lenient toward third-party clients: ignore unknown fields (Pydantic default),
    and only read what we actually use -- otherwise each agent passing its own params would 422.
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # compat for clients that put prompt at the top level
    # Declared below only so they can be read; the muse.ai side does not act on them
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API (new Codex default)."""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AnthropicMessagesRequest(BaseModel):
    """Anthropic Messages API request (POST /v1/messages).
    Compatible with Claude Code, Claude Agent SDK, and official Anthropic SDKs."""
    model: str = "claude-3-5-sonnet"
    messages: list[ChatMessage] = []
    system: str | list | None = None
    max_tokens: int | None = 4096
    metadata: dict | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    tools: list | None = None
    tool_choice: object | None = None
    timeout: int | None = None
    thinking: object | None = None

class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # multi-line text, one account per line
    expires: dict[str, int] = Field(default_factory=dict)   # cookie name -> expiry timestamp


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None


# ------------------------- Prompt construction -------------------------
# NOTE: aspect-ratio keyword tuples keep Chinese aliases (竖屏/横屏/正方形) for backward compat with existing API callers. Do not remove during translation cleanup.
def build_image_prompt(r: ImageRequest) -> str:
    has_ref = bool(r.reference_image or r.image or r.images)
    if has_ref:
        parts = [f"Using the attached reference image for image generation/editing: {r.prompt.strip()}"]
    else:
        parts = [f"Brand-new text-to-image creation (no reference image provided; do not look up history albums or ask the user for a source image; draw one brand-new image from scratch from the text description): {r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        parts.append("[Composition and aspect-ratio requirement]: strict 9:16 vertical full-frame portrait (9:16 vertical portrait aspect ratio, phone fullscreen vertical frame taller than wide); never generate landscape, keep vertical composition")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("[Composition and aspect-ratio requirement]: 16:9 widescreen landscape frame (16:9 widescreen landscape aspect ratio)")
    elif any(k in ar or k in sz for k in ("1:1", "square", "正方形", "1024x1024")):
        parts.append("[Composition and aspect-ratio requirement]: 1:1 square frame (1:1 square aspect ratio)")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("[Composition and aspect-ratio requirement]: 4:3 aspect-ratio frame")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("[Composition and aspect-ratio requirement]: 3:4 vertical frame")
    elif r.aspect_ratio:
        parts.append(f"[Composition and aspect-ratio requirement]: {r.aspect_ratio} aspect ratio")
    elif r.size:
        parts.append(f"Size/ratio: {r.size}")

    if has_ref:
        parts.append("[Clean-frame requirement]: completely remove all text, watermarks, signatures, badges, and logo marks from the reference image (clean image without any watermark, text, or logo); output a perfectly clean text-free frame")

    if r.extra:
        parts.append(r.extra)
    return ", ".join(parts)


def _content_text(content) -> str:
    """Normalize OpenAI content into plain text (supports multimodal list form)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append("[image]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def _render_tool_result_text(tool_name: str, raw: str) -> str:
    """Render tool result as natural in-conversation text — no protocol markers.

    Muse should see this as factual context, like the user sharing info they looked up.
    """
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            pairs = [f"{k}: {v}" for k, v in data.items() if v is not None and str(v).strip()]
            pretty = ", ".join(pairs) if len(pairs) <= 4 else "\n".join(pairs)
        elif isinstance(data, list):
            lines = []
            for item in data[:10]:
                if isinstance(item, dict):
                    kv = ", ".join(f"{k}: {v}" for k, v in item.items() if v is not None)
                    lines.append(kv)
                else:
                    lines.append(str(item))
            pretty = "\n".join(lines)
        else:
            pretty = str(data).strip()
    except Exception:
        pretty = raw.strip()
    return pretty


def _tool_call_label(tool_calls: list) -> str:  # noqa: ARG001
    """Assistant tool_calls turn — return empty so it's skipped in the prompt."""
    return ""


def _match_tool_name(call_id: str | None, tool_calls_history: list[tuple]) -> str:
    """Find the tool function name from recent assistant tool_call turns by call id."""
    if not call_id:
        return "tool"
    for label_text in reversed(tool_calls_history):
        # label_text is like "[Called: get_weather(city=\"Tokyo\")]"
        m = re.search(r'\[Called: ([\w.]+)', label_text)
        if m:
            return m.group(1)
    return "tool"


def build_chat_prompt(messages: list[ChatMessage]) -> str:
    """Join messages into one prompt for muse.ai.

    The muse.ai web page is itself a contextual session, but this API is stateless (each call may land on
    a different account/page), so inlining history into the prompt is the most controllable -- which matches
    Codex-style clients that send full history every round.
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content).strip()
        if role == "tool":
            # Inject tool result as an ASSISTANT turn so muse reads it as data it
            # already retrieved — prevents muse from firing its own web search.
            if text:
                pretty = _render_tool_result_text("", text)
                if pretty:
                    turns.append(("assistant", pretty))
            continue
        if not text:
            # assistant turn with only tool_calls — omit (internal step, not shown)
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # Single turn with no system instructions -> send the raw text, closest to natural chat
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"Background and task setup:\n{sys_text}")
    for role, text in turns:
        label = "assistant" if role == "assistant" else "user"
        parts.append(f"{label}:\n{text}")
    return "\n\n".join(parts)

def _anthropic_content_text(content) -> str:
    """Normalize Anthropic content into plain text (supports blocks and string form)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                b_type = block.get("type")
                if b_type in (None, "text", "input_text", "output_text"):
                    parts.append(str(block.get("text") or ""))
                elif b_type == "tool_use":
                    # Skip tool_use blocks — internal step, not shown to muse
                    pass
                elif b_type == "tool_result":
                    res_content = _anthropic_content_text(block.get("content"))
                    # Render as plain data — no protocol labels
                    parts.append(_render_tool_result_text("", res_content))
                elif b_type == "image":
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return str(content)


def build_anthropic_prompt(req: AnthropicMessagesRequest) -> str:
    """Join Anthropic messages and system prompt into one coherent prompt for muse.ai."""
    system_text = _anthropic_content_text(req.system).strip()
    turns = []
    for m in req.messages:
        role = (m.role or "").strip().lower()
        text = _anthropic_content_text(m.content).strip()
        if text:
            turns.append((role, text))
        # Empty assistant turns (tool_calls only) → silently skipped by the text check above
    if len(turns) == 1 and not system_text and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system_text:
        parts.append(f"Background and task setup:\n{system_text}")
    for role, text in turns:
        label = "assistant" if role == "assistant" else "user"
        parts.append(f"{label}:\n{text}")
    return "\n\n".join(parts)


# ------------------------- Tool-calling (function calling) adapter -------------------------
# The muse.ai web model **never** returns structured tool_calls, so this layer adapts the protocol:
#   1) when the request carries tools, translate the tool definitions into a [Tool-calling protocol] prompt section;
#   2) the model outputs ```json {"tool": "...", "arguments": {...}} ``` per the protocol;
#   3) we parse that back into OpenAI tool_calls for the downstream agent.
#
# Note: this is "best-effort adaptation", not a guarantee -- the target model is a general chat model without
# function-calling fine-tuning; protocol compliance needs empirical observation.
_TOOL_PROTOCOL_HEAD = """You may call the following tools to help complete the user task.
If you need to call a tool, output a JSON code block in exactly this shape (with no extra explanation):
```json
{"name": "<tool name>", "arguments": {<params>}}
```
To call multiple tools, output a JSON array of such objects.
If no tool is needed, just answer in natural language.

Available tools:
"""


def _describe_params(params) -> str:
    """Describe JSON Schema params as readable multi-line text."""
    if not isinstance(params, dict):
        return "      (no params)"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      (no params)"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "required" if name in required else "optional",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """Translate tool definitions into a prompt fragment (empty string when no tools).

    Supports both Chat Completions `{"type":"function","function":{...}}`
    and Responses API `{"type":"function","name":...,"parameters":...}`.
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   params:\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """Convert parsed JSON into OpenAI tool_calls; returns None when it does not look like a tool call."""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """Extract tool calls from model output.

    Returns (tool_calls, remaining text); (None, original) when none found.
    Supported shapes:
    1) ```json ... ``` fenced block;
    2) bare JSON (optionally prefixed with json/JSON);
    3) a complete JSON object/array embedded in text.
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest) -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    dur = r.duration or 6
    has_ref = bool(r.reference_image or r.image or r.image_url)

    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"Use the reference image attached with this upload as the first-frame reference (strictly start from the image just uploaded here, never history or other images): generate a strict 9:16 vertical fullscreen phone image-to-video (9:16 vertical portrait video, 720x1280, taller-than-wide fullscreen phone frame, continuing motion from the attached reference as first frame; never landscape or letterboxed; exactly {dur} seconds): {user_prompt}")
        else:
            parts.append(f"Brand-new text-to-video (pure-text fresh generation; never reference history images or context): strict 9:16 vertical fullscreen phone video (9:16 vertical portrait video, 720x1280, taller-than-wide fullscreen phone frame; never landscape or letterboxed; vertical composition; exactly {dur} seconds): {user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"Use the reference image attached with this upload as the first-frame reference (strictly start from the image just uploaded here, never history or other images): generate a 16:9 widescreen landscape image-to-video (16:9 widescreen landscape video, continuing motion from the attached reference as first frame; exactly {dur} seconds): {user_prompt}")
        else:
            parts.append(f"Brand-new text-to-video (pure-text fresh generation; never reference history images or context): 16:9 landscape widescreen video (16:9 widescreen landscape video; exactly {dur} seconds): {user_prompt}")
    else:
        if has_ref:
            parts.append(f"Use the reference image attached with this upload as the first-frame reference (strictly start from the image just uploaded here, never history or other images): image-to-video continuing motion from the attached reference as first frame (exactly {dur} seconds): {user_prompt}")
        else:
            parts.append(f"Brand-new text-to-video (pure-text fresh generation; never reference history images or context): generate a video (exactly {dur} seconds): {user_prompt}")

    if r.resolution:
        parts.append(f"Quality spec: {r.resolution}")
    if r.extra:
        parts.append(r.extra)
    return ", ".join(parts)


def media_url(name: str) -> str:
    """Media URL.

    When public_base is set, return an **absolute URL** -- OpenAI-compatible clients (and agent
    platforms) generally render or download data[].url directly; a relative path would resolve against
    the client's own domain and 404. Fall back to a relative path when public_base is empty.
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- Cookie parsing -------------------------
def parse_cookie_payload(text: str) -> tuple[dict[str, str], dict[str, int]]:
    """Parse cookies and optional expiration timestamps from diverse formats:
    1. Netscape HTTP Cookie File (cookies.txt format, including #HttpOnly_ prefixes and tab/space columns)
    2. Firefox / Chrome DevTools inspect element JSON (e.g. {"Request Cookies": {...}} or raw cookie dicts)
    3. JSON array of objects (HAR format or [{name: ..., value: ..., expires: ...}])
    4. Copied text with ANSI terminal escape sequences or bracket artifacts ([13;28;13;1;0;1_...)
    5. Standard HTTP headers (`Cookie: a=1; b=2`, Set-Cookie headers)
    """
    text = (text or "").strip()
    if not text:
        return {}, {}

    import urllib.parse

    # 0. Terminal copypasta recovery:
    # Terminals (conpty/mintty/tmux) emit control escapes like:
    # - [13;28;..._ for Carriage Return / Newline (ASCII 13)
    # - [9;15;..._ for Tab (ASCII 9)
    # We map them back to actual newlines and tabs, then strip any remaining [x;y;z_ markers.
    text = re.sub(r"\[13(?:;\d+)*_", "\n", text)
    text = re.sub(r"\[9(?:;\d+)*_", "\t", text)
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\[\d+(?:;\d+)*_", "", text)

    cookies: dict[str, str] = {}
    expires: dict[str, int] = {}

    # 1. Netscape cookies.txt format
    lines = text.splitlines()
    has_netscape = False
    for line in lines:
        raw_l = line.strip()
        if not raw_l:
            continue
        if raw_l.startswith("# Netscape") or raw_l.startswith("# https:") or raw_l.startswith("# This is") or raw_l.startswith("# HTTP Cookie File"):
            has_netscape = True
            continue
        if raw_l.startswith("#HttpOnly_"):
            raw_l = raw_l[len("#HttpOnly_"):].strip()
        elif raw_l.startswith("#"):
            continue
        parts = re.split(r"\t+|\s{2,}", raw_l)
        if len(parts) >= 7:
            has_netscape = True
            name = parts[5].strip()
            val = urllib.parse.unquote(parts[6].strip())
            if name:
                cookies[name] = val
                try:
                    exp_val = int(parts[4].strip())
                    if exp_val > 0:
                        expires[name] = exp_val
                except (ValueError, TypeError):
                    pass
    if has_netscape and cookies:
        return cookies, expires

    # 2. JSON / Firefox inspect element
    trimmed = text.strip()
    if trimmed.lower().startswith("cookie:"):
        trimmed = trimmed[7:].strip()

    if "{" in trimmed or "[" in trimmed:
        for candidate in [trimmed, re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", trimmed)]:
            cand_str = candidate.group(0) if hasattr(candidate, "group") else candidate
            if not cand_str:
                continue
            try:
                import json
                obj = json.loads(cand_str)
                def extract(o):
                    if isinstance(o, dict):
                        for k in ("Request Cookies", "requestCookies", "cookies", "Cookies"):
                            if k in o and isinstance(o[k], (dict, list)):
                                return extract(o[k])
                        for k, v in o.items():
                            if isinstance(v, (dict, list)):
                                extract(v)
                            else:
                                cookies[str(k).strip()] = urllib.parse.unquote(str(v).strip())
                    elif isinstance(o, list):
                        for item in o:
                            if isinstance(item, dict):
                                if "name" in item and "value" in item:
                                    name = str(item["name"]).strip()
                                    cookies[name] = urllib.parse.unquote(str(item["value"]).strip())
                                    for ek in ("expires", "expirationDate", "expiry"):
                                        if ek in item:
                                            try:
                                                exp_v = int(float(item[ek]))
                                                if exp_v > 0:
                                                    expires[name] = exp_v
                                            except (ValueError, TypeError):
                                                pass
                                else:
                                    extract(item)
                extract(obj)
                if cookies:
                    return cookies, expires
            except Exception:
                pass

    # 3. Fallback: regex for JSON key-value pairs (e.g. malformed JSON or unescaped quotes)
    json_pairs = re.findall(r'\"([a-zA-Z0-9_\-\.]+)\"\s*:\s*\"([^\"]*)\"', text)
    if json_pairs:
        keys = [k for k, v in json_pairs if k not in ("Request Cookies", "requestCookies", "cookies", "Cookies")]
        if any(c in keys for c in ("hatch_sess", "hatch_gw", "hatch_vml", "hatch_native_auth_device", "datr", "dpr")):
            for k, v in json_pairs:
                if k not in ("Request Cookies", "requestCookies", "cookies", "Cookies"):
                    cookies[k] = urllib.parse.unquote(v)
            return cookies, expires

    # 4. Standard HTTP header (Cookie: a=1; b=2) or Set-Cookie lines
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part:
            continue
        if part.lower().startswith("cookie:"):
            part = part[7:].strip()
        if part.lower().startswith("set-cookie:"):
            part = part[11:].strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k.lower() in ("path", "domain", "expires", "max-age", "samesite", "secure", "httponly"):
            continue
        if k:
            cookies[k] = urllib.parse.unquote(v.strip())
    return cookies, expires


def parse_cookie_text(text: str) -> dict[str, str]:
    """Parse `a=1; b=2` / JSON / Firefox DevTools / cookies.txt / Set-Cookie into a dict."""
    return parse_cookie_payload(text)[0]


def parse_batch(text: str) -> list[tuple[str, dict, dict]]:
    """Batch import: one `label | cookie-string` per line (label optional).
    Also transparently detects if the entire text is a single multi-line cookies.txt file
    or Firefox DevTools JSON object and parses it as a single account."""
    raw_text = (text or "").strip()
    if not raw_text:
        return []

    raw_text = re.sub(r"\[13(?:;\d+)*_", "\n", raw_text)
    raw_text = re.sub(r"\[9(?:;\d+)*_", "\t", raw_text)
    raw_text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_text)
    raw_text = re.sub(r"\[\d+(?:;\d+)*_", "", raw_text)
    lines = [l.strip() for l in raw_text.splitlines() if l.strip() and not l.strip().startswith("#")]

    # Check if this is an explicit multi-account batch format (lines containing 'label | cookie')
    has_explicit_batch = any("|" in l and "=" not in l.split("|", 1)[0] for l in lines)

    if not has_explicit_batch:
        # Check if the entire payload parses as a unified single account
        single_c, single_e = parse_cookie_payload(raw_text)
        if single_c:
            return [("", single_c, single_e)]

    out: list[tuple[str, dict, dict]] = []
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # a `|` inside a cookie value is not a separator
                label, body = head.strip(), tail.strip()
        c, e = parse_cookie_payload(body)
        if c:
            out.append((label, c, e))
    return out


# ------------------------- Core generation -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """Read cookies back from the browser after generation and persist them to the pool.

    Measured result: **core-cookie expires is NOT extended by usage** (hatch_vml expires after a fixed
    ~2 days); what gets synced back here is mostly the non-core cookies re-issued on every visit
    (_fbp / wd / dpr etc.) plus any newly added entries, keeping account data from going stale.

    Note: this function must **never fail the generation** -- generation already succeeded;
    syncing is best-effort, so any problem only logs one line.
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # Merge expiries instead of overwriting: session cookies read back with expires of -1,
        # and a wholesale overwrite would wipe the recorded expiries (learned the hard way).
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("Cookie sync failed (generation unaffected): %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None) -> tuple[dict, str | None]:
    # Browser ownership covers account selection, retry and cleanup, not just generate().
    deadline = time.monotonic() + max(1, timeout)
    if not GEN_LOCK.acquire(timeout=max(1, timeout)):
        raise MuseGenerationError("Browser queue wait timed out; please retry later")
    try:
        return _run_generation_locked(prompt, kind, timeout, account_id,
                                      on_progress, reference_image, deadline=deadline)
    finally:
        GEN_LOCK.release()


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None, deadline=None) -> tuple[dict, str | None]:
    acc = store.get_account(account_id) if account_id else None
    if acc and not acc.get("enabled", True):
        acc = None
    if not acc:
        acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise MuseAuthError("No available accounts; import cookies on the admin page first")

    last_exc = None
    cur_acc = acc
    for attempt in range(2):
        if deadline is not None and time.monotonic() >= deadline:
            raise MuseGenerationError("Total task wait budget exhausted; stopping retries")
        if attempt > 0:
            if last_exc and "Model returned only text, no media" in str(last_exc):
                break
            alt = store.pick_account(rotate=True, force_rotate=True, exclude_id=cur_acc["id"])
            if not alt or alt["id"] == cur_acc["id"]:
                break
            cur_acc = alt
            log.info("[image/video auto-failover] Switching to standby account %s (%s), retrying...", cur_acc.get("label"), cur_acc["id"])
        try:
            refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
            if refreshed:
                cur_acc = refreshed
            engine.start()
            remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
            if remaining <= 0:
                raise MuseGenerationError("Total task wait budget exhausted; stopping retries")
            res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                  timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                  account_id=cur_acc["id"], on_progress=on_progress,
                                  reference_image=reference_image)
            store.mark(cur_acc["id"], True, "")
            _sync_cookies(cur_acc["id"])
            return res, cur_acc["id"]
        except MuseAuthError as exc:
            last_exc = exc
            store.mark(cur_acc["id"], False, str(exc))
            engine.stop()
        except MuseGenerationError as exc:
            last_exc = exc
            store.mark(cur_acc["id"], True, f"Task error: {str(exc)[:60]}")
            try:
                engine.reset_thread()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            engine.stop()
            last_exc = MuseGenerationError(f"Generation failed: {exc}")
    raise last_exc


# ------------------------- Basic endpoints -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {
        "object": "list",
        "data": MODELS,
        "has_more": False,
        "first_id": MODELS[0]["id"] if MODELS else None,
        "last_id": MODELS[-1]["id"] if MODELS else None,
    }

# ------------------------- Image generation -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _queue_image(req: ImageRequest, prompt: str, reference_image: str | None,
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "reference": reference_image},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("image_request_key") == key_hash:
                    if existing.get("image_request_hash") != request_hash:
                        raise HTTPException(409, "Idempotency-Key reused with different request")
                    return JSONResponse(status_code=202, content={
                        "id": existing["id"], "task_id": existing["id"],
                        "object": "image.task", "status": existing["status"],
                        "progress": existing.get("progress", 0),
                        "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        if sum(t.get("kind") == "image" and t.get("status") in ("queued", "processing")
               for t in tasks) >= 8:
            raise HTTPException(429, "Image queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        t0 = time.time()
        store.update_task(tid, status="processing", progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_image=reference_image,
                on_progress=lambda p: store.update_task(tid, progress=p))
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - t0, 1),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"])})
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="failed", error=str(exc),
                              elapsed=round(time.time() - t0, 1))

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images:
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first
    return _queue_image(req, build_image_prompt(req), ref_img, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task not found")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images and isinstance(req.images, list):
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first

    prompt = build_image_prompt(req)
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_img)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, prompt, "image", timeout, reference_image=ref_img)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI-compatible image-to-image / image-edit endpoint; supports multipart/form-data and application/json."""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data = None
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form()
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        img_field = form.get("image")
        if img_field and hasattr(img_field, "read"):
            content = await img_field.read()
            ref_mime = getattr(img_field, "content_type", "image/png") or "image/png"
            ref_image_data = f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}"
        elif isinstance(img_field, str):
            ref_image_data = img_field
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        img_val = body.get("image")
        if isinstance(img_val, dict):
            ref_image_data = img_val.get("url") or img_val.get("b64_json")
        elif isinstance(img_val, str):
            ref_image_data = img_val
        if not ref_image_data and body.get("images") and isinstance(body.get("images"), list):
            first = body["images"][0]
            if isinstance(first, dict):
                ref_image_data = first.get("image_url") or first.get("url")
            elif isinstance(first, str):
                ref_image_data = first
        if not ref_image_data:
            ref_image_data = body.get("reference_image") or body.get("image_url")

    if not prompt:
        prompt = "Generate an image from this reference picture"

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        reference_image=ref_image_data,
        **{"async": async_mode}
    )
    full_prompt = build_image_prompt(req_obj)
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, full_prompt, "image", gen_timeout, reference_image=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- Video generation (async tasks) -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest, _=Depends(auth)):
    ref_img = None
    if req.reference_image:
        ref_img = req.reference_image
    elif req.image_url:
        ref_img = req.image_url if isinstance(req.image_url, str) else (req.image_url.get("url") if isinstance(req.image_url, dict) else None)
    elif req.image:
        ref_img = req.image.get("url") if isinstance(req.image, dict) else req.image

    prompt = build_video_prompt(req)
    timeout = req.timeout or CFG.video_timeout
    task = store.create_task("video", req.prompt)
    store.update_task(task["id"], api_prompt=prompt)

    store.update_task(task["id"], progress=10)

    def worker():
        store.update_task(task["id"], status="processing", progress=15)
        t0 = time.time()
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb, reference_image=ref_img)
            vurl = media_url(res["filename"])
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - t0, 1),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
        except Exception as exc:  # noqa: BLE001
            store.update_task(task["id"], status="failed",
                              elapsed=round(time.time() - t0, 1), error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    out = dict(t)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        elapsed = time.time() - out.get("created_at", time.time())
        calc_prog = min(92, int(20 + elapsed * 1.1))
        out["progress"] = max(out.get("progress", 0) or 0, calc_prog)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- Chat (OpenAI-compatible) -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """Smooth a bulky DOM-poll delta into native-LLM-like micro-tokens.
    Tiny text (<= 3 chars) passes through with zero delay; large bulk
    refreshes are sliced into `chunk_size`-char pieces spaced `delay` apart."""
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

def _want_usage(stream_options) -> bool:
    """Downstream clients (LangChain, some SDKs / agent frameworks) send
    `stream_options.include_usage=true`, requiring one extra chunk before [DONE] with
    empty `choices: []` plus `usage`. Without it a few frameworks wait for usage forever."""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False



@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI-compatible chat endpoint -- any agent client can connect.

    Note: the muse.ai web page is an **auto-routing** agent with no model picker; every request
    lands on the same web assistant (calling itself Koda, backed by Muse-series language models). So the
    `model` field only keeps downstream compatibility and does not affect routing.

    When the request carries `tools`, a [Tool-calling protocol] section is injected and the model's JSON output
    is parsed back into `tool_calls` (muse.ai has no native function calling; this layer is a protocol adapter).

    Chat shares one browser instance with image/video generation, serialized via `GEN_LOCK`.
    """
    prompt = build_chat_prompt(req.messages) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages is empty")

    # ── Jev tool-call dispatch (runs before muse; bypasses muse entirely on match) ──
    # muse.ai's assistant explicitly refuses pseudo-tool-call JSON, so prompt injection
    # doesn't work.  Jev reads the conversation + tool definitions and selects the right
    # tool and its arguments directly.  Falls through to muse when no tool is triggered.
    # Only dispatch via Jev on round 1 (no tool results in history yet).
    # Round 2+ has role="tool" messages — tool data is already injected as assistant
    # context in the prompt; muse just needs to summarize it.
    _has_tool_results = any(
        (getattr(m, "role", None) or "") == "tool"
        for m in req.messages
    )
    if req.tools and not _has_tool_results:
        if req.stream:
            # For streaming requests: run Jev inside the generator so we can emit
            # a keepalive comment immediately (prevents omp/client first-event timeout).
            _jev_messages = list(req.messages)
            _jev_tools = list(req.tools)
            _stream_cid = "chatcmpl-" + uuid.uuid4().hex[:24]
            _stream_created = int(time.time())
            _stream_model = resolve_model(req.model, default="muse-spark")

            async def _jev_streaming_dispatch():
                # Keepalive comment — arrives immediately, resets client first-event timer
                yield ": processing\n\n"
                jev_calls = await asyncio.to_thread(jev_tool_dispatch, _jev_messages, _jev_tools)
                if jev_calls:
                    log.info("Jev dispatched tool_calls (stream): %s", [c["function"]["name"] for c in jev_calls])
                    # Emit role chunk
                    yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {"role": "assistant"}))
                    # Emit tool_call deltas per OpenAI streaming convention
                    head = [{"index": i, "id": c["id"], "type": "function",
                             "function": {"name": c["function"]["name"], "arguments": ""}}
                            for i, c in enumerate(jev_calls)]
                    body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                            for i, c in enumerate(jev_calls)]
                    yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {"tool_calls": head}))
                    yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {"tool_calls": body}))
                    yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {}, finish="tool_calls"))
                    yield "data: [DONE]\n\n"
                else:
                    # No tool call — fall through: stream muse normally
                    # Re-enter normal stream path by yielding from it
                    acc2 = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
                    if not acc2:
                        yield _sse({"error": {"message": "No available accounts", "type": "server_error", "code": 503}})
                        return
                    _prompt2 = build_chat_prompt(_jev_messages) or ""
                    _to2 = int(req.timeout or CFG.chat_timeout)
                    yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {"role": "assistant"}))
                    stream_gen = safe_chat_stream(acc2["cookies"], _prompt2,
                                                  acc2.get("cookies_exp"), _to2,
                                                  account_id=acc2["id"])
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {"content": piece}))
                    yield _sse(_chat_chunk(_stream_cid, _stream_created, _stream_model, {}, finish="stop"))
                    yield "data: [DONE]\n\n"

            return StreamingResponse(_jev_streaming_dispatch(), media_type="text/event-stream", headers=_SSE_HEADERS)
        else:
            # Non-streaming: run Jev and return JSON directly
            jev_calls = await asyncio.to_thread(jev_tool_dispatch, req.messages, req.tools)
            if jev_calls:
                cid = "chatcmpl-" + uuid.uuid4().hex[:24]
                created = int(time.time())
                model = resolve_model(req.model, default="muse-spark")
                log.info("Jev dispatched tool_calls: %s", [c["function"]["name"] for c in jev_calls])
                return {"id": cid, "object": "chat.completion", "created": created,
                        "model": model,
                        "choices": [{"index": 0, "finish_reason": "tool_calls",
                                     "message": {"role": "assistant", "content": None,
                                                 "tool_calls": jev_calls}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    # The old prompt-injection path (off by default; MUSE2API_TOOL_PROTOCOL=1 re-enables).
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "No available accounts; import cookies on the admin page first")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """Send in two chunks per OpenAI convention: id/name first, then the full arguments."""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # Emit role:assistant chunk immediately so downstream clients show cursor right away
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"Assistant timeout: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"Internal error: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """Normalize Responses API instructions / input into messages."""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message shape
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="[Tool result]\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="[Requesting tool call]"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API -- the new Codex default endpoint.

    Only the subset Codex actually uses: text in -> text out (including streaming events).
    muse.ai never returns structured tool_calls; see the README for tool-calling limits.
    """
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input is empty")

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "No available accounts; import cookies on the admin page first")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses streaming failed: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    return envelope("completed", text)

# ------------------------- Anthropic Messages API (Claude Code & Agent SDK compatible) -------------------------
_ANTHROPIC_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
    "X-Accel-Buffering": "no",
}


def _anthropic_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.head("/api/hello")
@app.get("/api/hello")
def api_hello():
    """Connection-warming probe from Claude Code."""
    return Response(status_code=200)


@app.post("/v1/messages/count_tokens")
@app.post("/messages/count_tokens")
def count_tokens(req: AnthropicMessagesRequest, _=Depends(auth)):
    """Token counting endpoint for Claude Code and Anthropic clients."""
    prompt = build_anthropic_prompt(req)
    input_tokens = max(1, len(prompt) // 4)
    return {"input_tokens": input_tokens}


@app.post("/v1/messages")
@app.post("/messages")
@app.post("/v1/v1/messages")
async def anthropic_messages(req: AnthropicMessagesRequest, _=Depends(auth)):
    """Anthropic Messages API endpoint.
    Fully compatible with Claude Code, Claude Agent SDK, Anthropic Python/TypeScript SDKs,
    Cursor, Continue, Cline, and any Anthropic-compatible tool."""
    prompt = build_anthropic_prompt(req)
    if not prompt:
        raise HTTPException(400, "messages is empty")

    # ── Jev tool-call dispatch for Anthropic Messages API ──────────────────────────
    # Skip on round 2+ (tool_result blocks present) — muse handles it from injected context.
    _anth_has_results = any(
        isinstance(m.content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m.content
        )
        for m in req.messages
    )
    if req.tools and not _anth_has_results:
        anthropic_messages_list = [m.model_dump() for m in req.messages]
        jev_calls = await asyncio.to_thread(jev_tool_dispatch, anthropic_messages_list, req.tools)
        if jev_calls:
            log.info("Jev dispatched tool_calls (Anthropic): %s", [c["function"]["name"] for c in jev_calls])
            tool_use_blocks = [
                {
                    "type": "tool_use",
                    "id": c["id"],
                    "name": c["function"]["name"],
                    "input": json.loads(c["function"]["arguments"] or "{}"),
                }
                for c in jev_calls
            ]
            input_tokens = max(1, len(prompt) // 4)
            return {
                "id": "msg_" + uuid.uuid4().hex[:24],
                "type": "message",
                "role": "assistant",
                "model": req.model,
                "content": tool_use_blocks,
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            }

    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol and req.tools else ""
    if tool_note:
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "No available accounts; import cookies on the admin page first")

    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    input_tokens = max(1, len(prompt) // 4)

    if req.stream:
        def sync_anthropic_stream():
            try:
                # 1. message_start
                yield _anthropic_sse("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": req.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": input_tokens, "output_tokens": 1}
                    }
                })

                # 2. content_block_start
                yield _anthropic_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""}
                })

                # 3. Stream incremental deltas
                output_chars = 0
                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                for chunk in stream_gen:
                    for piece in _pace_text(chunk):
                        output_chars += len(piece)
                        yield _anthropic_sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": piece}
                        })

                # 4. content_block_stop
                yield _anthropic_sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": 0
                })

                # 5. message_delta
                output_tokens = max(1, output_chars // 4)
                yield _anthropic_sse("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens}
                })

                # 6. message_stop
                yield _anthropic_sse("message_stop", {
                    "type": "message_stop"
                })
            except Exception as exc:
                log.warning("Anthropic streaming failed: %s", exc)
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _anthropic_sse("error", {
                    "type": "error",
                    "error": {"type": "api_error", "message": str(exc)}
                })

        return StreamingResponse(sync_anthropic_stream(), media_type="text/event-stream", headers=_ANTHROPIC_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    output_tokens = max(1, len(text) // 4)
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": req.model,
        "content": [
            {
                "type": "text",
                "text": text
            }
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        }
    }

@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid filename")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    return FileResponse(p)


# ------------------------- Admin: overview -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "elevated": _is_elevated(),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- Admin: account pool -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    added: list[dict] = []
    seen: list[dict] = []          # all cookies imported this time, used to check core-item completeness

    if req.batch:
        for label, cookies, exp_map in parse_batch(req.batch):
            acc = store.add_account(cookies, label or req.label, cookies_exp=exp_map or req.expires)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    expires = dict(req.expires or {})
    if req.cookie_header:
        parsed_c, parsed_e = parse_cookie_payload(req.cookie_header)
        cookies.update(parsed_c)
        if parsed_e and not expires:
            expires.update(parsed_e)
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "No cookies parsed; check the format")

    # Passing only requires one account to hold all core cookies (judged across the batch)
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "warning": (f"Missing core cookies: {', '.join(missing)}; this account may fail to generate"
                        if missing else "")}


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    acc = store.update_account(aid, label=req.label, enabled=req.enabled)
    if not acc:
        raise HTTPException(404, "Account not found")
    return {k: v for k, v in acc.items() if k != "cookies"} | {
        "cookie_count": len(acc.get("cookies", {}))}


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "Account not found")
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """Actually open muse.ai to verify whether this account's cookies still log in."""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("cookies"):
        raise HTTPException(400, "This account has no cookies")

    def _probe():
        with GEN_LOCK:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"))
                synced = _sync_cookies(aid)
                quota = None
                try:  # also refresh quota; failure does not affect the test verdict
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"))
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception:  # noqa: BLE001
                    quota = None
                store.mark(aid, True, "Session valid")
                return {"ok": True, "message": "Session valid; generation should work",
                        "synced": synced, "quota": quota}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"Test inconclusive (account state kept): {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """Update one account's cookies (for topping up cookies after a session expires)."""
    cookies = dict(payload.get("cookies") or {})
    expires = dict(payload.get("expires") or {})
    if payload.get("cookie_header"):
        parsed_c, parsed_e = parse_cookie_payload(payload["cookie_header"])
        cookies.update(parsed_c)
        if parsed_e and not expires:
            expires.update(parsed_e)
    if not cookies:
        raise HTTPException(400, "No cookies parsed")
    exp = {k: int(v) for k, v in expires.items() if _pos(v)}
    # The top-up is a "fresh session's" cookies, so the expiry-estimate anchor must reset to now;
    # otherwise the old session's anchor carries over and understates the remaining days.
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="Cookies updated",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "Account not found")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No available accounts")
    try:
        engine.start()
        engine.refresh(acc["cookies"], acc.get("cookies_exp"))
        return {"ok": True, "account": acc["id"]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- Admin: quota -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """Read this account's quota from the muse.ai Settings panel (live) and cache it on the account record.

    Example return: {"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("cookies"):
        raise HTTPException(400, "This account has no cookies")

    def _probe():
        with GEN_LOCK:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"))
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Quota query failed: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """Query quota once with the least-recently-used available account (accounts in one pool usually share one muse.ai plan,
    so one check is enough for a single user; check per account with multiple accounts)."""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No available accounts")
    return await query_quota(acc["id"], _)


# ------------------------- Admin: access info / API key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """Generate a new API key, write it to .env, and apply it immediately (no restart)."""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "New key generated and applied; the old key is invalid -- update downstream projects"}


def _persist_env(key: str, value: str):
    """Write config back to .env (keeping other lines, atomic replace)."""
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


# ------------------------- Admin: fetching cookies -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """Return the local cookie-fetch helper script (advanced; requires Python)."""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "Helper script missing")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """Zip and return the browser extension (recommended; no CLI).

    Download, unzip, load in chrome://extensions developer mode, click once to import cookies.
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "Extension directory missing")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(src)):
            p = os.path.join(src, name)
            if os.path.isfile(p):
                z.write(p, os.path.join("muse2api-extension", name))
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """List extension directory contents (for the frontend)."""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "Extension directory missing")
    return {"files": sorted(f for f in os.listdir(src)
                            if os.path.isfile(os.path.join(src, f)))}


# ------------------------- Admin: tasks / media -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": store.list_tasks(limit)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "Task not found")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            items.append({"name": name, "url": media_url(name), "bytes": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        os.remove(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


# ------------------------- 前端页面 -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>Admin page file admin.html is missing.</p>"
                "<p>APIs available: <code>/v1/images/generations</code>, "
                "<code>/v1/videos</code></p></body>")


@app.get("/", response_class=HTMLResponse)
def index():
    return _admin_html()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _admin_html()



# ------------------------- Account auto-keepalive and silent renewal -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """Trigger the Meta gateway via muse.ai/api/session to issue fresh hatch_vml (+48h) / hatch_sess (+30d) and wake the cloud VM."""
    acc = store.get_account(aid)
    if not acc or not acc.get("cookies"):
        return {"ok": False, "id": aid, "label": (acc or {}).get("label", aid), "error": "Account has no valid cookies"}
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True)
        store.update_account(
            aid,
            cookies=res["cookies"],
            cookies_exp=res["cookies_exp"],
            ok=True if res.get("ok") else False,
            synced_at=int(time.time()),
        )
        vm_state = res.get("vm_state") or "RUNNING"
        store.touch_keepalive(aid, True, f"Session healthy - auto keepalive (VM: {vm_state})")
        quota = acc.get("quota")
        if check_quota and GEN_LOCK.acquire(blocking=False):
            try:
                engine.start()
                quota = engine.quota(res["cookies"], res["cookies_exp"])
                quota["checked_at"] = int(time.time())
                store.update_account(aid, quota=quota)
            except Exception as qe:  # noqa: BLE001
                log.warning("Failed to read quota for account %s: %s", aid, qe)
            finally:
                GEN_LOCK.release()
        updated = store.get_account(aid) or {}
        return {
            "ok": True,
            "id": aid,
            "label": acc.get("label", aid),
            "vm_id": res.get("vm_id"),
            "vm_state": vm_state,
            "wake_ok": res.get("wake_ok"),
            "expires_at": updated.get("expires_at"),
            "quota": quota,
        }
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"Keepalive auth failed: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        store.touch_keepalive(aid, None, f"Keepalive inconclusive (account state kept): {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """Run account keepalive: force=True refreshes all enabled accounts, otherwise only near-expiry/unchecked ones, keeping the VM warm."""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not exp_at
                    or (exp_at - now < 36 * 3600)
                    or ((now - last_ka) > 15 * 60)
                    or (a.get("ok") is not True)
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "Session healthy and recently kept alive"})
                    continue

                log.info("[auto-keepalive] Silent renewal + VM wake for account %s (%s)...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("[auto-keepalive] Account %s result: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """Silently pre-warm the browser and the first available account's WebSocket tunnel in the background, so the first request after restart is instant."""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    # Idle shutdown is freeing RAM on purpose; don't relaunch until real demand.
    idle_min = CFG.browser_idle_min
    if idle_min and idle_min > 0 and getattr(engine, "idle_stopped_at", 0):
        if time.time() - engine.idle_stopped_at < idle_min * 60:
            return
    acc = store.pick_account(rotate=False)
    if not acc or not acc.get("cookies"):
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    try:
        log.info("[browser warmup] Pre-warming hot tab for account %s (%s) in background...", acc.get("label"), acc["id"])
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("[browser warmup] Hot tab + WebSocket ready for account %s (%s)", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("[browser warmup] Warmup error: %s", exc)
    finally:
        GEN_LOCK.release()


async def _idle_reaper_loop():
    """Stop the browser after MUSE2API_BROWSER_IDLE_MIN minutes without generations (0 = disabled).

    Only fires while GEN_LOCK is free, so an in-flight generation can never be
    killed. Pure-HTTP keepalive probes don't touch the browser, so they neither
    reset the clock nor get disturbed; the next generation relaunches it."""
    idle_min = CFG.browser_idle_min
    if not idle_min or idle_min <= 0:
        return
    log.info("[browser idle shutdown] Enabled: browser stops after %d idle minutes", idle_min)
    while True:
        await asyncio.sleep(60)
        try:
            if GEN_LOCK.acquire(blocking=False):
                try:
                    engine.stop_if_idle(idle_min)
                finally:
                    GEN_LOCK.release()
        except Exception as e:  # noqa: BLE001
            log.warning("[browser idle shutdown] Reaper error: %s", e)


async def _keepalive_loop():
    """Resident background daemon: polls account health every 15 minutes and keeps the VM warm."""
    log.info("[auto-keepalive daemon] Started, check interval: 15 minutes")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_warmup_browser_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("[browser warmup] Error: %s", e)
    while True:
        try:
            await run_keepalive_all(force=False)
            if not getattr(engine, "current_acc_id", None) or engine.page is None:
                await asyncio.to_thread(_warmup_browser_sync)
        except Exception as e:  # noqa: BLE001
            log.error("[auto-keepalive daemon] Poll error: %s", e)
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """Admin manually triggers a keepalive renewal for all accounts."""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "Keepalive task already running; please wait"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """Manually run keepalive renewal for a single account."""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """Get the keepalive daemon status."""
    return KEEPALIVE_STATE


# ------------------------- Live repo update detection, notification, and one-click online upgrade -------------------------
REPO_URL = os.environ.get("MUSE2API_REPO_URL", "https://github.com/its-benjamin/muse2api")
UPSTREAM_REPO_URL = "https://github.com/czg86389-hub/muse2api"
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "config.py", "store.py", "cdp.py", "jev_dispatch.py",
    "admin.html", "README.md", "version.json", "requirements.txt",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools", "start-windows.bat", "start-windows.ps1", "run.py",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _read_env_key(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                s = ln.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _read_local_version() -> dict:
    p = os.path.join(BASE_DIR, "version.json")
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {"version": "1.5.0", "highlights": []}


def _installed_sha_file() -> str:
    return os.path.join(CFG.data_dir, ".installed_sha")


def _git(args: list[str], timeout: int = 30):
    import subprocess
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", f"safe.directory={BASE_DIR}", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(token: str = ""):
    """Ensure BASE_DIR is initialized as a git repo bound to its-benjamin/muse2api."""
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"https://x-access-token:{token}@github.com/its-benjamin/muse2api.git" if token else f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
        _git(["fetch", "origin", "main"], timeout=45)
        _git(["reset", "--mixed", "origin/main"])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", "its-benjamin"])
    _git(["config", "user.email", "its-benjamin@users.noreply.github.com"])

def _check_update_sync(force: bool = False) -> dict:
    """Check whether the official GitHub repo (czg86389-hub/muse2api) has new versions or commits.
    Cached for 90 seconds by default to avoid tripping GitHub API rate limits."""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and (now - _UPDATE_CACHE["ts"]) < 90:
        return _UPDATE_CACHE["data"]

    import requests
    token = _read_env_key("GITHUB_TOKEN")
    local_ver_obj = _read_local_version()
    local_version = str(local_ver_obj.get("version") or "1.5.0")

    has_git = os.path.isdir(os.path.join(BASE_DIR, ".git"))
    local_sha, local_msg, local_ts = "", "", 0
    if has_git:
        try:
            r_sha = _git(["rev-parse", "--short", "HEAD"])
            if r_sha.returncode == 0:
                local_sha = r_sha.stdout.strip()[:7]
            r_log = _git(["log", "-1", "--format=%s||%ct"])
            if r_log.returncode == 0 and "||" in r_log.stdout:
                parts = r_log.stdout.strip().split("||", 1)
                local_msg = parts[0]
                local_ts = int(parts[1])
        except Exception:
            pass

    if not local_sha and os.path.isfile(_installed_sha_file()):
        try:
            with open(_installed_sha_file(), encoding="utf-8") as f:
                local_sha = f.read().strip()[:7]
        except OSError:
            pass

    remote_version = local_version
    highlights = list(local_ver_obj.get("highlights") or [])
    try:
        rv = requests.get(
            f"https://raw.githubusercontent.com/its-benjamin/muse2api/main/version.json?t={int(now)}",
            timeout=6,
        )
        if rv.status_code == 200:
            rvj = rv.json()
            if isinstance(rvj, dict):
                remote_version = str(rvj.get("version") or remote_version)
                if rvj.get("highlights"):
                    highlights = list(rvj["highlights"])
    except Exception:
        pass

    remote_sha, remote_msg, remote_time = "", "", ""
    recent_commits = []
    try:
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "muse2api-updater"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(
            "https://api.github.com/repos/its-benjamin/muse2api/commits?sha=main&per_page=5",
            headers=headers,
            timeout=6,
        )
        if resp.status_code == 200 and isinstance(resp.json(), list):
            commits = resp.json()
            for idx, c in enumerate(commits):
                sha7 = (c.get("sha") or "")[:7]
                c_msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
                c_date = (((c.get("commit") or {}).get("committer") or {}).get("date") or "")
                c_url = c.get("html_url") or f"{REPO_URL}/commit/{sha7}"
                if idx == 0:
                    remote_sha = sha7
                    remote_msg = c_msg
                    remote_time = c_date
                recent_commits.append({
                    "sha": sha7,
                    "message": c_msg,
                    "date": c_date,
                    "url": c_url,
                })
    except Exception:
        pass

    # For Docker/ZIP installs (no .git) whose version matches on first run, record the baseline SHA
    if not local_sha and remote_sha and local_version == remote_version:
        local_sha = remote_sha
        try:
            with open(_installed_sha_file(), "w", encoding="utf-8") as f:
                f.write(remote_sha)
        except OSError:
            pass

    def _parse_v(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in re.findall(r"\d+", v))
        except Exception:
            return (0,)

    has_update = False
    if remote_sha and local_sha:
        has_update = (remote_sha != local_sha)
    elif remote_version and local_version:
        has_update = _parse_v(remote_version) > _parse_v(local_version)

    data = {
        "repo_url": REPO_URL,
        "has_git": has_git,
        "local_version": local_version,
        "remote_version": remote_version,
        "local_sha": local_sha,
        "local_msg": local_msg,
        "local_ts": local_ts,
        "remote_sha": remote_sha,
        "remote_msg": remote_msg,
        "remote_time": remote_time,
        "has_update": has_update,
        "up_to_date": not has_update and bool(remote_sha or remote_version),
        "highlights": highlights,
        "recent_commits": recent_commits,
        "checked_at": int(now),
    }
    _UPDATE_CACHE["ts"] = now
    _UPDATE_CACHE["data"] = data
    return data


def _upgrade_from_github_sync() -> dict:
    """Pull the latest code from GitHub over core files (works with or without git in Docker/ZIP envs); never touches .env or data/."""
    import requests
    import tarfile

    token = _read_env_key("GITHUB_TOKEN")
    upgraded_via = ""
    try:
        _ensure_git_repo(token)
        f_res = _git(["fetch", "origin", "main"], timeout=45)
        if f_res.returncode == 0:
            _git(["checkout", "-f", "origin/main", "--", "."])
            _git(["reset", "--mixed", "origin/main"])
            upgraded_via = "git"
    except Exception as e:
        log.warning("Git fetch for update failed; falling back to tarball update: %s", e)

    if not upgraded_via:
        resp = requests.get(
            "https://codeload.github.com/its-benjamin/muse2api/tar.gz/refs/heads/main",
            timeout=60,
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"Failed to download GitHub update bundle (HTTP {resp.status_code})")
        protected_files = {".env", "data/accounts.json", "data/tasks.json"}
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                if ".." in rel or rel in protected_files:
                    continue
                target_path = os.path.join(BASE_DIR, rel)
                if member.isdir():
                    os.makedirs(target_path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    fobj = tar.extractfile(member)
                    if fobj is not None:
                        with open(target_path, "wb") as out_f:
                            out_f.write(fobj.read())
        upgraded_via = "tarball"

    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True)
    if status.get("remote_sha"):
        try:
            with open(_installed_sha_file(), "w", encoding="utf-8") as f:
                f.write(status["remote_sha"])
            status["local_sha"] = status["remote_sha"]
            status["has_update"] = False
            status["up_to_date"] = True
        except OSError:
            pass
    return {
        "ok": True,
        "via": upgraded_via,
        "message": f"Updated to latest version {status.get('remote_version')} ({status.get('remote_sha')})",
        "status": status,
    }


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False):
    """Lets every deployed node check the official GitHub repo for new versions."""
    return await asyncio.to_thread(_check_update_sync, force)


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
async def admin_upgrade_now(payload: dict = Body(default={}), _=Depends(auth)):
    """One-click pull of the latest update from the official GitHub repo with automatic graceful restart."""
    res = await asyncio.to_thread(_upgrade_from_github_sync)
    restart = payload.get("restart", True) if isinstance(payload, dict) else True
    if restart:
        def _delayed_restart():
            time.sleep(0.5)
            try:
                engine.stop()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return res


@app.post("/admin/repo/push")
async def admin_repo_push(payload: dict = Body(default={}), _=Depends(auth)):
    """Maintainer-only: push this node's core code to the GitHub repo (auto-excludes .env and data/)."""
    msg = (payload.get("message") or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    new_token = (payload.get("github_token") or "").strip()
    if new_token:
        _persist_env("GITHUB_TOKEN", new_token)
        os.environ["GITHUB_TOKEN"] = new_token
    token = new_token or _read_env_key("GITHUB_TOKEN")

    def _do_push():
        _ensure_git_repo(token)
        existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
        _git(["add", "--", *existing_paths])
        st = _git(["status", "--porcelain", "--", *existing_paths])
        committed = False
        if st.stdout.strip():
            c_res = _git(["commit", "-m", msg])
            if c_res.returncode != 0:
                raise HTTPException(500, f"Git commit failed: {c_res.stderr or c_res.stdout}")
            committed = True
        p_res = _git(["push", "origin", "HEAD:main"], timeout=60)
        if p_res.returncode != 0:
            err = (p_res.stderr or p_res.stdout or "").strip()
            raise HTTPException(500, f"Git push failed: {err[:300]}")
        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)
        return {
            "ok": True,
            "committed": committed,
            "message": "Committed and pushed to the GitHub repo",
            "status": status,
        }

    return await asyncio.to_thread(_do_push)


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") == "image" and task.get("status") in ("queued", "processing"):
            store.update_task(task["id"], status="failed", error="Service restart interrupted the task; please resubmit")
    if not CFG.api_key:
        import secrets
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("No MUSE2API_KEY found; auto-generated initial key: %s", new_key)
    asyncio.create_task(_keepalive_loop())
    asyncio.create_task(_idle_reaper_loop())
    if _is_elevated():
        log.warning("Running as Administrator: Chrome will not stay attached "
                    "(it re-spawns de-elevated and the debug port never opens). "
                    "Restart from a NON-elevated terminal for the browser to work.")


@app.on_event("shutdown")
def _shutdown():
    engine.stop()
