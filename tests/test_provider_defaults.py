"""Default provider resolution (no Ollama/network required).

DEFAULT_PROVIDER is computed at import time from SWE_AGENT_PROVIDER, so these
tests reload the config module under a patched environment and reload it back to
a clean state afterwards.
"""
from __future__ import annotations

import importlib

import swe_agent.config as config


def _reload_config():
    return importlib.reload(config)


def test_default_provider_is_ollama_when_env_unset(monkeypatch):
    monkeypatch.delenv("SWE_AGENT_PROVIDER", raising=False)
    cfg = _reload_config()
    try:
        assert cfg.DEFAULT_PROVIDER == "ollama"
    finally:
        monkeypatch.delenv("SWE_AGENT_PROVIDER", raising=False)
        importlib.reload(config)


def test_env_override_changes_provider(monkeypatch):
    monkeypatch.setenv("SWE_AGENT_PROVIDER", "openai")
    cfg = _reload_config()
    try:
        assert cfg.DEFAULT_PROVIDER == "openai"
    finally:
        monkeypatch.delenv("SWE_AGENT_PROVIDER", raising=False)
        importlib.reload(config)


def test_env_override_is_lowercased(monkeypatch):
    monkeypatch.setenv("SWE_AGENT_PROVIDER", "Nemotron")
    cfg = _reload_config()
    try:
        assert cfg.DEFAULT_PROVIDER == "nemotron"
    finally:
        monkeypatch.delenv("SWE_AGENT_PROVIDER", raising=False)
        importlib.reload(config)
