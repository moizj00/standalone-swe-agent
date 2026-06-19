"""LLM transport: native Ollama /api/chat with streaming, num_ctx, retries, and a
robust fallback parser for small models that emit tool calls as inline JSON.

We deliberately use the NATIVE endpoint (not the OpenAI-compatible /v1) because:
  - /v1 cannot set num_ctx (context silently caps at Ollama's ~4K default), and
  - /v1 has buggy streaming-with-tools.
The native endpoint accepts options.num_ctx, streams tool calls, and honors keep_alive.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .config import (BACKOFF_BASE, CONNECT_TIMEOUT, DEFAULT_NUM_CTX, DEFAULT_TEMPERATURE,
                     DEFAULT_TOP_P, KEEP_ALIVE, MAX_RETRIES, READ_TIMEOUT)

_session = requests.Session()


# --------------------------------------------------------------------------- normalization

def _normalize_tool_calls(raw: List[dict]) -> List[dict]:
    """Convert Ollama-native tool calls into {id, name, arguments(dict)} records."""
    out = []
    for tc in raw or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name")
        args = fn.get("arguments", tc.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name:
            out.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "name": name, "arguments": args})
    return out


def _scan_json_objects(text: str) -> List[str]:
    """Return all top-level brace-balanced JSON object substrings (string-aware)."""
    objs, depth, start = [], 0, None
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(text[start:i + 1])
                    start = None
    return objs


def extract_inline_tool_calls(content: str, valid_names) -> Tuple[List[dict], str]:
    """Recover tool calls a small model put inside its text. Returns (calls, cleaned_content).

    Only payloads whose name matches a registered tool are accepted, so prose that
    merely mentions JSON does not misfire.
    """
    if not content:
        return [], content
    candidates = []
    for m in re.finditer(r"```(?:json|tool_call|tool)?\s*(\{.*?\})\s*```", content, re.S):
        candidates.append(m.group(1))
    candidates.extend(_scan_json_objects(content))

    found = []
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or (obj.get("function") or {}).get("name")
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters")
        if args is None:
            args = obj.get("args", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name in valid_names:
            found.append({"id": f"inline_{uuid.uuid4().hex[:8]}", "name": name,
                          "arguments": args if isinstance(args, dict) else {}})

    # A fenced ```json {...}``` object is matched BOTH by the fence regex above and by
    # the bare-object scan, so the same call would otherwise be returned (and dispatched)
    # twice. Dedupe by (name, arguments) -- duplicate identical calls are never intended
    # and re-running a non-idempotent tool (commit, shell, delete) twice is harmful.
    deduped, seen = [], set()
    for c in found:
        key = (c["name"], json.dumps(c["arguments"], sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    cleaned = re.sub(r"```(?:json|tool_call|tool)?\s*\{.*?\}\s*```", "", content, flags=re.S).strip()
    return deduped, cleaned


# --------------------------------------------------------------------------- transport

def _do_request(url: str, payload: dict, on_token: Optional[Callable[[str], None]]) -> Tuple[str, List[dict]]:
    content_parts: List[str] = []
    tool_calls: List[dict] = []
    with _session.post(url, json=payload, stream=payload.get("stream", False),
                       timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
        r.raise_for_status()
        if payload.get("stream"):
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message") or {}
                piece = msg.get("content")
                if piece:
                    content_parts.append(piece)
                    if on_token:
                        on_token(piece)
                for tc in msg.get("tool_calls") or []:
                    tool_calls.append(tc)
                if chunk.get("done"):
                    break
        else:
            data = r.json()
            msg = data.get("message") or {}
            content_parts.append(msg.get("content") or "")
            tool_calls = msg.get("tool_calls") or []
    return "".join(content_parts), tool_calls


_RETRYABLE = (requests.ConnectionError, requests.Timeout, requests.HTTPError)


def chat(messages: List[dict], model: str, tools: List[dict], *,
         base_url: str, num_ctx: int = DEFAULT_NUM_CTX,
         temperature: float = DEFAULT_TEMPERATURE, stream: bool = True,
         on_token: Optional[Callable[[str], None]] = None,
         use_tools: bool = True) -> Tuple[str, List[dict]]:
    """Call the model once. Returns (content, raw_tool_calls). Retries transient errors."""
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "keep_alive": KEEP_ALIVE,
        "options": {"num_ctx": num_ctx, "temperature": temperature, "top_p": DEFAULT_TOP_P},
    }
    if use_tools and tools:
        payload["tools"] = tools

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return _do_request(url, payload, on_token)
        except _RETRYABLE as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.3))
    raise RuntimeError(f"Ollama request failed after {MAX_RETRIES} attempts: {last_err}")


def normalize(raw_tool_calls: List[dict]) -> List[dict]:
    return _normalize_tool_calls(raw_tool_calls)


def check_server(base_url: str, model: str) -> Tuple[bool, str]:
    """Pre-flight: is the server up and is the model pulled?"""
    try:
        r = _session.get(base_url.rstrip("/") + "/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        return False, (f"Ollama server is not reachable at {base_url} ({e}).\n"
                       f"Start it with 'ollama serve'.")
    # Ollama tags often include a :latest suffix; match loosely.
    base = model.split(":")[0]
    if model in names or any(n.split(":")[0] == base for n in names):
        return True, "ok"
    return False, (f"Model '{model}' is not pulled. Available: {', '.join(names) or '(none)'}.\n"
                   f"Pull it with 'ollama pull {model}'.")
