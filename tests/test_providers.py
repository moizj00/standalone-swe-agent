"""Tests for cloud provider registry (no API calls)."""

from swe_agent.providers import get_provider, is_cloud_provider
from swe_agent.providers.openai_compat import to_openai_messages


def test_cloud_providers_registered():
    for name in ("minimax", "kimi", "nemotron", "openai"):
        assert is_cloud_provider(name)
        assert get_provider(name) is not None


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