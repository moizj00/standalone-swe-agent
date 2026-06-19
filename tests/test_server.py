"""Tests for the HTTP/SSE bridge (swe_agent.server).

The agent is mocked via an injected agent_factory, so these run with no Ollama.
The server is started on an ephemeral port and driven with the `requests` client
(already a runtime dependency).
"""
from __future__ import annotations

import json
import threading

import pytest
import requests

from swe_agent.agent import Agent
from swe_agent.config import ApprovalMode
from swe_agent.server import (ServerConfig, build_server, gemini_tool_declarations,
                              translate_messages, _prime_agent)
from swe_agent.tools.base import ToolContext
from swe_agent.tools.exec import BackgroundRegistry


# ---- mock agent ------------------------------------------------------------

def echo_mock(messages):
    last_user = next((m.get("content") or "" for m in reversed(messages)
                      if m.get("role") == "user"), "")
    return "", [{"function": {"name": "task_complete",
                              "arguments": {"final_summary": f"echo: {last_user}"}}}]


def echo_factory(config, sid):
    ctx = ToolContext(cwd=config.cwd, approval=config.approval,
                      approve_cb=lambda *a: True, bg_registry=BackgroundRegistry())
    return Agent(model="mock", ctx=ctx, system_prompt="test", stream=False,
                 verbose=False, mock=echo_mock)


def _start(cfg: ServerConfig):
    httpd = build_server(cfg)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


@pytest.fixture
def srv(tmp_path):
    cfg = ServerConfig(host="127.0.0.1", port=0, cwd=tmp_path, persist=False,
                       agent_factory=echo_factory)
    httpd, base = _start(cfg)
    yield base
    httpd.shutdown(); httpd.server_close()


@pytest.fixture
def srv_token(tmp_path):
    cfg = ServerConfig(host="127.0.0.1", port=0, cwd=tmp_path, persist=False,
                       agent_factory=echo_factory, token="secret")
    httpd, base = _start(cfg)
    yield base
    httpd.shutdown(); httpd.server_close()


def _user(text):
    return {"messages": [{"role": "user", "parts": [{"text": text}]}]}


# ---- pure-unit ------------------------------------------------------------

def test_translate_messages_gemini_and_plain():
    out = translate_messages([
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
        {"role": "user", "content": "again"},
        {"role": "user", "parts": [{"text": "   "}]},  # dropped (empty)
    ])
    assert out == [("user", "hi"), ("assistant", "hello"), ("user", "again")]


def test_gemini_tool_declarations_uppercased():
    decls = gemini_tool_declarations()
    assert len(decls) > 0
    by_name = {d["name"]: d for d in decls}
    assert "read_file" in by_name
    params = by_name["read_file"]["parameters"]
    assert params["type"] == "OBJECT"
    assert params["properties"]["path"]["type"] == "STRING"


def test_prime_agent_replays_history_on_new_session(tmp_path):
    cfg = ServerConfig(cwd=tmp_path, persist=False)
    agent = echo_factory(cfg, "s1")
    entry = {"agent": agent}
    msgs = [("user", "first"), ("assistant", "ok"), ("user", "second")]
    text = _prime_agent(entry, msgs, created=True)
    assert text == "second"
    roles = [(m["role"], m["content"]) for m in agent.messages if m["role"] != "system"]
    assert ("user", "first") in roles and ("assistant", "ok") in roles
    assert roles[-1] == ("user", "second")


def test_prime_agent_existing_session_appends_only_last(tmp_path):
    cfg = ServerConfig(cwd=tmp_path, persist=False)
    agent = echo_factory(cfg, "s1")
    entry = {"agent": agent}
    _prime_agent(entry, [("user", "a"), ("assistant", "b"), ("user", "c")], created=False)
    non_system = [m for m in agent.messages if m["role"] != "system"]
    assert len(non_system) == 1 and non_system[0]["content"] == "c"


def test_prime_agent_rejects_non_user_last(tmp_path):
    cfg = ServerConfig(cwd=tmp_path, persist=False)
    agent = echo_factory(cfg, "s1")
    with pytest.raises(ValueError):
        _prime_agent({"agent": agent}, [("user", "a"), ("assistant", "b")], created=True)


# ---- HTTP -----------------------------------------------------------------

def test_health(srv):
    r = requests.get(srv + "/api/health", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["tools"] > 0
    assert body["approval"] == "read-only"


def test_tools_endpoint(srv):
    r = requests.get(srv + "/api/tools", timeout=10)
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert any(t["name"] == "write_file" for t in tools)


def test_chat_blocking(srv):
    r = requests.post(srv + "/api/chat", json=_user("hello"), timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert "echo: hello" in body["text"]
    assert body["session_id"]


def test_chat_rejects_non_user_last(srv):
    payload = {"messages": [{"role": "model", "parts": [{"text": "stray"}]}]}
    r = requests.post(srv + "/api/chat", json=payload, timeout=10)
    assert r.status_code == 400


def test_chat_stream_sse(srv):
    r = requests.post(srv + "/api/chat/stream", json=_user("streamed"),
                      stream=True, timeout=10)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("Content-Type", "")
    events = []
    for raw in r.iter_lines(decode_unicode=True):
        if raw and raw.startswith("data: "):
            events.append(json.loads(raw[6:]))
    types = [e["type"] for e in events]
    assert types[0] == "session"
    assert "tool_call" in types and "final" in types
    final = next(e for e in events if e["type"] == "final")
    assert "echo: streamed" in final["text"]


def test_session_id_is_stable_and_reused(srv):
    r1 = requests.post(srv + "/api/chat", json=_user("one"), timeout=10)
    sid = r1.json()["session_id"]
    payload = {"session_id": sid, "messages": [{"role": "user", "parts": [{"text": "two"}]}]}
    r2 = requests.post(srv + "/api/chat", json=payload, timeout=10)
    assert r2.json()["session_id"] == sid
    assert "echo: two" in r2.json()["text"]


def test_custom_tools_invalid_rejected(srv):
    payload = {"messages": [{"role": "user", "parts": [{"text": "hi"}]}],
               "custom_tools": [{"name": "9bad", "description": ""}]}
    r = requests.post(srv + "/api/chat", json=payload, timeout=10)
    assert r.status_code == 400
    assert "custom_tools" in r.json()["error"]


def test_custom_tools_internal_url_rejected(srv):
    payload = {"messages": [{"role": "user", "parts": [{"text": "hi"}]}],
               "custom_tools": [{"name": "x", "description": "d",
                                 "http": {"method": "GET", "url": "http://127.0.0.1/secret"}}]}
    r = requests.post(srv + "/api/chat", json=payload, timeout=10)
    assert r.status_code == 400


def test_custom_tools_valid_accepted(srv):
    payload = {"messages": [{"role": "user", "parts": [{"text": "hi"}]}],
               "custom_tools": [{"name": "get_x", "description": "d",
                                 "http": {"method": "GET", "url": "https://api.example.com/x"}}]}
    r = requests.post(srv + "/api/chat", json=payload, timeout=10)
    assert r.status_code == 200
    assert "echo: hi" in r.json()["text"]


def test_invalid_session_id_rejected(srv):
    payload = {"session_id": "../etc/passwd", "messages": [{"role": "user", "parts": [{"text": "x"}]}]}
    r = requests.post(srv + "/api/chat", json=payload, timeout=10)
    assert r.status_code == 400
    assert "session_id" in r.json()["error"]


def test_auth_required(srv_token):
    assert requests.get(srv_token + "/api/health", timeout=10).status_code == 401
    ok = requests.get(srv_token + "/api/health",
                      headers={"Authorization": "Bearer secret"}, timeout=10)
    assert ok.status_code == 200


def test_auth_wrong_token(srv_token):
    r = requests.post(srv_token + "/api/chat", json=_user("x"),
                      headers={"Authorization": "Bearer nope"}, timeout=10)
    assert r.status_code == 401
