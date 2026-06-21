"""Anthropic Messages API transport.

Speaks the same ``chat() -> (content, raw_tool_calls)`` contract as the Ollama
backend (see ``swe_agent/_ollama.py``) so ``swe_agent.llm.normalize`` and the
rest of the agent loop don't need to care which LLM is behind the model.

The shape work happens at the edges:
- inbound:  Ollama-flavored messages + Ollama-flavored tool schema → Anthropic
- outbound: Anthropic Messages API response → Ollama-native tool_calls shape
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

from .config import (BACKOFF_BASE, CONNECT_TIMEOUT, DEFAULT_TEMPERATURE,
                     MAX_RETRIES, READ_TIMEOUT)

ANTHROPIC_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# Generous default; tool-using agents need headroom for long single turns.
DEFAULT_MAX_TOKENS = 4096

_session = requests.Session()


# --------------------------------------------------------------------------- translation

def _split_system(messages: List[dict]) -> Tuple[str, List[dict]]:
    """Anthropic takes `system` as a top-level field, not a message."""
    if messages and messages[0].get("role") == "system":
        return messages[0].get("content", "") or "", messages[1:]
    return "", list(messages)


def _to_anthropic_tools(tools: List[dict]) -> List[dict]:
    """Map Ollama-style ``{type:function, function:{name, description, parameters}}``
    to Anthropic's ``{name, description, input_schema}``."""
    out = []
    for t in tools or []:
        fn = t.get("function") or t
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": params,
        })
    return out


def _to_anthropic_messages(conv: List[dict]) -> List[dict]:
    """Translate the agent's running conversation into Anthropic's content-block shape.

    Agent stores:
      - {role: "user", content: str}
      - {role: "assistant", content: str, tool_calls?: [{function: {name, arguments}}]}
      - {role: "tool", tool_name: str, content: str}      ← reply for the prior assistant turn

    Anthropic expects:
      - {role: "user",      content: [{type: "text", text}]}
      - {role: "assistant", content: [{type: "text", text}, {type: "tool_use", id, name, input}, ...]}
      - {role: "user",      content: [{type: "tool_result", tool_use_id, content}, ...]}

    Tool-use ids aren't stored on the agent's assistant message, so we synthesize
    deterministic ones from message position. The matching tool_result blocks in
    the next user message use the same ids by walking the source list in order.
    """
    out: List[dict] = []
    pending_ids: List[str] = []
    for idx, m in enumerate(conv):
        role = m.get("role")
        if role == "user":
            if pending_ids:
                # Should not happen if the loop is well-formed; clear and move on.
                pending_ids = []
            out.append({"role": "user", "content": [{"type": "text", "text": m.get("content") or ""}]})
        elif role == "assistant":
            blocks: List[dict] = []
            text = m.get("content") or ""
            if text:
                blocks.append({"type": "text", "text": text})
            ids_this_turn: List[str] = []
            for j, tc in enumerate(m.get("tool_calls") or []):
                fn = tc.get("function") or tc
                tu_id = f"tu_{idx}_{j}"
                blocks.append({
                    "type": "tool_use",
                    "id": tu_id,
                    "name": fn.get("name"),
                    "input": fn.get("arguments") or {},
                })
                ids_this_turn.append(tu_id)
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            pending_ids = ids_this_turn
        elif role == "tool":
            # One tool result per pending id, in arrival order. Anthropic requires
            # that tool_result blocks live in a single user message immediately
            # following the assistant message that issued the tool_use blocks.
            tu_id = pending_ids.pop(0) if pending_ids else f"tu_{idx}_0"
            result = {"type": "tool_result", "tool_use_id": tu_id, "content": m.get("content") or ""}
            # Coalesce consecutive tool results into one user message.
            if out and out[-1]["role"] == "user" and out[-1]["content"] \
                    and out[-1]["content"][-1].get("type") == "tool_result":
                out[-1]["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


# --------------------------------------------------------------------------- transport

def _request_headers() -> Dict[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _do_request(payload: dict, on_token: Optional[Callable[[str], None]]) -> Tuple[str, List[dict]]:
    """POST /v1/messages, parse SSE if streaming, return (text, ollama-native tool_calls)."""
    url = ANTHROPIC_BASE + "/v1/messages"
    streaming = bool(payload.get("stream"))
    with _session.post(url, json=payload, headers=_request_headers(),
                       stream=streaming, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
        r.raise_for_status()
        if streaming:
            return _consume_sse(r, on_token)
        data = r.json()
        return _from_message_json(data)


def _from_message_json(data: dict) -> Tuple[str, List[dict]]:
    text_parts: List[str] = []
    tool_calls: List[dict] = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "function": {"name": block.get("name"), "arguments": block.get("input") or {}},
            })
    return "".join(text_parts), tool_calls


def _consume_sse(response: requests.Response, on_token: Optional[Callable[[str], None]]) -> Tuple[str, List[dict]]:
    """Parse Anthropic's SSE stream.

    Emits text deltas to ``on_token`` and accumulates ``tool_use`` blocks
    (concatenating ``input_json_delta`` fragments). Returns the final
    ``(text, tool_calls)`` once ``message_stop`` arrives.
    """
    text_parts: List[str] = []
    # block_index -> partial state for an in-flight content block.
    inflight: Dict[int, dict] = {}
    tool_calls: List[dict] = []

    event_type: Optional[str] = None
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw == "":
            event_type = None
            continue
        if raw.startswith("event:"):
            event_type = raw[6:].strip()
            continue
        if not raw.startswith("data:"):
            continue
        data_str = raw[5:].strip()
        if not data_str:
            continue
        try:
            evt = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        etype = event_type or evt.get("type")
        if etype == "content_block_start":
            block = evt.get("content_block") or {}
            inflight[evt.get("index", 0)] = {
                "type": block.get("type"),
                "id": block.get("id"),
                "name": block.get("name"),
                "input_json": "",
            }
        elif etype == "content_block_delta":
            delta = evt.get("delta") or {}
            idx = evt.get("index", 0)
            state = inflight.get(idx)
            if delta.get("type") == "text_delta":
                piece = delta.get("text") or ""
                if piece:
                    text_parts.append(piece)
                    if on_token:
                        on_token(piece)
            elif delta.get("type") == "input_json_delta" and state is not None:
                state["input_json"] += delta.get("partial_json") or ""
        elif etype == "content_block_stop":
            idx = evt.get("index", 0)
            state = inflight.pop(idx, None)
            if state and state.get("type") == "tool_use":
                try:
                    args = json.loads(state["input_json"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": state.get("id"),
                    "function": {"name": state.get("name"), "arguments": args},
                })
        elif etype == "message_stop":
            break
        elif etype == "error":
            err = evt.get("error") or {}
            raise RuntimeError(f"Anthropic stream error: {err.get('type')}: {err.get('message')}")
    return "".join(text_parts), tool_calls


_RETRYABLE = (requests.ConnectionError, requests.Timeout, requests.HTTPError)


def chat(messages: List[dict], model: str, tools: List[dict], *,
         base_url: str = "", num_ctx: int = 0,
         temperature: float = DEFAULT_TEMPERATURE, stream: bool = True,
         on_token: Optional[Callable[[str], None]] = None,
         use_tools: bool = True) -> Tuple[str, List[dict]]:
    """Same signature as the Ollama transport; ``base_url`` and ``num_ctx`` are ignored."""
    system, conv = _split_system(messages)
    payload: Dict[str, object] = {
        "model": model,
        "system": system,
        "messages": _to_anthropic_messages(conv),
        "max_tokens": int(os.environ.get("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
        "temperature": temperature,
        "stream": stream,
    }
    if use_tools and tools:
        payload["tools"] = _to_anthropic_tools(tools)

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return _do_request(payload, on_token)
        except _RETRYABLE as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.3))
    raise RuntimeError(f"Anthropic request failed after {MAX_RETRIES} attempts: {last_err}")


def check_server(base_url: str, model: str) -> Tuple[bool, str]:
    """Cheap auth check: a 1-token request that fails fast if the key is wrong."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, ("ANTHROPIC_API_KEY is not set. Export it before starting the agent.")
    try:
        r = _session.post(
            ANTHROPIC_BASE + "/v1/messages",
            headers=_request_headers(),
            json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=10,
        )
    except Exception as e:
        return False, f"Anthropic API is not reachable ({e})."
    if r.status_code == 200:
        return True, "ok"
    if r.status_code in (401, 403):
        return False, "Anthropic rejected the API key (401/403). Check ANTHROPIC_API_KEY."
    # Other failures (e.g. unknown model -> 400) come back with a JSON error body.
    try:
        msg = (r.json().get("error") or {}).get("message") or r.text[:200]
    except Exception:
        msg = r.text[:200]
    return False, f"Anthropic preflight failed ({r.status_code}): {msg}"
