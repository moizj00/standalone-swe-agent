"""LLM transport dispatcher.

The agent treats every model through a small surface — ``chat()``, ``normalize()``,
``extract_inline_tool_calls()``, ``check_server()`` — and this module routes the
calls to the right backend by inspecting the model name. ``claude-*`` goes to the
Anthropic Messages API; everything else stays on the Ollama-native /api/chat path
the original code was built around.

The Ollama and Anthropic transports live in ``swe_agent/_ollama.py`` and
``swe_agent/_anthropic.py`` respectively. Both speak the same in/out contract:

    chat(messages, model, tools, *, base_url, num_ctx, temperature, stream,
         on_token, use_tools) -> (content_str, raw_tool_calls_list_of_dict)

Returned ``raw_tool_calls`` are in the Ollama-native shape
(``[{function: {name, arguments}}, ...]``) so the existing ``normalize()`` works
unchanged whichever backend is in use.

Shared utilities — ``normalize`` and ``extract_inline_tool_calls`` — are backend
agnostic; they live here so both transports (and tests) import them from one place.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import List, Tuple

from . import _anthropic, _ollama


# --------------------------------------------------------------------------- backend selection

def _backend_for(model: str):
    """Pick the transport for ``model``. Override with ``SWE_AGENT_BACKEND``."""
    import os
    forced = (os.environ.get("SWE_AGENT_BACKEND") or "").strip().lower()
    if forced == "anthropic":
        return _anthropic
    if forced == "ollama":
        return _ollama
    return _anthropic if (model or "").startswith("claude-") else _ollama


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
    """Recover tool calls a small model put inside its text. Returns (calls, cleaned_content)."""
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

    deduped, seen = [], set()
    for c in found:
        key = (c["name"], json.dumps(c["arguments"], sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    cleaned = re.sub(r"```(?:json|tool_call|tool)?\s*\{.*?\}\s*```", "", content, flags=re.S).strip()
    return deduped, cleaned


def normalize(raw_tool_calls: List[dict]) -> List[dict]:
    return _normalize_tool_calls(raw_tool_calls)


# --------------------------------------------------------------------------- dispatch

def chat(messages, model, tools, **kw):
    return _backend_for(model).chat(messages, model, tools, **kw)


def check_server(base_url: str, model: str) -> Tuple[bool, str]:
    return _backend_for(model).check_server(base_url, model)
