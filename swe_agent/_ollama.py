"""Ollama-native /api/chat transport.

Extracted from the original ``swe_agent/llm.py``. The native endpoint accepts
``options.num_ctx``, streams tool calls, and honors ``keep_alive`` — none of
which the OpenAI-compatible /v1 path does.
"""
from __future__ import annotations

import json
import random
import time
from typing import Callable, List, Optional, Tuple

import requests

from .config import (BACKOFF_BASE, CONNECT_TIMEOUT, DEFAULT_NUM_CTX, DEFAULT_TEMPERATURE,
                     DEFAULT_TOP_P, KEEP_ALIVE, MAX_RETRIES, READ_TIMEOUT)

_session = requests.Session()


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


def check_server(base_url: str, model: str) -> Tuple[bool, str]:
    try:
        r = _session.get(base_url.rstrip("/") + "/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        return False, (f"Ollama server is not reachable at {base_url} ({e}).\n"
                       f"Start it with 'ollama serve'.")
    base = model.split(":")[0]
    if model in names or any(n.split(":")[0] == base for n in names):
        return True, "ok"
    return False, (f"Model '{model}' is not pulled. Available: {', '.join(names) or '(none)'}.\n"
                   f"Pull it with 'ollama pull {model}'.")
