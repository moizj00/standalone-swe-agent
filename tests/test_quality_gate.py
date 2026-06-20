"""Tests for quality gate (no Ollama required)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from swe_agent.agent import Agent
from swe_agent.config import ApprovalMode
from swe_agent.quality_gate import QualityGate
from swe_agent.tools.base import ToolContext, ToolSpec


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=tmp_path, approval=ApprovalMode.YOLO)


def _spec(name: str, *, mutating: bool = False) -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={}, impl=lambda *a, **k: "ok",
                    mutating=mutating, category="write" if mutating else "read")


def test_readonly_task_complete_allowed():
    gate = QualityGate(enabled=True, verbose=False)
    ok, msg = gate.check_task_complete({
        "final_summary": "Listed 5 tRPC routers with procedure counts; read-only survey, no files changed.",
    })
    assert ok is True
    assert msg == ""


def test_vague_summary_rejected():
    gate = QualityGate(enabled=True, verbose=False)
    ok, msg = gate.check_task_complete({"final_summary": "done"})
    assert ok is False
    assert "[QUALITY_GATE]" in msg


def test_mutation_without_verification_rejected():
    gate = QualityGate(enabled=True, verbose=False)
    gate.record(step=1, name="edit", args={"path": "a.py"}, result="ok", spec=_spec("edit", mutating=True))
    ok, msg = gate.check_task_complete({
        "final_summary": "Added type hint to foo() in auth.ts for better static checking.",
        "files_changed": ["auth.ts"],
        "confidence": "high",
    })
    assert ok is False
    assert "[QUALITY_GATE]" in msg


def test_mutation_with_verification_allowed():
    gate = QualityGate(enabled=True, verbose=False)
    gate.record(step=1, name="edit", args={"path": "a.py"}, result="ok", spec=_spec("edit", mutating=True))
    gate.record(step=2, name="run_linter", args={}, result="All checks passed", spec=_spec("run_linter"))
    ok, msg = gate.check_task_complete({
        "final_summary": "Added type hint to foo() in auth.ts. Verify with: npx tsc --noEmit (passed).",
        "files_changed": ["auth.ts"],
        "confidence": "high",
    })
    assert ok is True


def test_low_confidence_skip_allowed():
    gate = QualityGate(enabled=True, verbose=False)
    gate.record(step=1, name="write_file", args={}, result="ok", spec=_spec("write_file", mutating=True))
    ok, msg = gate.check_task_complete({
        "final_summary": "Updated config only. Verification skipped because no test suite exists in this repo.",
        "confidence": "low",
    })
    assert ok is True


def test_agent_rejects_premature_complete():
    calls = {"n": 0}

    def mock(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("editing", [{"function": {"name": "edit", "arguments": {
                "path": "a.py", "old_string": "x", "new_string": "y",
            }}}])
        return ("done", [{"function": {"name": "task_complete", "arguments": {
            "final_summary": "Fixed it.",
            "confidence": "high",
        }}}])

    with tempfile.TemporaryDirectory() as td:
        gate = QualityGate(enabled=True, verbose=False)
        agent = Agent(
            model="test", ctx=_ctx(Path(td)), system_prompt="test",
            stream=False, verbose=False, max_steps=5, mock=mock,
            loop_guard=None, quality_gate=gate,
        )
        # Disable auto loop guard
        agent.loop_guard = None
        agent.add_user("fix")
        agent.run_turn()
        assert gate.gate_events
        assert any("QUALITY_GATE" in (m.get("content") or "") for m in agent.messages if m.get("role") == "tool")