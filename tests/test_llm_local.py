"""Hermetic tests for the LOCAL (native Ollama) transport path in swe_agent.llm.

Everything here mocks the requests Session / response objects -- no live Ollama
server and no network is ever touched. We exercise streaming JSON-lines parsing,
non-streaming parsing, malformed/garbage handling, mid-stream errors, retry &
backoff behavior, timeout/connection-refused mapping, num_ctx propagation, and
keep_alive.
"""
from __future__ import annotations

import json
from typing import List, Optional

import pytest
import requests

from swe_agent import llm
from swe_agent.config import (DEFAULT_NUM_CTX, KEEP_ALIVE, MAX_RETRIES,
                              CONNECT_TIMEOUT, READ_TIMEOUT)


# --------------------------------------------------------------------------- fakes

class FakeResponse:
    """Minimal stand-in for a requests.Response used as a context manager."""

    def __init__(self, *, lines: Optional[List[str]] = None, json_data=None,
                 status_code: int = 200, text: str = "",
                 raise_json: bool = False):
        self._lines = lines or []
        self._json_data = json_data
        self.status_code = status_code
        self.text = text
        self._raise_json = raise_json

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def json(self):
        if self._raise_json:
            raise ValueError("No JSON object could be decoded")
        return self._json_data


class FakeSession:
    """Records the last POST and returns queued responses (or raises)."""

    def __init__(self, responses):
        # responses: list of FakeResponse or Exception instances, consumed in order
        self._responses = list(responses)
        self.calls = []  # list of (url, payload, kwargs)

    def post(self, url, json=None, stream=False, timeout=None):
        self.calls.append({"url": url, "payload": json, "stream": stream,
                           "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make the retry backoff instant so tests stay fast."""
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)


def _stream_lines(*chunks) -> List[str]:
    return [json.dumps(c) for c in chunks]


# --------------------------------------------------------------------------- streaming

def test_streaming_concatenates_content_and_calls_on_token(monkeypatch):
    tokens = []
    resp = FakeResponse(lines=_stream_lines(
        {"message": {"content": "Hello "}},
        {"message": {"content": "world"}},
        {"message": {"content": ""}, "done": True},
    ))
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    content, calls = llm._do_request(
        "http://x/api/chat", {"stream": True}, tokens.append)
    assert content == "Hello world"
    assert calls == []
    assert tokens == ["Hello ", "world"]


def test_streaming_skips_garbage_and_partial_lines(monkeypatch):
    """A non-JSON / partial line must be skipped, not abort the whole stream."""
    resp = FakeResponse(lines=[
        json.dumps({"message": {"content": "a"}}),
        "",                      # blank line
        "{not valid json",       # garbage / truncated
        "[1, 2, 3]",             # valid JSON but not an object -> ignored
        json.dumps({"message": {"content": "b"}, "done": True}),
    ])
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    content, calls = llm._do_request("http://x/api/chat", {"stream": True}, None)
    assert content == "ab"
    assert calls == []


def test_streaming_accumulates_tool_calls_across_chunks(monkeypatch):
    resp = FakeResponse(lines=_stream_lines(
        {"message": {"content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}},
        {"message": {"content": "", "tool_calls": [
            {"function": {"name": "ls", "arguments": {"path": "."}}}]}, "done": True},
    ))
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    _, calls = llm._do_request("http://x/api/chat", {"stream": True}, None)
    assert [c["function"]["name"] for c in calls] == ["read_file", "ls"]
    norm = llm.normalize(calls)
    assert [c["name"] for c in norm] == ["read_file", "ls"]


def test_streaming_stops_at_done(monkeypatch):
    """Anything after a done=True chunk must be ignored."""
    resp = FakeResponse(lines=_stream_lines(
        {"message": {"content": "x"}, "done": True},
        {"message": {"content": "SHOULD-NOT-APPEAR"}},
    ))
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    content, _ = llm._do_request("http://x/api/chat", {"stream": True}, None)
    assert content == "x"


def test_streaming_midstream_error_raises(monkeypatch):
    """Ollama can report a fatal error mid-stream over HTTP 200."""
    resp = FakeResponse(lines=_stream_lines(
        {"message": {"content": "partial"}},
        {"error": "model requires more system memory"},
    ))
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    with pytest.raises(llm.OllamaError, match="more system memory"):
        llm._do_request("http://x/api/chat", {"stream": True}, None)


# --------------------------------------------------------------------------- non-streaming

def test_nonstreaming_parses_message(monkeypatch):
    resp = FakeResponse(json_data={"message": {
        "content": "done", "tool_calls": [
            {"function": {"name": "ls", "arguments": {"path": "."}}}]}})
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    content, calls = llm._do_request("http://x/api/chat", {"stream": False}, None)
    assert content == "done"
    assert calls[0]["function"]["name"] == "ls"


def test_nonstreaming_empty_message_is_safe(monkeypatch):
    resp = FakeResponse(json_data={})
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    content, calls = llm._do_request("http://x/api/chat", {"stream": False}, None)
    assert content == ""
    assert calls == []


def test_nonstreaming_error_field_raises(monkeypatch):
    resp = FakeResponse(json_data={"error": "model 'bogus' not found"})
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    with pytest.raises(llm.OllamaError, match="not found"):
        llm._do_request("http://x/api/chat", {"stream": False}, None)


def test_nonstreaming_non_json_body_raises(monkeypatch):
    resp = FakeResponse(raise_json=True, text="<html>502 Bad Gateway</html>",
                        status_code=200)
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    with pytest.raises(llm.OllamaError, match="non-JSON"):
        llm._do_request("http://x/api/chat", {"stream": False}, None)


def test_nonstreaming_non_dict_body_raises(monkeypatch):
    resp = FakeResponse(json_data=[1, 2, 3])
    monkeypatch.setattr(llm, "_session", FakeSession([resp]))
    with pytest.raises(llm.OllamaError, match="unexpected response"):
        llm._do_request("http://x/api/chat", {"stream": False}, None)


# --------------------------------------------------------------------------- HTTP status mapping

def test_http_4xx_is_nonretryable_ollama_error(monkeypatch):
    """A 404 (model not pulled) surfaces the body immediately, no retries."""
    resp = FakeResponse(status_code=404, json_data={"error": "model not found"})
    session = FakeSession([resp])
    monkeypatch.setattr(llm, "_session", session)
    with pytest.raises(llm.OllamaError, match="HTTP 404.*model not found"):
        llm.chat([], "m", [], base_url="http://x", stream=False)
    assert len(session.calls) == 1  # not retried


def test_http_500_is_retried_then_raises(monkeypatch):
    resps = [FakeResponse(status_code=500, text="boom") for _ in range(MAX_RETRIES)]
    session = FakeSession(resps)
    monkeypatch.setattr(llm, "_session", session)
    with pytest.raises(RuntimeError, match="failed after"):
        llm.chat([], "m", [], base_url="http://x", stream=False)
    assert len(session.calls) == MAX_RETRIES  # 5xx is transient -> retried


def test_http_429_is_retryable(monkeypatch):
    """Rate-limit (429) must be retried, then succeed."""
    ok = FakeResponse(json_data={"message": {"content": "ok"}})
    session = FakeSession([FakeResponse(status_code=429, text="slow down"), ok])
    monkeypatch.setattr(llm, "_session", session)
    content, _ = llm.chat([], "m", [], base_url="http://x", stream=False)
    assert content == "ok"
    assert len(session.calls) == 2


# --------------------------------------------------------------------------- retry/backoff

def test_transient_then_success(monkeypatch):
    ok = FakeResponse(json_data={"message": {"content": "recovered"}})
    session = FakeSession([requests.Timeout("read timed out"), ok])
    monkeypatch.setattr(llm, "_session", session)
    content, _ = llm.chat([], "m", [], base_url="http://x", stream=False)
    assert content == "recovered"
    assert len(session.calls) == 2


def test_backoff_grows_exponentially(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(llm.random, "uniform", lambda *_: 0.0)
    session = FakeSession([requests.Timeout("t") for _ in range(MAX_RETRIES)])
    monkeypatch.setattr(llm, "_session", session)
    with pytest.raises(RuntimeError):
        llm.chat([], "m", [], base_url="http://x", stream=False)
    # MAX_RETRIES attempts -> (MAX_RETRIES - 1) sleeps, doubling each time.
    assert len(sleeps) == MAX_RETRIES - 1
    assert sleeps == sorted(sleeps)
    assert sleeps[1] == pytest.approx(sleeps[0] * 2)


def test_connection_refused_gives_clear_message(monkeypatch):
    session = FakeSession([requests.ConnectionError("refused")] * MAX_RETRIES)
    monkeypatch.setattr(llm, "_session", session)
    with pytest.raises(RuntimeError, match="Cannot reach Ollama.*ollama serve"):
        llm.chat([], "m", [], base_url="http://localhost:11434", stream=False)


# --------------------------------------------------------------------------- payload shape

def test_payload_sets_num_ctx_keepalive_and_url(monkeypatch):
    ok = FakeResponse(json_data={"message": {"content": "hi"}})
    session = FakeSession([ok])
    monkeypatch.setattr(llm, "_session", session)
    llm.chat([{"role": "user", "content": "hi"}], "qwen2.5-coder:7b", [],
             base_url="http://localhost:11434/", num_ctx=16384, stream=False)
    call = session.calls[0]
    assert call["url"] == "http://localhost:11434/api/chat"  # trailing slash trimmed
    assert call["payload"]["options"]["num_ctx"] == 16384
    assert call["payload"]["keep_alive"] == KEEP_ALIVE
    assert call["timeout"] == (CONNECT_TIMEOUT, READ_TIMEOUT)
    assert "tools" not in call["payload"]  # no tools passed -> key omitted


def test_invalid_num_ctx_falls_back_to_default(monkeypatch):
    ok = FakeResponse(json_data={"message": {"content": "hi"}})
    session = FakeSession([ok, ok, ok])
    monkeypatch.setattr(llm, "_session", session)
    for bad in (0, -5, None):
        session.calls.clear()
        session._responses = [FakeResponse(json_data={"message": {"content": "x"}})]
        llm.chat([], "m", [], base_url="http://x", num_ctx=bad, stream=False)
        assert session.calls[0]["payload"]["options"]["num_ctx"] == DEFAULT_NUM_CTX


def test_tools_included_only_when_use_tools(monkeypatch):
    ok = FakeResponse(json_data={"message": {"content": "x"}})
    tools = [{"type": "function", "function": {"name": "ls"}}]

    session = FakeSession([ok])
    monkeypatch.setattr(llm, "_session", session)
    llm.chat([], "m", tools, base_url="http://x", stream=False, use_tools=True)
    assert session.calls[0]["payload"]["tools"] == tools

    session2 = FakeSession([FakeResponse(json_data={"message": {"content": "x"}})])
    monkeypatch.setattr(llm, "_session", session2)
    llm.chat([], "m", tools, base_url="http://x", stream=False, use_tools=False)
    assert "tools" not in session2.calls[0]["payload"]


def test_stream_flag_propagates_to_session(monkeypatch):
    resp = FakeResponse(lines=_stream_lines({"message": {"content": "s"}, "done": True}))
    session = FakeSession([resp])
    monkeypatch.setattr(llm, "_session", session)
    content, _ = llm.chat([], "m", [], base_url="http://x", stream=True)
    assert content == "s"
    assert session.calls[0]["stream"] is True
    assert session.calls[0]["payload"]["stream"] is True
