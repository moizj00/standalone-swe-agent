"""Tests for the core agent loop (swe_agent.agent.Agent) driven by a mock model."""
from __future__ import annotations

from conftest import make_agent, scripted

from swe_agent.tools import ADVERTISED


def test_task_complete_ends_turn_with_summary(ctx):
    mock = scripted(("", [("task_complete", {"final_summary": "all done"})]))
    agent = make_agent(ctx, mock)
    out = agent.run_turn()
    assert "all done" in out
    # Exactly one model step was consumed.
    assert agent.steps == 1


def test_no_tool_calls_ends_turn_with_content(ctx):
    mock = scripted(("here is my answer", []))
    agent = make_agent(ctx, mock)
    out = agent.run_turn()
    assert out == "here is my answer"


def test_multi_step_tool_then_complete(ctx):
    (ctx.cwd / "hello.txt").write_text("hi", encoding="utf-8")
    mock = scripted(
        ("reading first", [("read_file", {"path": "hello.txt"})]),
        ("now finishing", [("task_complete", {"final_summary": "read the file"})]),
    )
    agent = make_agent(ctx, mock)
    out = agent.run_turn()
    assert "read the file" in out
    assert agent.steps == 2
    # The tool observation was appended to history.
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert any(m["tool_name"] == "read_file" for m in tool_msgs)


def test_write_tool_actually_writes(ctx):
    mock = scripted(
        ("", [("write_file", {"path": "out.txt", "content": "data"})]),
        ("", [("task_complete", {"final_summary": "wrote it"})]),
    )
    agent = make_agent(ctx, mock)
    agent.run_turn()
    assert (ctx.cwd / "out.txt").read_text(encoding="utf-8") == "data"


def test_files_changed_rendered_in_summary(ctx):
    mock = scripted(("", [("task_complete", {
        "final_summary": "done", "confidence": "high",
        "files_changed": ["a.py", "b.py"]})]))
    out = make_agent(ctx, mock).run_turn()
    assert "a.py" in out and "b.py" in out
    assert "Confidence: high" in out


def test_max_steps_reached(ctx):
    # Always asks to read a file, never completes.
    def never_done(messages):
        return "", [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]

    agent = make_agent(ctx, never_done, max_steps=3)
    out = agent.run_turn()
    assert "max steps" in out.lower()
    assert agent.steps == 3


def test_unknown_tool_reported(ctx):
    agent = make_agent(ctx, scripted())
    result = agent._dispatch({"name": "does_not_exist", "arguments": {}})
    assert "unknown tool" in result.lower()
    assert ADVERTISED[0] in result  # lists available tools


def test_inline_recovery_in_loop(ctx):
    """When the model emits no native calls but text contains a tool call,
    the loop recovers and dispatches it."""
    (ctx.cwd / "f.txt").write_text("content", encoding="utf-8")
    mock = scripted(
        ('```json\n{"name": "read_file", "arguments": {"path": "f.txt"}}\n```', []),
        ("", [("task_complete", {"final_summary": "ok"})]),
    )
    agent = make_agent(ctx, mock)
    out = agent.run_turn()
    assert "ok" in out
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert any(m["tool_name"] == "read_file" for m in tool_msgs)


def test_observation_truncation(ctx):
    from swe_agent.config import MAX_OBSERVATION_CHARS
    big = "x" * (MAX_OBSERVATION_CHARS + 5000)
    (ctx.cwd / "big.txt").write_text(big, encoding="utf-8")
    agent = make_agent(ctx, scripted())
    obs = agent._dispatch({"name": "read_file", "arguments": {"path": "big.txt"}})
    assert "observation truncated" in obs
    assert len(obs) <= MAX_OBSERVATION_CHARS + 64
