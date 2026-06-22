"""Tests for cloud provider registry and OpenAI-compatible transport (no network).

All HTTP is mocked by monkeypatching ``openai_compat._session.post`` so these
tests never touch the network.
"""

import json

import pytest
import requests

from swe_agent import llm
from swe_agent.providers import (
    check_cloud_provider,
    get_provider,
    is_cloud_provider,
)
from swe_agent.providers import openai_compat
from swe_agent.providers.openai_compat import (
    CloudAPIError,
    OpenAICompatibleProvider,
    _merge_stream_tool_calls,
    to_openai_messages,
)


# --------------------------------------------------------------------------- fakes

class FakeResponse:
    """Minimal stand-in for requests.Response for both JSON and streaming paths."""

    def __init__(self, *, status_code=200, reason="OK", json_body=None,
                 text=None, lines=None):
        self.status_code = status_code
        self.reason = reason
        self._json_body = json_body
        self._lines = lines or []
        if text is not None:
            self.text = text
        elif json_body is not None:
            self.text = json.dumps(json_body)
        else:
            self.text = ""

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_provider():
    return OpenAICompatibleProvider(
        model="m", base_url="https://example.test/v1", api_key="sk-test"
    )


def _patch_post(monkeypatch, response, capture=None):
    def fake_post(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture["kwargs"] = kwargs
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(openai_compat._session, "post", fake_post)


# --------------------------------------------------------------------------- registry

def test_cloud_providers_registered():
    for name in ("minimax", "kimi", "nemotron", "openai"):
        assert is_cloud_provider(name)
        assert get_provider(name) is not None


def test_unknown_provider_not_cloud():
    assert not is_cloud_provider("definitely-not-real")
    assert get_provider("definitely-not-real") is None
    assert get_provider("") is None


def test_check_cloud_provider_missing_key(monkeypatch):
    # Clear every env var that could supply an OpenAI key.
    for var in ("OPENAI_API_KEY",):
        monkeypatch.delenv(var, raising=False)
    ok, msg = check_cloud_provider("openai")
    assert ok is False
    assert "OPENAI_API_KEY" in msg
    assert "requires an API key" in msg


def test_check_cloud_provider_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    ok, msg = check_cloud_provider("openai")
    assert ok is True
    assert "ok" in msg


def test_check_cloud_provider_alias_key(monkeypatch):
    for var in ("MOONSHOT_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "via-alias")
    ok, msg = check_cloud_provider("kimi")
    assert ok is True


def test_check_cloud_provider_unknown():
    ok, msg = check_cloud_provider("nope")
    assert ok is False
    assert "Unknown cloud provider" in msg


def test_nemotron_dummy_key_fallback(monkeypatch):
    for var in ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NGC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    ok, msg = check_cloud_provider("nemotron")
    assert ok is True
    assert "placeholder key" in msg


# --------------------------------------------------------------------------- to_openai_messages

def test_to_openai_messages_tool_role():
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "call_abc", "function": {"name": "ls", "arguments": {"path": "."}}},
        ]},
        {"role": "tool", "tool_call_id": "call_abc", "tool_name": "ls", "content": "ok"},
    ]
    out = to_openai_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "."}'
    assert out[1]["tool_call_id"] == "call_abc"


def test_to_openai_messages_plain_roles():
    out = to_openai_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ])
    assert [m["role"] for m in out] == ["system", "user", "assistant"]
    assert out[1]["content"] == "hi"


def test_to_openai_messages_tool_call_string_args_preserved():
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "sh", "arguments": '{"cmd": "ls"}'}},
        ]},
    ]
    out = to_openai_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"cmd": "ls"}'


def test_to_openai_messages_tool_id_fallback():
    # Tool message lacking tool_call_id should fall back to tool_name.
    out = to_openai_messages([
        {"role": "tool", "tool_name": "ls", "content": "x"},
    ])
    assert out[0]["tool_call_id"] == "ls"


def test_to_openai_messages_assistant_missing_id_gets_one():
    out = to_openai_messages([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "ls", "arguments": {}}},
        ]},
    ])
    tc_id = out[0]["tool_calls"][0]["id"]
    assert tc_id and tc_id.startswith("call_")


# --------------------------------------------------------------------------- streaming tool-call assembly

def test_merge_stream_tool_calls_fragments():
    chunks = [
        {"index": 0, "id": "call_1", "function": {"name": "se", "arguments": ""}},
        {"index": 0, "function": {"name": "arch", "arguments": '{"q":'}},
        {"index": 0, "function": {"arguments": ' "x"}'}},
    ]
    merged = _merge_stream_tool_calls(chunks)
    assert len(merged) == 1
    assert merged[0]["id"] == "call_1"
    assert merged[0]["function"]["name"] == "search"
    assert merged[0]["function"]["arguments"] == '{"q": "x"}'


def test_merge_stream_tool_calls_multiple_indices():
    chunks = [
        {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}},
        {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}},
    ]
    merged = _merge_stream_tool_calls(chunks)
    assert [m["function"]["name"] for m in merged] == ["one", "two"]


# --------------------------------------------------------------------------- non-streaming chat

def test_chat_non_streaming_content_and_tools(monkeypatch):
    body = {
        "choices": [{
            "message": {
                "content": "hello",
                "tool_calls": [
                    {"id": "x", "type": "function",
                     "function": {"name": "ls", "arguments": '{"path": "."}'}},
                ],
            },
        }],
    }
    capture = {}
    _patch_post(monkeypatch, FakeResponse(json_body=body), capture)
    content, raw = _make_provider().chat([{"role": "user", "content": "hi"}], [],
                                         stream=False)
    assert content == "hello"
    # Shape must be consumable by llm.normalize().
    normed = llm.normalize(raw)
    assert normed[0]["name"] == "ls"
    assert normed[0]["arguments"] == {"path": "."}
    # Auth header + URL plumbed correctly.
    assert capture["url"] == "https://example.test/v1/chat/completions"
    assert capture["kwargs"]["headers"]["Authorization"] == "Bearer sk-test"
    assert capture["kwargs"]["json"]["model"] == "m"


def test_chat_non_streaming_empty_content(monkeypatch):
    body = {"choices": [{"message": {"content": None}}]}
    _patch_post(monkeypatch, FakeResponse(json_body=body))
    content, raw = _make_provider().chat([], [], stream=False)
    assert content == ""
    assert raw == []


def test_chat_use_tools_false_omits_tools(monkeypatch):
    capture = {}
    _patch_post(monkeypatch, FakeResponse(json_body={"choices": [{"message": {"content": "x"}}]}), capture)
    _make_provider().chat([], [{"type": "function"}], stream=False, use_tools=False)
    assert "tools" not in capture["kwargs"]["json"]


def test_chat_use_tools_true_includes_tools(monkeypatch):
    capture = {}
    tools = [{"type": "function", "function": {"name": "ls"}}]
    _patch_post(monkeypatch, FakeResponse(json_body={"choices": [{"message": {"content": "x"}}]}), capture)
    _make_provider().chat([], tools, stream=False, use_tools=True)
    assert capture["kwargs"]["json"]["tools"] == tools


def test_chat_temperature_plumbed(monkeypatch):
    capture = {}
    _patch_post(monkeypatch, FakeResponse(json_body={"choices": [{"message": {"content": "x"}}]}), capture)
    _make_provider().chat([], [], stream=False, temperature=0.9)
    assert capture["kwargs"]["json"]["temperature"] == 0.9


# --------------------------------------------------------------------------- error handling

def test_chat_401_raises_non_retryable_auth_message(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return FakeResponse(status_code=401, reason="Unauthorized",
                            json_body={"error": {"message": "bad key"}})
    monkeypatch.setattr(openai_compat._session, "post", fake_post)
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=False)
    assert "401" in str(exc.value)
    assert "bad key" in str(exc.value)
    # Must NOT retry a 401.
    assert calls["n"] == 1


def test_chat_400_surfaces_error_body_no_retry(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return FakeResponse(status_code=400, reason="Bad Request",
                            json_body={"error": {"message": "model not found"}})
    monkeypatch.setattr(openai_compat._session, "post", fake_post)
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=False)
    assert "model not found" in str(exc.value)
    assert calls["n"] == 1


def test_chat_500_is_retried_then_fails(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(openai_compat.time, "sleep", lambda *a, **k: None)

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return FakeResponse(status_code=500, reason="Server Error",
                            json_body={"error": {"message": "boom"}})
    monkeypatch.setattr(openai_compat._session, "post", fake_post)
    with pytest.raises(RuntimeError) as exc:
        _make_provider().chat([], [], stream=False)
    assert "after" in str(exc.value)
    assert calls["n"] == openai_compat.MAX_RETRIES


def test_chat_429_is_retryable(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(openai_compat.time, "sleep", lambda *a, **k: None)

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return FakeResponse(status_code=429, reason="Too Many Requests",
                            json_body={"error": {"message": "rate"}})
    monkeypatch.setattr(openai_compat._session, "post", fake_post)
    with pytest.raises(RuntimeError):
        _make_provider().chat([], [], stream=False)
    assert calls["n"] == openai_compat.MAX_RETRIES


def test_chat_connection_error_retried(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(openai_compat.time, "sleep", lambda *a, **k: None)

    def fake_post(url, **kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("down")
    monkeypatch.setattr(openai_compat._session, "post", fake_post)
    with pytest.raises(RuntimeError):
        _make_provider().chat([], [], stream=False)
    assert calls["n"] == openai_compat.MAX_RETRIES


def test_chat_200_with_error_body(monkeypatch):
    body = {"error": {"message": "quota exceeded"}}
    _patch_post(monkeypatch, FakeResponse(status_code=200, json_body=body))
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=False)
    assert "quota exceeded" in str(exc.value)


def test_chat_missing_choices(monkeypatch):
    _patch_post(monkeypatch, FakeResponse(json_body={"choices": []}))
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=False)
    assert "no choices" in str(exc.value)


def test_chat_non_json_body(monkeypatch):
    _patch_post(monkeypatch, FakeResponse(status_code=200, json_body=None,
                                          text="<html>oops</html>"))
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=False)
    assert "non-JSON" in str(exc.value)


# --------------------------------------------------------------------------- streaming

def _sse(obj):
    return "data: " + json.dumps(obj)


def test_chat_streaming_content_and_tokens(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {"content": "Hel"}}]}),
        "",  # keep-alive blank line
        _sse({"choices": [{"delta": {"content": "lo"}}]}),
        "data: [DONE]",
    ]
    _patch_post(monkeypatch, FakeResponse(lines=lines))
    tokens = []
    content, raw = _make_provider().chat([], [], stream=True,
                                         on_token=tokens.append)
    assert content == "Hello"
    assert tokens == ["Hel", "lo"]
    assert raw == []


def test_chat_streaming_tool_calls_assembled(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "ls", "arguments": ""}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path"'}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ': "."}'}}]}}]}),
        "data: [DONE]",
    ]
    _patch_post(monkeypatch, FakeResponse(lines=lines))
    content, raw = _make_provider().chat([], [], stream=True)
    normed = llm.normalize(raw)
    assert normed[0]["name"] == "ls"
    assert normed[0]["arguments"] == {"path": "."}


def test_chat_streaming_no_space_after_data(monkeypatch):
    # Some gateways emit "data:{...}" without a space.
    lines = [
        "data:" + json.dumps({"choices": [{"delta": {"content": "x"}}]}),
        "data:[DONE]",
    ]
    _patch_post(monkeypatch, FakeResponse(lines=lines))
    content, raw = _make_provider().chat([], [], stream=True)
    assert content == "x"


def test_chat_streaming_ignores_comments_and_bad_json(monkeypatch):
    lines = [
        ": keep-alive comment",
        "event: message",
        _sse({"choices": [{"delta": {"content": "ok"}}]}),
        "data: {not valid json",
        "data: [DONE]",
    ]
    _patch_post(monkeypatch, FakeResponse(lines=lines))
    content, raw = _make_provider().chat([], [], stream=True)
    assert content == "ok"


def test_chat_streaming_http_error_surfaces_body(monkeypatch):
    _patch_post(monkeypatch, FakeResponse(status_code=401, reason="Unauthorized",
                                          json_body={"error": {"message": "nope"}}))
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=True)
    assert "401" in str(exc.value)
    assert "nope" in str(exc.value)


def test_chat_streaming_error_in_stream(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {"content": "partial"}}]}),
        _sse({"error": {"message": "mid-stream failure"}}),
    ]
    _patch_post(monkeypatch, FakeResponse(lines=lines))
    with pytest.raises(CloudAPIError) as exc:
        _make_provider().chat([], [], stream=True)
    assert "mid-stream failure" in str(exc.value)


def test_chat_streaming_empty_choices_chunk_skipped(monkeypatch):
    lines = [
        _sse({"choices": []}),  # e.g. initial role/usage chunk
        _sse({"choices": [{"delta": {"content": "y"}}]}),
        "data: [DONE]",
    ]
    _patch_post(monkeypatch, FakeResponse(lines=lines))
    content, raw = _make_provider().chat([], [], stream=True)
    assert content == "y"


# --------------------------------------------------------------------------- base_url normalization

def test_base_url_trailing_slash_stripped():
    p = OpenAICompatibleProvider(model="m", base_url="https://x.test/v1/", api_key="k")
    assert p.base_url == "https://x.test/v1"
