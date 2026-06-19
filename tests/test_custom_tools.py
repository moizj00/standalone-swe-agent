"""Tests for user-defined custom HTTP tools (swe_agent.tools.custom) and their
dispatch through the Agent. Network is monkeypatched; the SSRF guard means a real
call to a test server would be blocked anyway."""
from __future__ import annotations

from pathlib import Path

import pytest

from swe_agent.agent import Agent
from swe_agent.config import ApprovalMode
from swe_agent.tools import custom
from swe_agent.tools.base import ToolContext
from swe_agent.tools.exec import BackgroundRegistry


def _fake_resp(status=200, text="ok", location=None):
    class R:
        pass
    r = R()
    r.status_code = status
    r.text = text
    r.headers = {"Location": location} if location else {}
    return r


def _ctx(tmp_path, approval=ApprovalMode.AUTO_ACCEPT):
    return ToolContext(cwd=tmp_path, approval=approval, approve_cb=lambda *a: True,
                       bg_registry=BackgroundRegistry())


def _agent(tmp_path, extra, approval=ApprovalMode.AUTO_ACCEPT):
    return Agent(model="m", ctx=_ctx(tmp_path, approval), system_prompt="s",
                 stream=False, verbose=False, mock=lambda m: ("", []), extra_tools=extra)


# ---- validation -----------------------------------------------------------

def test_validate_ok():
    assert custom.validate_def(
        {"name": "get_x", "description": "d", "http": {"method": "GET", "url": "https://api.example.com/x"}}) == []


def test_validate_bad_name():
    assert any("invalid tool name" in e for e in custom.validate_def({"name": "9x", "description": "d"}))


def test_validate_requires_description():
    assert any("description" in e for e in custom.validate_def({"name": "x", "description": ""}))


def test_validate_blocks_internal_url():
    errs = custom.validate_def({"name": "x", "description": "d",
                                "http": {"method": "GET", "url": "http://169.254.169.254/"}})
    assert any("SSRF" in e or "internal" in e for e in errs)


def test_validate_bad_method_and_location():
    errs = custom.validate_def({"name": "x", "description": "d",
                                "http": {"method": "FETCH", "url": "https://a.com",
                                         "param_location": {"p": "cookie"}}})
    assert any("method" in e for e in errs) and any("location" in e for e in errs)


def test_validate_rejects_shadow_of_builtin():
    errs = custom.validate_def({"name": "read_file", "description": "d"})
    assert any("shadow" in e for e in errs)


def test_validate_malformed_url_does_not_raise():
    # a non-numeric port used to raise ValueError out of ssrf_check; must now be a
    # collected validation error instead of tearing down the request.
    errs = custom.validate_def({"name": "x", "description": "d",
                                "http": {"method": "GET", "url": "http://example.com:notaport/"}})
    assert errs  # returned, not raised


def test_build_toolspecs_duplicate_and_cap():
    _, errs = custom.build_toolspecs([{"name": "a", "description": "d"}, {"name": "a", "description": "d"}])
    assert any("duplicate" in e for e in errs)
    _, errs = custom.build_toolspecs([{"name": f"t{i}", "description": "d"} for i in range(custom.MAX_TOOLS + 1)])
    assert any("too many" in e for e in errs)


# ---- request mapping ------------------------------------------------------

def _params(*names):
    return {"type": "object", "properties": {n: {"type": "string"} for n in names}}


def test_render_path_query_body_header():
    defn = {"name": "f", "description": "d", "parameters": _params("id", "q", "b", "h"), "http": {
        "method": "POST", "url": "https://api.x/{id}/do",
        "param_location": {"id": "path", "q": "query", "b": "body", "h": "header"}}}
    url, query, headers, body = custom._render(defn, {"id": "42", "q": "hi", "b": 1, "h": "H"})
    assert url == "https://api.x/42/do"
    assert query == {"q": "hi"} and body == {"b": 1} and headers.get("h") == "H"


def test_render_default_location_by_method():
    _, q, _, b = custom._render({"name": "f", "description": "d", "parameters": _params("a"), "http": {"method": "GET", "url": "https://api.x"}}, {"a": 1})
    assert q == {"a": 1} and b is None
    _, q, _, b = custom._render({"name": "f", "description": "d", "parameters": _params("a"), "http": {"method": "POST", "url": "https://api.x"}}, {"a": 1})
    assert q == {} and b == {"a": 1}


def test_render_filters_undeclared_args():
    defn = {"name": "f", "description": "d",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            "http": {"method": "GET", "url": "https://api.x"}}
    _, query, _, body = custom._render(defn, {"q": "ok", "admin": "true", "delete_all": True})
    assert query == {"q": "ok"} and body is None  # undeclared args dropped


def test_validate_path_param_without_placeholder():
    errs = custom.validate_def({"name": "x", "description": "d",
                                "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
                                "http": {"method": "DELETE", "url": "https://api.x/users",
                                         "param_location": {"id": "path"}}})
    assert any("placeholder" in e for e in errs)


def test_validate_url_placeholder_requires_param():
    errs = custom.validate_def({"name": "x", "description": "d", "parameters": _params(),
                                "http": {"method": "GET", "url": "https://api.x/users/{id}"}})
    assert any("placeholder" in e for e in errs)


def test_validate_rejects_host_placeholder():
    errs = custom.validate_def({"name": "x", "description": "d", "parameters": _params("host"),
                                "http": {"method": "GET", "url": "https://{host}/v1"}})
    assert any("host" in e for e in errs)


def test_origin_normalizes_default_port():
    from swe_agent.tools import _net
    assert _net._origin("https://h/x") == _net._origin("https://h:443/y")
    assert _net._origin("http://h/x") == _net._origin("http://h:80/y")


def test_required_argument_enforced(monkeypatch, tmp_path):
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return _fake_resp(200, "ok")

    monkeypatch.setattr(custom, "safe_request", fake)
    spec = custom.build_toolspec({"name": "f", "description": "d",
                                  "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                                  "http": {"method": "GET", "url": "https://api.x/{id}", "param_location": {"id": "path"}}})
    out = spec.impl(_ctx(tmp_path))  # required `id` omitted
    assert "missing required" in out and called["n"] == 0  # request never sent


def test_validate_non_dict_headers():
    errs = custom.validate_def({"name": "x", "description": "d",
                                "http": {"method": "GET", "url": "https://api.x", "headers": ["bad"]}})
    assert any("headers must be an object" in e for e in errs)


def test_build_toolspecs_non_dict_headers_no_crash():
    specs, errs = custom.build_toolspecs(
        [{"name": "x", "description": "d", "http": {"method": "GET", "url": "https://api.x", "headers": "oops"}}])
    assert errs and "x" not in specs  # reported as a validation error, not an AttributeError


def test_render_bearer_auth():
    _, _, headers, _ = custom._render(
        {"name": "f", "description": "d", "http": {"method": "GET", "url": "https://a", "auth": {"type": "bearer", "token": "T"}}}, {})
    assert headers["Authorization"] == "Bearer T"


# ---- executor -------------------------------------------------------------

def test_executor_calls_safe_request(monkeypatch, tmp_path):
    seen = {}

    def fake(method, url, **kw):
        seen.update(method=method, url=url, kw=kw)
        return _fake_resp(200, "BODY")

    monkeypatch.setattr(custom, "safe_request", fake)
    spec = custom.build_toolspec({"name": "f", "description": "d", "parameters": _params("id", "q"), "http": {
        "method": "GET", "url": "https://api.x/{id}", "param_location": {"id": "path"}}})
    out = spec.impl(_ctx(tmp_path), id="7", q="z")
    assert "HTTP 200" in out and "BODY" in out
    assert seen["url"] == "https://api.x/7" and seen["kw"]["params"] == {"q": "z"}


def test_executor_no_endpoint_stub(tmp_path):
    spec = custom.build_toolspec({"name": "f", "description": "d"})
    assert "no endpoint configured" in spec.impl(_ctx(tmp_path))


def test_executor_ssrf_refusal_surfaces(monkeypatch, tmp_path):
    spec = custom.build_toolspec({"name": "f", "description": "d", "http": {"method": "GET", "url": "http://example.com"}})

    def boom(*a, **k):
        raise ValueError("refused: x resolves to internal address (SSRF guard)")

    monkeypatch.setattr(custom, "safe_request", boom)
    out = spec.impl(_ctx(tmp_path))
    assert "Error calling" in out and "SSRF" in out


def test_response_redacts_configured_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(custom, "safe_request",
                        lambda method, url, **kw: _fake_resp(200, "you sent Bearer SUPERSECRET and key APIKEY123"))
    spec = custom.build_toolspec({"name": "f", "description": "d", "http": {
        "method": "GET", "url": "https://api.x",
        "headers": {"X-Api-Key": "APIKEY123"},
        "auth": {"type": "bearer", "token": "SUPERSECRET"}}})
    out = spec.impl(_ctx(tmp_path))
    assert "SUPERSECRET" not in out and "APIKEY123" not in out and "REDACTED" in out


def test_safe_request_strips_auth_and_body_on_cross_origin_redirect(monkeypatch):
    from swe_agent.tools import _net
    monkeypatch.setattr(_net, "ssrf_check", lambda url: None)  # avoid DNS in the test
    seen = []

    def fake_request(method, url, headers=None, **kw):
        seen.append((url, headers or {}, kw.get("json")))
        if len(seen) == 1:
            return _fake_resp(302, "", location="https://other.example/next")  # cross-origin
        return _fake_resp(200, "ok")

    monkeypatch.setattr(_net.requests, "request", fake_request)
    _net.safe_request("POST", "https://api.example/start",
                      headers={"Authorization": "Bearer T", "X-Api-Key": "K"},
                      json={"secret": "x"})
    # same-origin first hop keeps auth + body
    assert seen[0][1].get("Authorization") == "Bearer T" and seen[0][2] == {"secret": "x"}
    # cross-origin second hop: headers stripped, body dropped, default UA kept
    assert "Authorization" not in seen[1][1] and "X-Api-Key" not in seen[1][1]
    assert seen[1][2] is None and "User-Agent" in seen[1][1]


def test_executor_hides_transport_secrets(monkeypatch, tmp_path):
    spec = custom.build_toolspec({"name": "f", "description": "d", "http": {
        "method": "GET", "url": "https://api.x", "auth": {"type": "bearer", "token": "SECRET"}}})

    def boom(*a, **k):
        raise RuntimeError("connection failed with Authorization: Bearer SECRET")

    monkeypatch.setattr(custom, "safe_request", boom)
    out = spec.impl(_ctx(tmp_path))
    assert "SECRET" not in out and "request failed" in out


# ---- gating + dispatch ----------------------------------------------------

def test_custom_tool_is_exec_and_blocked_in_readonly(tmp_path):
    spec = custom.build_toolspec({"name": "f", "description": "d", "http": {"method": "GET", "url": "https://a"}})
    assert spec.category == "exec" and spec.mutating is True
    agent = _agent(tmp_path, {"f": spec}, approval=ApprovalMode.READ_ONLY)
    ok, msg = agent._gate(spec, "f", {})
    assert ok is False and "read-only" in msg


def test_custom_tool_allowed_in_auto_accept(tmp_path):
    spec = custom.build_toolspec({"name": "f", "description": "d", "http": {"method": "GET", "url": "https://a"}})
    ok, _ = _agent(tmp_path, {"f": spec})._gate(spec, "f", {})
    assert ok is True


def test_agent_dispatches_custom_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(custom, "safe_request", lambda method, url, **kw: _fake_resp(200, "PONG"))
    spec = custom.build_toolspec({"name": "ping_api", "description": "d", "http": {"method": "GET", "url": "https://api.x/ping"}})
    agent = _agent(tmp_path, {"ping_api": spec})
    obs = agent._dispatch({"name": "ping_api", "arguments": {}})
    assert "HTTP 200" in obs and "PONG" in obs


def test_unknown_tool_lists_custom_in_error(tmp_path):
    spec = custom.build_toolspec({"name": "known_api", "description": "d"})
    agent = _agent(tmp_path, {"known_api": spec}, approval=ApprovalMode.YOLO)
    obs = agent._dispatch({"name": "nope", "arguments": {}})
    assert "unknown tool" in obs and "known_api" in obs
