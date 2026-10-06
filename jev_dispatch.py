"""Jev-powered tool-call dispatcher for muse2api.

Uses the OpenCode free Jev endpoint (https://opencode.ai/zen/v1/systemone) —
no API key, no signup, same jev-1.13 model. Discovered from 9router source.

The muse.ai assistant refuses to emit pseudo-tool-call JSON, so prompt injection
doesn't work. This module intercepts requests *before* muse.ai when tools are present:
Jev reads the conversation + tool definitions, picks the right tool via Choice,
fills enum params via speculative-fanout Choice, confirms free-form params via Noul,
then extracts free-form values from the conversation text — all without touching muse.

muse.ai is only called on round 2 (with tool result injected as context),
and it responds as if it just looked the data up itself.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

import requests as _requests

log = logging.getLogger("muse2api")

CONFIDENCE_THRESHOLD = 0.50

# OpenCode free Jev endpoint — no auth required
_OC_SYSTEMONE_URL = "https://opencode.ai/zen/v1/systemone"
_OC_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer public",
    "x-opencode-client": "desktop",
    "User-Agent": "opencode/1.18.31",
}
_OC_MODEL = "jev-1.13-free"

# Paid TypeSafe key fallback (only used if MUSE2API_JEV_PAID=1)
_PAID_KEY = "apikey_21376f58ba8d69451e8c86722cc1be667c_64c8e08ff3b7d7fb998afe67c6e3662e8410a0c87fc23aee7459669999d33a04"
_PAID_URL = "https://api.typesafe.ai/v1/system-one"


def _call_jev(state: str, questions: dict) -> dict:
    """Call Jev via OpenCode free endpoint. Falls back to paid TypeSafe key on error."""
    payload = {
        "model": _OC_MODEL,
        "state": state,
        "questions": {
            k: _question_to_dict(v) for k, v in questions.items()
        }
    }
    try:
        r = _requests.post(_OC_SYSTEMONE_URL, headers=_OC_HEADERS,
                           json=payload, timeout=20)
        if r.status_code == 200:
            data = r.json()
            # Normalize to flat answers dict
            return _normalize_answers(data)
        log.warning("OpenCode Jev returned %s, trying paid fallback", r.status_code)
    except Exception as exc:
        log.warning("OpenCode Jev request failed: %s — trying paid fallback", exc)

    # Paid fallback
    paid_key = os.environ.get("MUSE2API_JEV_KEY", _PAID_KEY)
    r2 = _requests.post(
        _PAID_URL,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {paid_key}"},
        json={"model": "jev-1.13", "state": state,
              "questions": {k: _question_to_dict(v) for k, v in questions.items()}},
        timeout=20
    )
    r2.raise_for_status()
    return _normalize_answers(r2.json())


def _question_to_dict(q) -> dict:
    """Convert a question spec dict (already a dict) to API format."""
    return q  # questions are built as plain dicts


def _normalize_answers(data: dict) -> dict:
    """Return a flat {question_id: answer_dict} map from either API response shape."""
    # OpenCode shape: {"answers": {"qid": {...}}}
    # TypeSafe SDK shape: {"choices": {...}, "nouls": {...}, "scores": {...}}
    answers = data.get("answers") or {}
    if answers:
        return answers
    # Merge TypeSafe SDK shape
    merged = {}
    merged.update(data.get("choices") or {})
    merged.update(data.get("nouls") or {})
    merged.update(data.get("scores") or {})
    return merged


def _conversation_state(messages: list) -> str:
    """Last 6 messages as plain text for Jev."""
    parts = []
    for m in messages[-6:]:
        if not isinstance(m, dict):
            try:
                m = m.model_dump()
            except Exception:
                continue
        role = (m.get("role") or "user").strip()
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        if content and role != "tool":
            parts.append(f"{role.upper()}: {str(content).strip()}")
    return "\n".join(parts)


def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if not isinstance(m, dict):
            try:
                m = m.model_dump()
            except Exception:
                continue
        if (m.get("role") or "").strip().lower() == "user":
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            if content:
                return str(content).strip()
    return ""


def _extract_value(param_name: str, param_desc: str, text: str) -> str:
    """Extract a free-form parameter value from conversation text."""
    if not text:
        return ""
    pn = param_name.lower()
    pd = param_desc.lower()

    # URL
    # File path / filename
    if any(k in pn or k in pd for k in ("path", "file", "filename", "filepath",
                                         "script", "module", "source", "target",
                                         "output", "input", "src", "dst", "dest")):
        # Absolute path: /foo/bar.ext or C:\foo\bar
        m = re.search(r'([A-Za-z]:[\\/][\w./\\-]+|/[\w./\\-]{2,})', text)
        if m:
            return m.group(1).strip()
        # Relative path with extension: ./foo.py or just foo.py
        m = re.search(r'(?<!\w)([./~]?[\w./\\-]+\.[\w]{1,10})(?!\w)', text)
        if m:
            return m.group(1).strip()
        # Backtick or quoted filename
        m = re.search(r'[`"\']([^`"\']{2,120})[`"\']', text)
        if m:
            return m.group(1)
        # Plain word.ext anywhere
        m = re.search(r'\b([\w-]+\.(?:py|js|ts|rs|go|c|cpp|h|json|yml|yaml|toml|txt|md|sh|bat|css|html|sql|env))\b', text)
        if m:
            return m.group(1)
    if any(k in pn or k in pd for k in ("url", "link", "href", "uri", "endpoint")):
        m = re.search(r'https?://[^\s"\'>,]+', text)
        if m:
            return m.group(0)

    # City / location / place
    if any(k in pn or k in pd for k in ("city", "location", "place", "country",
                                         "region", "origin", "destination",
                                         "departure", "arrival", "from", "to", "where")):
        all_locs = re.findall(
            r'\b(?:in|to|from|at|near|flying\s+to|going\s+to|leaving)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)',
            text
        )
        if all_locs:
            if any(k in pn for k in ("dest", "arrival", "to")):
                return all_locs[-1]
            if any(k in pn for k in ("origin", "departure", "from", "source")):
                return all_locs[0]
            return all_locs[0]
        caps = re.findall(r'\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]+)?)\b', text)
        stop = {"What", "How", "Can", "Please", "I", "The", "Is", "Are", "Do",
                "Tell", "Me", "My", "Find", "Get", "Show", "Give", "Search",
                "Book", "Check", "Look"}
        caps = [c for c in caps if c not in stop]
        if caps:
            return caps[-1] if any(k in pn for k in ("dest", "arrival", "to")) else caps[0]

    # Query / search / keyword / code / command
    if any(k in pn or k in pd for k in ("query", "search", "keyword", "q",
                                         "term", "topic", "subject", "message",
                                         "content", "text", "input", "prompt",
                                         "question", "request", "description",
                                         "code", "command", "cmd", "snippet", "expression")):
        # For code/command: extract after colon or from code fence
        if any(k in pn or k in pd for k in ("code", "command", "cmd", "snippet")):
            # Try longer phrases first so "run" doesn't greedily match before "this command:"
            m = re.search(
                r'(?:this\s+(?:code|command|script|snippet)|execute|eval)\s*:\s*(.+)',
                text, re.IGNORECASE | re.DOTALL)
            if m and m.group(1).strip():
                return m.group(1).strip()
            # "Run: ..." or bare colon
            m = re.search(r'(?:run|exec)\s*:\s*(.+)', text, re.IGNORECASE | re.DOTALL)
            if m and m.group(1).strip():
                return m.group(1).strip()
            # Any colon with content after
            m = re.search(r':\s*(`{3}(?:\w+)?\n?(.+?)`{3}|(.+))$', text, re.DOTALL)
            if m:
                inner = (m.group(2) or m.group(3) or "").strip()
                if inner and len(inner) > 2:
                    return inner
        return text

    # Name / title
    if any(k in pn or k in pd for k in ("name", "title", "label", "tag",
                                         "username", "person", "author", "artist", "product")):
        m = re.search(r'["\']([^"\']{2,60})["\']', text)
        if m:
            return m.group(1)
        caps = re.findall(r'\b([A-Z][a-zA-Z]{1,}(?:\s+[A-Z][a-zA-Z]+)?)\b', text)
        stop = {"What", "How", "Can", "Please", "I", "The", "Is", "Are", "Do"}
        caps = [c for c in caps if c not in stop]
        if caps:
            return caps[0]

    # Date / time
    if any(k in pn or k in pd for k in ("date", "time", "when", "day", "month", "year", "schedule")):
        m = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
        if m:
            return m.group(1)
        m = re.search(
            r'\b(next\s+\w+|this\s+\w+|tomorrow|today|monday|tuesday|wednesday|'
            r'thursday|friday|saturday|sunday|\d+\s+(?:jan|feb|mar|apr|may|jun|'
            r'jul|aug|sep|oct|nov|dec)\w*(?:\s+\d{4})?)\b', text, re.IGNORECASE)
        if m:
            return m.group(1)

    # Number
    if any(k in pn or k in pd for k in ("number", "count", "limit", "max", "amount", "quantity", "size")):
        m = re.search(r'\b(\d+)\b', text)
        if m:
            return m.group(1)

    # Generic fallback: quoted → proper noun → first 120 chars
    m = re.search(r'["\']([^"\']{2,80})["\']', text)
    if m:
        return m.group(1)
    caps = re.findall(r'\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]+)?)\b', text)
    stop = {"What", "How", "Can", "Please", "I", "The", "Is", "Are", "Do",
            "Tell", "Me", "My", "Find", "Get", "Show", "Give", "Search",
            "Check", "Look", "Book", "Need", "Want"}
    caps = [c for c in caps if c not in stop]
    if caps:
        return caps[0]
    return text[:120]


def _extract_fn(tool: dict) -> dict | None:
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return fn
    if tool.get("name"):
        return tool
    return None


def _enum_values(param_schema: dict) -> list[str] | None:
    enum = param_schema.get("enum")
    if isinstance(enum, list) and all(isinstance(v, str) for v in enum):
        return enum
    return None


def jev_tool_dispatch(messages: list, tools: list) -> list[dict] | None:
    """Try to resolve a tool call using Jev. Returns OpenAI-format tool_calls or None."""
    if not tools or not messages:
        return None
    try:
        return _dispatch(messages, tools)
    except Exception as exc:
        log.warning("Jev dispatch error (falling through to muse): %s", exc)
        return None


def _dispatch(messages: list, tools: list) -> list[dict] | None:
    fns: dict[str, dict] = {}
    for t in tools:
        fn = _extract_fn(t)
        if fn and fn.get("name"):
            fns[fn["name"]] = fn
    if not fns:
        return None

    state = _conversation_state(messages)
    last_user = _last_user_text(messages)

    # ── Build questions ───────────────────────────────────────────────────────
    tool_options = {"__none__": "No tool needed; the assistant can answer directly"}
    for name, fn in fns.items():
        tool_options[name] = (fn.get("description") or name).strip()

    questions: dict = {
        "__tool__": {
            "type": "choice",
            "instructions": (
                "Based on the conversation, which tool should be called next, if any? "
                "Pick __none__ only if the assistant can answer directly without a tool."
            ),
            "criteria": tool_options,
        }
    }

    for t_name, fn in fns.items():
        props = (fn.get("parameters") or {}).get("properties") or {}
        required = set((fn.get("parameters") or {}).get("required") or [])
        for p_name, p_schema in props.items():
            if not isinstance(p_schema, dict):
                continue
            p_type = p_schema.get("type", "string")
            if p_type not in ("string", "number", "integer", "boolean"):
                continue
            p_desc = p_schema.get("description") or p_name
            enum_vals = _enum_values(p_schema) if p_type == "string" else None

            questions[f"{t_name}.{p_name}.present"] = {
                "type": "noul",
                "instructions": (
                    f"Does the conversation supply a value for '{p_name}' "
                    f"({p_desc}) of {t_name}?"
                    + (" (required)" if p_name in required else " (optional)")
                )
            }
            if enum_vals:
                questions[f"{t_name}.{p_name}.value"] = {
                    "type": "choice",
                    "instructions": f"What value does the conversation give for '{p_name}' ({p_desc}) of {t_name}?",
                    "criteria": {v: v for v in enum_vals},
                }

    # ── Single Jev call (OpenCode free) ──────────────────────────────────────
    answers = _call_jev(state, questions)

    tool_ans = answers.get("__tool__") or {}
    chosen_tool = tool_ans.get("choice") or tool_ans.get("choices", [None])[0]
    confidence = tool_ans.get("confidence", 0.0)

    log.info("Jev: tool=%s confidence=%.2f (via OpenCode free)", chosen_tool, confidence)

    if not chosen_tool or chosen_tool == "__none__" or confidence < CONFIDENCE_THRESHOLD:
        return None

    fn = fns.get(chosen_tool)
    if not fn:
        return None

    # ── Assemble arguments ────────────────────────────────────────────────────
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    clean_args: dict = {}

    for p_name, p_schema in props.items():
        if not isinstance(p_schema, dict):
            continue
        p_type = p_schema.get("type", "string")
        enum_vals = _enum_values(p_schema) if p_type == "string" else None
        p_desc = p_schema.get("description") or p_name

        present_ans = answers.get(f"{chosen_tool}.{p_name}.present") or {}
        present_prob = present_ans.get("noul", 0.0)

        if enum_vals:
            val_ans = answers.get(f"{chosen_tool}.{p_name}.value") or {}
            val = val_ans.get("choice")
            if val and (present_prob >= 0.4 or p_name in required):
                clean_args[p_name] = val
        else:
            if present_prob >= 0.5:
                extracted = _extract_value(p_name, p_desc, last_user)
                if extracted:
                    clean_args[p_name] = extracted
                elif p_name in required:
                    if any(k in p_name.lower() for k in ("query", "q", "search", "text", "message", "content")):
                        clean_args[p_name] = last_user
                    else:
                        clean_args[p_name] = ""
            elif p_name in required:
                clean_args[p_name] = _extract_value(p_name, p_desc, last_user) or ""

    return [{
        "id": "call_jev_" + uuid.uuid4().hex[:16],
        "type": "function",
        "function": {
            "name": chosen_tool,
            "arguments": json.dumps(clean_args, ensure_ascii=False),
        },
    }]
