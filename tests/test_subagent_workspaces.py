"""Sub-agent workspace isolation (hermetic -- no Ollama/network).

``spawn_subagent`` creates the worktree synchronously before submitting the
child to the thread pool, so these tests monkeypatch ``run_subagent`` with a
no-op stub: no real model is ever contacted, but the isolation/metadata logic
runs exactly as in production.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

import swe_agent.agent as agent_mod
from swe_agent.config import ApprovalMode
from swe_agent.tools import subagent as sub
from swe_agent.tools.base import ToolContext
from swe_agent.workspaces import WORKTREE_SUBDIR


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    (path / "readme.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


@pytest.fixture
def stub_runner(monkeypatch):
    """Replace run_subagent so no real agent loop / model call happens."""
    calls = []

    def _stub(task, description, model, cwd, base_url, num_ctx, temperature,
              parent_approval, *, provider="ollama", api_key="", mode="audit"):
        calls.append({"cwd": cwd, "mode": mode})
        return f"(stub {mode} summary)"

    monkeypatch.setattr(agent_mod, "run_subagent", _stub)
    return calls


@pytest.fixture(autouse=True)
def _clear_registry():
    sub._SUBAGENTS.clear()
    yield
    sub._SUBAGENTS.clear()


def _ctx(cwd: Path, approval=ApprovalMode.AUTO_ACCEPT) -> ToolContext:
    return ToolContext(cwd=cwd, approval=approval, provider="ollama")


def _wait_done():
    """Block until all spawned futures finish (stub returns immediately)."""
    for rec in sub._SUBAGENTS.values():
        if rec.future is not None:
            rec.future.result(timeout=5)


def _only_record():
    assert len(sub._SUBAGENTS) == 1
    return next(iter(sub._SUBAGENTS.values()))


def test_audit_mode_creates_no_worktree(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    out = sub.spawn_subagent(_ctx(tmp_path), "look around", "audit code", mode="audit")
    assert "audit sub-agent" in out
    rec = _only_record()
    assert rec.mode == "audit"
    assert rec.workspace is None
    assert not (tmp_path / WORKTREE_SUBDIR).exists()


def test_default_mode_is_audit(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    sub.spawn_subagent(_ctx(tmp_path), "task", "label")  # no mode kwarg
    assert _only_record().mode == "audit"


def test_implement_mode_creates_unique_worktree(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl one", mode="implement")
    sub.spawn_subagent(ctx, "do work", "impl two", mode="implement")
    recs = list(sub._SUBAGENTS.values())
    assert len(recs) == 2
    paths = {r.workspace for r in recs}
    assert None not in paths
    assert len(paths) == 2  # unique per sub-agent
    for r in recs:
        ws = Path(r.workspace)
        assert ws.exists()
        assert ws.parent == tmp_path / WORKTREE_SUBDIR


def test_implement_refused_when_parent_read_only(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path, approval=ApprovalMode.READ_ONLY)
    out = sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    assert "Refused" in out
    assert "read-only" in out
    assert sub._SUBAGENTS == {}
    assert not (tmp_path / WORKTREE_SUBDIR).exists()


def test_implement_refused_outside_git_repo(tmp_path, stub_runner):
    # tmp_path is NOT a git repo.
    out = sub.spawn_subagent(_ctx(tmp_path), "do work", "impl", mode="implement")
    assert "Refused" in out
    assert "git repository" in out
    assert sub._SUBAGENTS == {}


def test_invalid_mode_rejected(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    out = sub.spawn_subagent(_ctx(tmp_path), "x", "y", mode="bogus")
    assert "Invalid mode" in out
    assert sub._SUBAGENTS == {}


def test_get_subagent_diff_returns_changes(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    _wait_done()
    rec = _only_record()
    # Simulate the child writing a new file inside its isolated worktree.
    (Path(rec.workspace) / "newfile.py").write_text("print('hi')\n", encoding="utf-8")
    diff = sub.get_subagent_diff(ctx, rec.id)
    assert "newfile.py" in diff
    assert "print" in diff


def test_get_subagent_diff_none_for_audit(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "look", "audit", mode="audit")
    rec = _only_record()
    out = sub.get_subagent_diff(ctx, rec.id)
    assert "no isolated workspace" in out


def test_get_subagent_result_reports_mode_and_diff_hint(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    _wait_done()
    rec = _only_record()
    out = sub.get_subagent_result(ctx, rec.id)
    assert "mode=implement" in out
    assert "status=done" in out
    assert "get_subagent_diff" in out


def test_worktree_container_is_gitignored(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    sub.spawn_subagent(_ctx(tmp_path), "do work", "impl", mode="implement")
    gi = tmp_path / WORKTREE_SUBDIR / ".gitignore"
    assert gi.exists()
    assert gi.read_text(encoding="utf-8").strip() == "*"
    # The parent tree must not see the worktree container as untracked.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(tmp_path),
                            capture_output=True, text=True).stdout
    assert ".agent/worktrees" not in status


def test_get_subagent_diff_includes_staged_changes(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    _wait_done()
    rec = _only_record()
    ws = Path(rec.workspace)
    # Child stages a modified tracked file (unstaged git diff would miss this).
    (ws / "readme.txt").write_text("staged change", encoding="utf-8")
    subprocess.run(["git", "add", "readme.txt"], cwd=str(ws), capture_output=True)
    diff = sub.get_subagent_diff(ctx, rec.id)
    assert "staged change" in diff


def test_diff_and_discard_refuse_while_running(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    release = threading.Event()

    def _blocking(*a, **k):
        release.wait(timeout=5)
        return "(done)"

    monkeypatch.setattr(agent_mod, "run_subagent", _blocking)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    rec = _only_record()
    try:
        assert rec.status == "running"
        assert "still running" in sub.get_subagent_diff(ctx, rec.id)
        assert "still running" in sub.discard_subagent_workspace(ctx, rec.id)
        assert Path(rec.workspace).exists()  # not discarded
    finally:
        release.set()
        rec.future.result(timeout=5)


def test_discard_subagent_workspace_removes_worktree(tmp_path, stub_runner):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    _wait_done()
    rec = _only_record()
    ws = Path(rec.workspace)
    assert ws.exists()

    out = sub.discard_subagent_workspace(ctx, rec.id)
    assert "worktree" in out.lower()
    assert not ws.exists()
    assert rec.workspace is None

    listed = subprocess.run(["git", "worktree", "list"], cwd=str(tmp_path),
                            capture_output=True, text=True)
    assert str(ws) not in listed.stdout


def test_discard_keeps_workspace_when_removal_fails(tmp_path, stub_runner, monkeypatch):
    _init_git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    sub.spawn_subagent(ctx, "do work", "impl", mode="implement")
    _wait_done()
    rec = _only_record()
    # Simulate a failed removal (e.g. git worktree remove timed out).
    monkeypatch.setattr(sub, "remove_subagent_worktree",
                        lambda root, ws: "Error: git worktree remove timed out")
    out = sub.discard_subagent_workspace(ctx, rec.id)
    assert out.startswith("Error:")
    # Workspace reference is retained so it can be discarded again later.
    assert rec.workspace is not None
