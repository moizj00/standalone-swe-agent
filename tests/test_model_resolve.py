"""Tests for Ollama model resolution (no server required)."""

from swe_agent import llm


def test_model_available_exact_and_base():
    available = ["qwen2.5-coder:7b"]
    assert llm.model_available("qwen2.5-coder:7b", available)
    assert llm.model_available("qwen2.5-coder:14b", available) is False


def test_exact_model_name_prefers_exact_tag():
    available = ["qwen2.5-coder:7b"]
    assert llm._exact_model_name("qwen2.5-coder:7b", available) == "qwen2.5-coder:7b"
    assert llm._exact_model_name("qwen2.5-coder:14b", available) is None