"""Tests for the Ralph loop (hermetic — a fake agent stands in for run_turn)."""
from __future__ import annotations

from pathlib import Path

from swe_agent.config import RALPH_STATE_FILE
from swe_agent.ralph import extract_promise, run_ralph


class _Ctx:
    def __init__(self, cwd):
        self.cwd = cwd


class _FakeAgent:
    """Minimal stand-in: yields scripted run_turn outputs and records re-feeds."""

    def __init__(self, cwd: Path, outputs):
        self.ctx = _Ctx(cwd)
        self._outputs = list(outputs)
        self.seen = []          # the prompt re-fed each iteration
        self.turns = 0
        self.critic_gate = None
        self.verbose = False

    def add_user(self, text: str):
        self.seen.append(text)

    def run_turn(self) -> str:
        out = self._outputs[self.turns] if self.turns < len(self._outputs) else ""
        self.turns += 1
        return out


# ----------------------------------------------------------------- extract_promise

def test_extract_promise_found():
    assert extract_promise("blah <promise>DONE</promise> trailing") == "DONE"


def test_extract_promise_normalizes_whitespace_and_case():
    assert extract_promise("x <PROMISE>  TASK   COMPLETE \n </PROMISE> y") == "TASK COMPLETE"


def test_extract_promise_absent():
    assert extract_promise("no promise here") is None
    assert extract_promise("") is None


def test_extract_promise_first_only():
    assert extract_promise("<promise>A</promise> ... <promise>B</promise>") == "A"


# ----------------------------------------------------------------- run_ralph

def test_stops_when_promise_matches(tmp_path: Path):
    agent = _FakeAgent(tmp_path, ["still working...", "all green <promise>DONE</promise>"])
    out = run_ralph(agent, "build it", max_iterations=5, completion_promise="DONE", verbose=False)
    assert "DONE" in out
    assert agent.turns == 2                                  # stopped on the 2nd pass
    assert not (tmp_path / RALPH_STATE_FILE).exists()        # state cleaned up


def test_promise_mismatch_keeps_looping(tmp_path: Path):
    # The model emits a DIFFERENT promise — must not end the loop.
    agent = _FakeAgent(tmp_path, ["<promise>NOPE</promise>"] * 3)
    run_ralph(agent, "task", max_iterations=3, completion_promise="DONE", verbose=False)
    assert agent.turns == 3                                  # ran to the cap, never matched


def test_honors_max_iterations(tmp_path: Path):
    agent = _FakeAgent(tmp_path, ["no promise"] * 10)
    run_ralph(agent, "task", max_iterations=4, completion_promise="DONE", verbose=False)
    assert agent.turns == 4


def test_unlimited_clamps_to_hard_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("swe_agent.ralph.RALPH_HARD_CAP", 3)
    agent = _FakeAgent(tmp_path, ["nope"] * 10)
    run_ralph(agent, "task", max_iterations=0, completion_promise="DONE", verbose=False)
    assert agent.turns == 3


def test_refeeds_same_task_each_iteration(tmp_path: Path):
    agent = _FakeAgent(tmp_path, ["a", "b"])
    run_ralph(agent, "implement X", max_iterations=2, verbose=False)
    assert agent.turns == 2
    assert all("implement X" in s for s in agent.seen)       # same task re-fed every pass


def test_external_cancel_via_state_file_removal(tmp_path: Path):
    agent = _FakeAgent(tmp_path, ["nope"] * 5)

    def cancel_after_first(i, _final):
        if i == 1:
            (tmp_path / RALPH_STATE_FILE).unlink()           # operator removes the state file

    run_ralph(agent, "task", max_iterations=5, completion_promise="DONE",
              verbose=False, on_iteration=cancel_after_first)
    assert agent.turns == 1                                  # cancelled at the iteration boundary


def test_resets_critic_rounds_each_iteration(tmp_path: Path):
    class _CG:
        rounds_used = 7
    agent = _FakeAgent(tmp_path, ["a", "b"])
    agent.critic_gate = _CG()
    run_ralph(agent, "task", max_iterations=2, verbose=False)
    assert agent.critic_gate.rounds_used == 0                # fresh critic budget per pass


# ----------------------------------------------------------------- run id

def test_run_id_written_to_state_file(tmp_path: Path):
    from swe_agent.ralph import RalphState
    st = RalphState(cwd=tmp_path, run_id="ralph-abc123")
    st.iteration = 1
    st.write("do the thing")
    content = (tmp_path / RALPH_STATE_FILE).read_text(encoding="utf-8")
    assert 'run_id: "ralph-abc123"' in content


def test_run_ralph_auto_assigns_run_id(tmp_path: Path):
    seen = {}

    def cap(i, _final):
        seen["content"] = (tmp_path / RALPH_STATE_FILE).read_text(encoding="utf-8")

    agent = _FakeAgent(tmp_path, ["<promise>DONE</promise>"])
    run_ralph(agent, "t", max_iterations=1, completion_promise="DONE",
              verbose=False, on_iteration=cap)
    assert "run_id:" in seen["content"] and "ralph-" in seen["content"]  # auto-assigned id


def test_run_ralph_honors_explicit_run_id(tmp_path: Path):
    seen = {}

    def cap(i, _final):
        seen["content"] = (tmp_path / RALPH_STATE_FILE).read_text(encoding="utf-8")

    agent = _FakeAgent(tmp_path, ["<promise>DONE</promise>"])
    run_ralph(agent, "t", max_iterations=1, completion_promise="DONE",
              run_id="ralph-explicit", verbose=False, on_iteration=cap)
    assert 'run_id: "ralph-explicit"' in seen["content"]
