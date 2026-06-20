"""OpenAI-compatible chat API provider (cloud models)."""
from __future__ import annotations

import json
import random
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import requests

from ..config import BACKOFF_BASE, CONNECT_TIMEOUT, MAX_RETRIES, READ_TIMEOUT

_session = requests.Session()
_RETRYABLE = (requests.ConnectionError, requests.Timeout, requests.HTTPError)


def to_openai_messages(messages: List[dict]) -> List[dict]:
    """Convert internal agent messages to OpenAI chat/completions format."""
    out: List[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or m.get("tool_name") or "call",
                "content": m.get("content") or "",
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or tc
                args = fn.get("arguments", {})
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tcs.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": fn.get("name"),
                        "arguments": args if isinstance(args, str) else "{}",
                    },
                })
            out.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": tcs,
            })
            continue
        out.append({"role": role, "content": m.get("content") or ""})
    return out


def _merge_stream_tool_calls(chunks: List[dict]) -> List[dict]:
    """Accumulate OpenAI streaming tool_call deltas into complete records."""
    by_index: Dict[int, dict] = {}
    for tc in chunks:
        idx = tc.get("index", 0)
        slot = by_index.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = (slot["function"].get("name") or "") + fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] = (slot["function"].get("arguments") or "") + fn["arguments"]
    return [by_index[i] for i in sorted(by_index)]


class OpenAICompatibleProvider:
    def __init__(self, *, model: str, base_url: str, api_key: str):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def chat(
        self,
        messages: List[dict],
        tools: List[dict],
        *,
        temperature: float = 0.2,
        stream: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
        use_tools: bool = True,
    ) -> Tuple[str, List[dict]]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "temperature": temperature,
            "stream": stream,
        }
        if use_tools and tools:
            payload["tools"] = tools

        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                if not stream:
                    return self._chat_once(url, headers, payload)
                return self._chat_stream(url, headers, payload, on_token)
            except _RETRYABLE as e:
                last_err = e
                if attempt == MAX_RETRIES - 1:
                    break
                time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.3))
        raise RuntimeError(f"Cloud API request failed after {MAX_RETRIES} attempts: {last_err}")

    def _chat_once(self, url: str, headers: dict, payload: dict) -> Tuple[str, List[dict]]:
        r = _session.post(url, headers=headers, json=payload,
                          timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if not r.ok:
            detail = (r.text or "")[:300]
            raise requests.HTTPError(f"{r.status_code} {r.reason}: {detail}", response=r)
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return msg.get("content") or "", msg.get("tool_calls") or []

    def _chat_stream(
        self, url: str, headers: dict, payload: dict,
        on_token: Optional[Callable[[str], None]],
    ) -> Tuple[str, List[dict]]:
        content_parts: List[str] = []
        tool_chunks: List[dict] = []
        with _session.post(url, headers=headers, json=payload, stream=True,
                           timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                chunk_s = line[6:].strip()
                if chunk_s == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_s)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    if on_token:
                        on_token(piece)
                if delta.get("tool_calls"):
                    tool_chunks.extend(delta["tool_calls"])
        return "".join(content_parts), _merge_stream_tool_calls(tool_chunks)