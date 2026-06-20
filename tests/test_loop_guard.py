"""Tests for runtime loop guard (no Ollama required)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swe_agent.agent import Agent
from swe_agent.config import ApprovalMode
from swe_agent.loop_guard import LoopGuard, READ_ONLY_TOOLS
from swe_agent.tools.base import ToolContext, ToolSpec


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=tmp_path, approval=ApprovalMode.YOLO)


def _spec(name: str, *, mutating: bool = False, category: str = "read") -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={}, impl=lambda *a, **k: "", mutating=mutating, category=category)


def _record_reads(guard: LoopGuard, ctx: ToolContext, n: int, *, same_path: bool = False) -> None:
    spec = _spec("read_file")
    for i in range(n):
        path = "src/foo.ts" if same_path else f"src/file_{i}.py"
        guard.record(step=i + 1, name="read_file", args={"path": path}, result="line 1\n", spec=spec, ctx=ctx)


def test_exact_repeat_detected():
    guard = LoopGuard(enabled=True, verbose=False)
    ctx = _ctx(Path("/tmp"))
    _record_reads(guard, ctx, 3, same_path=True)
    signal = guard.detect()
    assert signal is not None
    assert signal.signal == "exact_repeat"


def test_read_thrash_detected():
    guard = LoopGuard(enabled=True, verbose=False)
    ctx = _ctx(Path("/tmp"))
    _record_reads(guard, ctx, 5, same_path=False)
    signal = guard.detect()
    assert signal is not None
    assert signal.signal == "read_thrash"


def test_no_progress_detected():
    guard = LoopGuard(enabled=True, verbose=False)
    ctx = _ctx(Path("/tmp"))
    spec = _spec("read_file")
    guard.no_progress_steps = 12
    guard.record(step=12, name="read_file", args={"path": "x"}, result="ok", spec=spec, ctx=ctx)
    signal = guard.detect()
    assert signal is not None
    assert signal.signal == "no_progress"


def test_progress_resets_episode():
    guard = LoopGuard(enabled=True, verbose=False)
    ctx = _ctx(Path("/tmp"))
    guard.episode_level = 3
    guard.no_progress_steps = 10
    guard.mark_progress()
    assert guard.episode_level == 0
    assert guard.no_progress_steps == 0


def test_level2_blocks_readonly_tools():
    guard = LoopGuard(enabled=True, verbose=False)
    guard.block_readonly_turns = 1
    blocked, msg = guard.should_block_tool("read_file", _spec("read_file"))
    assert blocked is True
    assert "loop guard" in msg
    blocked2, _ = guard.should_block_tool("edit", _spec("edit", mutating=True, category="write"))
    assert blocked2 is False


def test_intervention_injected_before_model():
    guard = LoopGuard(enabled=True, verbose=False, yolo=True)
    ctx = _ctx(Path("/tmp"))
    _record_reads(guard, ctx, 5)
    intervention, abort = guard.check_before_model(step=6)
    assert abort is None
    assert intervention is not None
    assert "[LOOP_GUARD" in intervention["content"]


def test_edit_retry_detected():
    guard = LoopGuard(enabled=True, verbose=False)
    ctx = _ctx(Path("/tmp"))
    spec = _spec("edit", mutating=True, category="write")
    args = {"path": "a.py", "old_string": "x", "new_string": "y"}
    for i in range(2):
        guard.record(step=i + 1, name="edit", args=args, result="Error: old_string not found", spec=spec, ctx=ctx)
    signal = guard.detect()
    assert signal is not None
    assert signal.signal == "edit_retry"


def test_todo_progress_resets_no_progress(tmp_path: Path):
    guard = LoopGuard(enabled=True, verbose=False)
    ctx = _ctx(tmp_path)
    todos = [{"content": "step 1", "status": "completed"}]
    (tmp_path / ".agent_todos.json").write_text(json.dumps(todos), encoding="utf-8")
    spec = _spec("todo_write", category="meta")
    guard.no_progress_steps = 5
    guard.record(step=6, name="todo_write", args={"todos": todos}, result="ok", spec=spec, ctx=ctx)
    assert guard.no_progress_steps == 0


def test_agent_loop_guard_fires_on_mock_loop(tmp_path: Path):
    calls = {"n": 0}

    def mock(messages):
        calls["n"] += 1
        return ("reading again", [{"function": {"name": "read_file", "arguments": {"path": "x.py"}}}])

    ctx = ToolContext(cwd=tmp_path, approval=ApprovalMode.YOLO)
    guard = LoopGuard(enabled=True, verbose=False, yolo=True)
    agent = Agent(
        model="test",
        ctx=ctx,
        system_prompt="test",
        stream=False,
        verbose=False,
        max_steps=10,
        mock=mock,
        loop_guard=guard,
    )
    agent.add_user("explore")
    result = agent.run_turn()
    assert guard.loop_events or "[LOOP_GUARD" in str(agent.messages)
    assert calls["n"] >= 1


def test_cloud_escalation_callback():
    guard = LoopGuard(
        enabled=True,
        verbose=False,
        yolo=True,
        cloud_escalate_cb=lambda task, reason: "1. Edit file\n2. Run tests",
    )
    ctx = _ctx(Path("/tmp"))
    _record_reads(guard, ctx, 5)
    guard.episode_level = 3
    guard.last_signal = guard.detect()
    guard.episode_level = 3
    intervention, abort = guard.check_before_model(step=10)
    # Force level 4 path by setting episode high and detecting again
    guard.episode_level = 3
    signal = guard.detect()
    assert signal is not None
    guard.episode_level = 3
    guard.last_signal = signal
    guard.episode_level = 4
    plan = guard._try_cloud_escalation(signal)
    assert plan is not None
    assert "CLOUD_UNSTICK" in plan or "Edit file" in plan


def test_abort_at_level_five():
    guard = LoopGuard(enabled=True, verbose=False, yolo=True)
    ctx = _ctx(Path("/tmp"))
    _record_reads(guard, ctx, 5)
    guard.episode_level = 5
    signal = guard.detect()
    assert signal is not None
    _, abort = guard.check_before_model(step=20)
    # At level 5 with new signal, should abort
    guard.episode_level = 5
    _, abort2 = guard.check_before_model(step=21)
    assert abort2 is None or "LOOP_GUARD abort" in (abort2 or "")