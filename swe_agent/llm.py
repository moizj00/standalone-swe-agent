"""LLM transport: native Ollama /api/chat with streaming, num_ctx, retries, and a
robust fallback parser for small models that emit tool calls as inline JSON.

We deliberately use the NATIVE endpoint (not the OpenAI-compatible /v1) because:
  - /v1 cannot set num_ctx (context silently caps at Ollama's ~4K default), and
  - /v1 has buggy streaming-with-tools.
The native endpoint accepts options.num_ctx, streams tool calls, and honors keep_alive.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .config import (BACKOFF_BASE, CONNECT_TIMEOUT, DEFAULT_NUM_CTX, DEFAULT_MODEL,
                     DEFAULT_TEMPERATURE, DEFAULT_TOP_P, KEEP_ALIVE, MAX_RETRIES,
                     MODEL_PREFERENCES, READ_TIMEOUT)

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
         use_tools: bool = True, provider: str = "ollama",
         api_key: str = "") -> Tuple[str, List[dict]]:
    """Call the model once. Returns (content, raw_tool_calls). Retries transient errors."""
    from .providers import OpenAICompatibleProvider, is_cloud_provider

    if is_cloud_provider(provider):
        cloud = OpenAICompatibleProvider(model=model, base_url=base_url, api_key=api_key)
        return cloud.chat(
            messages, tools, temperature=temperature, stream=stream,
            on_token=on_token, use_tools=use_tools,
        )

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


def list_models(base_url: str) -> List[str]:
    """Return model tags reported by the Ollama server (empty if unreachable)."""
    try:
        r = _session.get(base_url.rstrip("/") + "/api/tags", timeout=5)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
    except Exception:
        return []


def _model_base(name: str) -> str:
    """Strip the tag suffix for loose matching (qwen2.5-coder:7b -> qwen2.5-coder)."""
    return name.rsplit(":", 1)[0] if ":" in name else name


def model_available(model: str, available: List[str]) -> bool:
    """True when *model* is present in Ollama's tag list.

    A request that pins an explicit tag (``name:tag``) must match that exact tag —
    a different tag of the same base does not count. A request with no tag matches
    any tag of that base (loose matching)."""
    if not model or not available:
        return False
    if model in available:
        return True
    if ":" in model:
        return False  # explicit tag: exact match only
    base = _model_base(model)
    return any(_model_base(n) == base for n in available)


def _exact_model_name(model: str, available: List[str]) -> Optional[str]:
    """Return the concrete tag Ollama reports for a requested/base model name.

    An explicit tag (``name:tag``) only resolves when that exact tag is present;
    a bare base name resolves to the first available tag sharing that base."""
    if model in available:
        return model
    if ":" in model:
        return None  # explicit tag: exact match only
    base = _model_base(model)
    for name in available:
        if _model_base(name) == base:
            return name
    return None


def resolve_model(base_url: str, requested: str) -> Tuple[Optional[str], str]:
    """Pick a usable model tag. Returns (model_or_none, message)."""
    available = list_models(base_url)
    if not available:
        return None, (f"Ollama server is not reachable at {base_url}.\n"
                      f"Start it with 'ollama serve'.")

    exact = _exact_model_name(requested, available)
    if exact:
        return exact, "ok"

    # Honor an explicit user override even when it is missing — do not silently swap.
    if os.environ.get("OLLAMA_AGENT_MODEL"):
        return None, (
            f"Model '{requested}' is not pulled (OLLAMA_AGENT_MODEL is set).\n"
            f"Available: {', '.join(available)}.\n"
            f"Pull it with: ollama pull {requested}"
        )

    candidates = []
    seen = set()
    for name in [requested, *MODEL_PREFERENCES, *available]:
        if not name or name in seen:
            continue
        seen.add(name)
        candidates.append(name)

    for candidate in candidates:
        resolved = _exact_model_name(candidate, available)
        if resolved:
            if candidate != requested and _model_base(candidate) != _model_base(requested):
                return resolved, (
                    f"Model '{requested}' is not pulled; using '{resolved}' instead.\n"
                    f"For best coding + tool use, run: ollama pull {DEFAULT_MODEL}"
                )
            return resolved, (
                f"Model '{requested}' is not pulled; using '{resolved}' instead.\n"
                f"Pull the recommended coder model: ollama pull {DEFAULT_MODEL}"
            )

    return None, (
        f"No usable model found. Requested '{requested}'.\n"
        f"Available: {', '.join(available)}.\n"
        f"Recommended: ollama pull {DEFAULT_MODEL}"
    )


def check_server(base_url: str, model: str) -> Tuple[bool, str]:
    """Pre-flight: is the server up and is the model pulled?"""
    resolved, msg = resolve_model(base_url, model)
    if resolved:
        return True, msg
    return False, msg


def low_memory_hint() -> Optional[str]:
    """Return a user-facing warning when the host looks RAM-starved for a 7B model."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            info = {}
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0])  # kB
        available_kb = info.get("MemAvailable", info.get("MemFree", 0))
        if available_kb and available_kb < 1_500_000:  # < ~1.5 GiB free
            return (
                f"Low available RAM (~{available_kb // 1024} MiB). "
                "First model responses can take several minutes on CPU. "
                "Close other apps, set OLLAMA_NUM_CTX=4096, or use a smaller model."
            )
    except OSError:
        pass
    return None
