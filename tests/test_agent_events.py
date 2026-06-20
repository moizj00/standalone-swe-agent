"""Tests for the Agent's structured event sink (event_cb)."""
from __future__ import annotations

from conftest import make_agent, scripted


def _collector():
    events = []
    return events, events.append


def test_event_sequence_tool_then_complete(ctx):
    (ctx.cwd / "f.txt").write_text("hi", encoding="utf-8")
    events, cb = _collector()
    mock = scripted(
        ("reading", [("read_file", {"path": "f.txt"})]),
        ("", [("task_complete", {"final_summary": "done"})]),
    )
    out = make_agent(ctx, mock, event_cb=cb).run_turn()

    types = [e["type"] for e in events]
    assert types[0] == "step"
    assert types[-1] == "final"
    assert any(e["type"] == "tool_call" and e["name"] == "read_file" for e in events)
    assert any(e["type"] == "tool_result" and e["name"] == "read_file" for e in events)
    final = next(e for e in events if e["type"] == "final")
    assert "done" in final["text"] and "done" in out


def test_tool_call_event_carries_arguments(ctx):
    (ctx.cwd / "f.txt").write_text("x", encoding="utf-8")
    events, cb = _collector()
    mock = scripted(("", [("read_file", {"path": "f.txt"})]),
                    ("", [("task_complete", {"final_summary": "ok"})]))
    make_agent(ctx, mock, event_cb=cb).run_turn()
    tc = next(e for e in events if e["type"] == "tool_call" and e["name"] == "read_file")
    assert tc["arguments"] == {"path": "f.txt"}


def test_assistant_event_when_content_present(ctx):
    events, cb = _collector()
    make_agent(ctx, scripted(("hello world", [])), event_cb=cb).run_turn()
    assert any(e["type"] == "assistant" and "hello world" in e["content"] for e in events)
    assert events[-1]["type"] == "final"


def test_error_event_on_model_failure(ctx):
    def boom(_messages):
        raise RuntimeError("kaboom")

    events, cb = _collector()
    out = make_agent(ctx, boom, event_cb=cb).run_turn()
    assert "kaboom" in out
    assert any(e["type"] == "error" for e in events)


def test_no_event_cb_is_silent(ctx):
    # Backward compatibility: the CLI path passes no event_cb.
    agent = make_agent(ctx, scripted(("hi", [])))
    assert agent.run_turn() == "hi"


def test_event_callback_exception_does_not_break_loop(ctx):
    def bad_cb(_e):
        raise ValueError("sink blew up")

    agent = make_agent(ctx, scripted(("answer", [])), event_cb=bad_cb)
    assert agent.run_turn() == "answer"  # loop survives a broken sink
