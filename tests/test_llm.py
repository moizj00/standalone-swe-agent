"""Tests for the transport/parsing layer (swe_agent.llm)."""
from __future__ import annotations

from swe_agent.llm import extract_inline_tool_calls, normalize
from swe_agent.tools import VALID_NAMES

FENCED = '''Sure, I'll read it.
```json
{"name": "read_file", "arguments": {"path": "x.py"}}
```'''


def test_fenced_tool_call_recovered_once():
    """A fenced ```json block must yield exactly ONE call, not two.

    Regression: the fence regex and the bare-object scan both match the same
    payload, so without dedup the call is dispatched twice -- catastrophic for
    non-idempotent tools (git_commit, run_command, delete_file).
    """
    calls, cleaned = extract_inline_tool_calls(FENCED, VALID_NAMES)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == {"path": "x.py"}
    # The fenced block is stripped from the visible content.
    assert "read_file" not in cleaned
    assert cleaned.strip() == "Sure, I'll read it."


def test_bare_object_recovered():
    calls, _ = extract_inline_tool_calls(
        '{"name": "ls", "arguments": {"path": "."}}', VALID_NAMES)
    assert len(calls) == 1
    assert calls[0]["name"] == "ls"


def test_distinct_calls_both_kept():
    """Dedup is by (name, args) -- genuinely different calls survive."""
    content = (
        '{"name": "read_file", "arguments": {"path": "a.py"}}\n'
        '{"name": "read_file", "arguments": {"path": "b.py"}}'
    )
    calls, _ = extract_inline_tool_calls(content, VALID_NAMES)
    paths = sorted(c["arguments"]["path"] for c in calls)
    assert paths == ["a.py", "b.py"]


def test_prose_mentioning_json_does_not_misfire():
    """Random JSON that isn't a known tool name must be ignored."""
    calls, _ = extract_inline_tool_calls(
        'Here is config: {"name": "not_a_tool", "value": 3}', VALID_NAMES)
    assert calls == []


def test_alias_name_accepted():
    """`bash`/`shell` are aliases of run_command and are valid names."""
    calls, _ = extract_inline_tool_calls(
        '{"name": "bash", "arguments": {"command": "ls"}}', VALID_NAMES)
    assert len(calls) == 1
    assert calls[0]["name"] == "bash"


def test_parameters_key_accepted():
    """Some models emit `parameters` instead of `arguments`."""
    calls, _ = extract_inline_tool_calls(
        '{"name": "read_file", "parameters": {"path": "y.py"}}', VALID_NAMES)
    assert len(calls) == 1
    assert calls[0]["arguments"] == {"path": "y.py"}


def test_empty_content_is_safe():
    assert extract_inline_tool_calls("", VALID_NAMES) == ([], "")
    assert extract_inline_tool_calls(None, VALID_NAMES) == ([], None)


def test_normalize_native_calls():
    raw = [{"function": {"name": "read_file", "arguments": {"path": "z.py"}}}]
    out = normalize(raw)
    assert out[0]["name"] == "read_file"
    assert out[0]["arguments"] == {"path": "z.py"}
    assert out[0]["id"]  # an id is always assigned


def test_normalize_string_arguments_parsed():
    """Ollama sometimes serializes arguments as a JSON string."""
    raw = [{"function": {"name": "ls", "arguments": '{"path": "."}'}}]
    out = normalize(raw)
    assert out[0]["arguments"] == {"path": "."}


def test_normalize_bad_string_arguments_become_empty():
    raw = [{"function": {"name": "ls", "arguments": "not json"}}]
    out = normalize(raw)
    assert out[0]["arguments"] == {}


def test_normalize_skips_nameless_calls():
    assert normalize([{"function": {"arguments": {}}}]) == []
