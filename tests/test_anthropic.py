"""Anthropic backend transport tests.

These tests never touch the real Anthropic API — they monkeypatch the module's
``_session.post`` to return canned bytes (for streaming) or canned JSON (for
non-streaming), then assert the translation layer and SSE parser produce the
same ``(content, raw_tool_calls)`` shape the Ollama backend would.
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import List

import pytest

from swe_agent import _anthropic, llm


# --------------------------------------------------------------------------- translation

def test_split_system_pulls_system_message_off_the_front():
    sys, conv = _anthropic._split_system([
        {"role": "system", "content": "you are X"},
        {"role": "user", "content": "hi"},
    ])
    assert sys == "you are X"
    assert conv == [{"role": "user", "content": "hi"}]


def test_split_system_handles_missing_system():
    sys, conv = _anthropic._split_system([{"role": "user", "content": "hi"}])
    assert sys == ""
    assert conv == [{"role": "user", "content": "hi"}]


def test_to_anthropic_tools_renames_parameters_to_input_schema():
    out = _anthropic._to_anthropic_tools([
        {"type": "function", "function": {
            "name": "read_file",
            "description": "read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }},
    ])
    assert out == [{
        "name": "read_file",
        "description": "read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }]


def test_to_anthropic_messages_round_trips_tool_use_and_tool_result():
    # Assistant turn that called two tools; the next two `tool` messages are the results.
    src = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "ok",
         "tool_calls": [
             {"function": {"name": "ls", "arguments": {"path": "."}}},
             {"function": {"name": "glob", "arguments": {"pattern": "*.py"}}},
         ]},
        {"role": "tool", "tool_name": "ls", "content": "[DIR] tests/"},
        {"role": "tool", "tool_name": "glob", "content": "swe_agent.py"},
    ]
    out = _anthropic._to_anthropic_messages(src)

    # user "list files"
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": "list files"}]}
    # assistant: text + two tool_use blocks
    asst = out[1]
    assert asst["role"] == "assistant"
    assert asst["content"][0] == {"type": "text", "text": "ok"}
    tool_uses = [b for b in asst["content"] if b["type"] == "tool_use"]
    assert [b["name"] for b in tool_uses] == ["ls", "glob"]
    assert tool_uses[0]["input"] == {"path": "."}
    ids = [b["id"] for b in tool_uses]
    assert len(set(ids)) == 2  # ids are unique
    # tool results: coalesced into ONE user message with two tool_result blocks
    results_msg = out[2]
    assert results_msg["role"] == "user"
    assert [b["type"] for b in results_msg["content"]] == ["tool_result", "tool_result"]
    # ids on tool_result blocks must match the assistant's tool_use ids in order
    assert [b["tool_use_id"] for b in results_msg["content"]] == ids
    assert [b["content"] for b in results_msg["content"]] == ["[DIR] tests/", "swe_agent.py"]


# --------------------------------------------------------------------------- SSE / response parsing

def _sse(events):
    """Encode (event_name, data_dict) pairs into a single bytes SSE stream."""
    chunks = []
    for name, data in events:
        chunks.append(f"event: {name}\n".encode())
        chunks.append(f"data: {json.dumps(data)}\n\n".encode())
    return b"".join(chunks)


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` used as a context manager."""

    def __init__(self, *, body=b"", json_payload=None, status_code=200):
        self.body = body
        self._json = json_payload
        self.status_code = status_code
        self.text = body.decode("utf-8", "replace") if body else ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def iter_lines(self, decode_unicode=False):
        # The SSE parser expects strings (decode_unicode=True is what _do_request passes).
        for line in self.body.split(b"\n"):
            yield line.decode("utf-8", "replace") if decode_unicode else line

    def json(self):
        return self._json


def test_streaming_text_and_tool_use_are_parsed(monkeypatch):
    body = _sse([
        ("message_start", {"type": "message_start", "message": {"id": "m1"}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hello"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": " world"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "tool_use", "id": "tu_x", "name": "ls", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"path":'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "input_json_delta", "partial_json": '"."}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_stop", {"type": "message_stop"}),
    ])

    captured = []
    monkeypatch.setattr(_anthropic._session, "post",
                        lambda *a, **kw: _FakeResponse(body=body))

    text, raw = _anthropic.chat(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}],
        "claude-sonnet-4-6", [], on_token=captured.append, stream=True,
    )
    assert text == "Hello world"
    assert captured == ["Hello", " world"]
    assert raw == [{"id": "tu_x", "function": {"name": "ls", "arguments": {"path": "."}}}]
    # And it round-trips through the dispatcher's normalize() unchanged.
    norm = llm.normalize(raw)
    assert norm[0]["name"] == "ls"
    assert norm[0]["arguments"] == {"path": "."}


def test_non_streaming_response_is_parsed(monkeypatch):
    monkeypatch.setattr(_anthropic._session, "post",
                        lambda *a, **kw: _FakeResponse(json_payload={
                            "content": [
                                {"type": "text", "text": "done"},
                                {"type": "tool_use", "id": "tu_y", "name": "glob",
                                 "input": {"pattern": "*.py"}},
                            ],
                        }))
    text, raw = _anthropic.chat(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}],
        "claude-sonnet-4-6", [], stream=False,
    )
    assert text == "done"
    assert raw == [{"id": "tu_y", "function": {"name": "glob", "arguments": {"pattern": "*.py"}}}]


def test_dispatcher_routes_claude_models_to_anthropic(monkeypatch):
    """The public ``llm.chat`` dispatches by model prefix without touching Ollama."""
    calls = {"anthropic": 0, "ollama": 0}

    def fake_anth(*a, **kw):
        calls["anthropic"] += 1
        return "ok", []

    def fake_oll(*a, **kw):
        calls["ollama"] += 1
        return "ok", []

    monkeypatch.setattr(_anthropic, "chat", fake_anth)
    monkeypatch.setattr("swe_agent._ollama.chat", fake_oll)

    llm.chat([], "claude-sonnet-4-6", [], base_url="")
    llm.chat([], "qwen2.5-coder:7b", [], base_url="")
    assert calls == {"anthropic": 1, "ollama": 1}


def test_dispatcher_respects_force_backend_env(monkeypatch):
    monkeypatch.setenv("SWE_AGENT_BACKEND", "anthropic")
    monkeypatch.setattr(_anthropic, "chat", lambda *a, **kw: ("via-anth", []))
    # Use a non-claude model name — should still be routed to anthropic.
    text, _ = llm.chat([], "qwen2.5-coder:7b", [], base_url="")
    assert text == "via-anth"


def test_check_server_reports_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, msg = _anthropic.check_server("", "claude-sonnet-4-6")
    assert ok is False
    assert "ANTHROPIC_API_KEY" in msg
