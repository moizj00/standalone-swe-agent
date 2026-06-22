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
# Only transient transport errors and retryable HTTP statuses (429 / 5xx, which we
# re-raise as requests.HTTPError) are retried. Client errors (4xx, auth, malformed
# bodies) raise CloudAPIError, which is NOT retryable so the clear message surfaces
# immediately instead of being buried after MAX_RETRIES attempts.
_RETRYABLE = (requests.ConnectionError, requests.Timeout, requests.HTTPError)


class CloudAPIError(RuntimeError):
    """Non-retryable error from an OpenAI-compatible cloud API.

    Covers 4xx client/auth errors, error bodies returned with HTTP 200, and
    malformed responses (missing choices). Kept out of ``_RETRYABLE`` so it
    propagates straight to the caller with a readable message.
    """


def _extract_api_error(data: dict) -> Optional[str]:
    """Return a human-readable message if the JSON body carries an API error."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        return err.get("message") or err.get("code") or json.dumps(err, ensure_ascii=False)
    return str(err)


def _check_http_status(r: "requests.Response") -> None:
    """Raise on a non-OK HTTP response, surfacing the API error body.

    429 and 5xx are raised as ``requests.HTTPError`` (retryable). All other 4xx
    errors raise ``CloudAPIError`` (non-retryable) with a tailored auth hint for
    401/403 so the user sees the real cause immediately.
    """
    if r.ok:
        return
    status = r.status_code
    detail = ""
    try:
        body = r.json()
        detail = _extract_api_error(body) or (r.text or "")
    except (ValueError, json.JSONDecodeError):
        detail = r.text or ""
    detail = (detail or "").strip()[:500]
    if status == 401:
        msg = f"Authentication failed (401): check your API key. {detail}".strip()
    elif status == 403:
        msg = f"Access forbidden (403): {detail}".strip()
    else:
        msg = f"{status} {r.reason}: {detail}".strip()
    if status == 429 or status >= 500:
        raise requests.HTTPError(msg, response=r)
    raise CloudAPIError(msg)


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
        _check_http_status(r)
        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise CloudAPIError(f"Cloud API returned non-JSON response: {(r.text or '')[:300]}") from e
        # Some OpenAI-compatible gateways return HTTP 200 with an {"error": ...} body.
        api_err = _extract_api_error(data)
        if api_err:
            raise CloudAPIError(f"Cloud API error: {api_err}")
        choices = data.get("choices")
        if not choices:
            raise CloudAPIError("Cloud API returned no choices in response.")
        msg = choices[0].get("message") or {}
        return msg.get("content") or "", msg.get("tool_calls") or []

    def _chat_stream(
        self, url: str, headers: dict, payload: dict,
        on_token: Optional[Callable[[str], None]],
    ) -> Tuple[str, List[dict]]:
        content_parts: List[str] = []
        tool_chunks: List[dict] = []
        with _session.post(url, headers=headers, json=payload, stream=True,
                           timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
            _check_http_status(r)
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                line = line.lstrip("\ufeff").strip()
                if not line.startswith("data:"):
                    # Ignore SSE comments/keep-alives and event/id fields.
                    continue
                chunk_s = line[5:].strip()  # tolerate both "data: {" and "data:{"
                if not chunk_s:
                    continue
                if chunk_s == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_s)
                except json.JSONDecodeError:
                    continue
                err = _extract_api_error(chunk)
                if err:
                    raise CloudAPIError(f"Cloud API stream error: {err}")
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    if on_token:
                        on_token(piece)
                if delta.get("tool_calls"):
                    tool_chunks.extend(delta["tool_calls"])
        return "".join(content_parts), _merge_stream_tool_calls(tool_chunks)