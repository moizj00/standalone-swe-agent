"""Tests for Ollama model resolution and discovery (no server required)."""
from __future__ import annotations

import requests

from swe_agent import llm


# --------------------------------------------------------------------------- matching

def test_model_available_exact_and_base():
    available = ["qwen2.5-coder:7b"]
    assert llm.model_available("qwen2.5-coder:7b", available)
    assert llm.model_available("qwen2.5-coder:14b", available) is False


def test_model_available_bare_name_matches_any_tag():
    available = ["qwen2.5-coder:7b"]
    assert llm.model_available("qwen2.5-coder", available)


def test_model_available_empty_inputs():
    assert llm.model_available("", ["qwen2.5-coder:7b"]) is False
    assert llm.model_available("qwen2.5-coder:7b", []) is False


def test_exact_model_name_prefers_exact_tag():
    available = ["qwen2.5-coder:7b"]
    assert llm._exact_model_name("qwen2.5-coder:7b", available) == "qwen2.5-coder:7b"
    assert llm._exact_model_name("qwen2.5-coder:14b", available) is None


def test_exact_model_name_bare_resolves_to_first_tag():
    available = ["llama3:8b", "qwen2.5-coder:7b"]
    assert llm._exact_model_name("qwen2.5-coder", available) == "qwen2.5-coder:7b"


def test_model_base_strips_tag():
    assert llm._model_base("qwen2.5-coder:7b") == "qwen2.5-coder"
    assert llm._model_base("qwen2.5-coder") == "qwen2.5-coder"


# --------------------------------------------------------------------------- list_models

class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def test_list_models_parses_tags(monkeypatch):
    class S:
        def get(self, url, timeout=None):
            return _Resp({"models": [{"name": "qwen2.5-coder:7b"},
                                     {"name": "llama3:8b"}, {"name": ""}]})
    monkeypatch.setattr(llm, "_session", S())
    assert llm.list_models("http://x") == ["qwen2.5-coder:7b", "llama3:8b"]


def test_list_models_unreachable_returns_empty(monkeypatch):
    class S:
        def get(self, url, timeout=None):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(llm, "_session", S())
    assert llm.list_models("http://x") == []


# --------------------------------------------------------------------------- resolve_model

def test_resolve_model_server_down(monkeypatch):
    monkeypatch.setattr(llm, "list_models", lambda _: [])
    resolved, msg = llm.resolve_model("http://x", "qwen2.5-coder:7b")
    assert resolved is None
    assert "not reachable" in msg


def test_resolve_model_exact_hit(monkeypatch):
    monkeypatch.setattr(llm, "list_models", lambda _: ["qwen2.5-coder:7b"])
    resolved, msg = llm.resolve_model("http://x", "qwen2.5-coder:7b")
    assert resolved == "qwen2.5-coder:7b"
    assert msg == "ok"


def test_resolve_model_pinned_override_not_swapped(monkeypatch):
    monkeypatch.setattr(llm, "list_models", lambda _: ["llama3:8b"])
    monkeypatch.setenv("OLLAMA_AGENT_MODEL", "qwen2.5-coder:7b")
    resolved, msg = llm.resolve_model("http://x", "qwen2.5-coder:7b")
    assert resolved is None
    assert "not pulled" in msg


def test_resolve_model_falls_back_to_preference(monkeypatch):
    monkeypatch.delenv("OLLAMA_AGENT_MODEL", raising=False)
    monkeypatch.setattr(llm, "list_models", lambda _: ["qwen2.5-coder:7b"])
    resolved, msg = llm.resolve_model("http://x", "some-missing-model")
    assert resolved == "qwen2.5-coder:7b"
    assert "instead" in msg


# --------------------------------------------------------------------------- low_memory_hint

def test_low_memory_hint_warns_when_starved(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8000000 kB\nMemAvailable: 500000 kB\n")
    import builtins
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if path == "/proc/meminfo":
            return real_open(meminfo, *a, **k)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    hint = llm.low_memory_hint()
    assert hint is not None
    assert "Low available RAM" in hint


def test_low_memory_hint_silent_when_plenty(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 32000000 kB\nMemAvailable: 16000000 kB\n")
    import builtins
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if path == "/proc/meminfo":
            return real_open(meminfo, *a, **k)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    assert llm.low_memory_hint() is None


def test_low_memory_hint_no_proc_is_safe(monkeypatch):
    import builtins
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if path == "/proc/meminfo":
            raise OSError("no such file")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    assert llm.low_memory_hint() is None
