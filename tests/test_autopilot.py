"""Hermetic tests for the autopilot loop (a FakeAgent edits files; a real tmp git repo)."""
from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from swe_agent.autopilot import AutopilotResult, run_autopilot

PYTEST = ["python", "-m", "pytest", "-q"]


class _Ctx:
    def __init__(self, cwd):
        self.cwd = cwd


class _FakeAgent:
    """Each run_turn() pops the next 'edit' (a function writing files into cwd)."""

    def __init__(self, cwd: Path, edits):
        self.ctx = _Ctx(cwd)
        self._edits = list(edits)
        self.seen = []
        self.turns = 0

    def add_user(self, text: str):
        self.seen.append(text)

    def run_turn(self) -> str:
        if self._edits:
            self._edits.pop(0)(self.ctx.cwd)
        self.turns += 1
        return "did an edit"


def _init_repo(path: Path) -> Repo:
    repo = Repo.init(path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "T")
        cw.set_value("user", "email", "t@e.com")
    (path / "README.md").write_text("# p\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    return repo


def _write_passing(p: Path):
    (p / "test_g.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")


def _write_failing(p: Path):
    (p / "test_g.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")


def test_green_on_first_try(tmp_path: Path):
    repo = _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [_write_passing])
    res = run_autopilot(agent, "add a test", repo_path=str(tmp_path),
                        test_command=PYTEST, verbose=False)
    assert isinstance(res, AutopilotResult)
    assert res.success and res.attempts == 1
    assert res.branch in {h.name for h in repo.heads} and res.commit
    assert isinstance(res.run_id, str) and res.run_id  # every run carries an id
    assert res.run_id in res.branch                     # branch is traceable to the run


def test_each_run_has_a_unique_id(tmp_path: Path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _init_repo(repo_a)
    _init_repo(repo_b)
    r1 = run_autopilot(_FakeAgent(repo_a, [_write_passing]), "t", repo_path=str(repo_a),
                       test_command=PYTEST, verbose=False)
    r2 = run_autopilot(_FakeAgent(repo_b, [_write_passing]), "t", repo_path=str(repo_b),
                       test_command=PYTEST, verbose=False)
    assert r1.run_id != r2.run_id


def test_explicit_run_id_is_honored(tmp_path: Path):
    _init_repo(tmp_path)
    res = run_autopilot(_FakeAgent(tmp_path, [_write_passing]), "t", repo_path=str(tmp_path),
                        test_command=PYTEST, run_id="myrun123", verbose=False)
    assert res.run_id == "myrun123" and "myrun123" in res.branch


def test_repairs_then_passes(tmp_path: Path):
    _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [_write_failing, _write_failing, _write_passing])
    res = run_autopilot(agent, "make it pass", repo_path=str(tmp_path),
                        max_repairs=3, test_command=PYTEST, verbose=False)
    assert res.success and res.attempts == 3
    assert any("failing" in s.lower() for s in agent.seen[1:])  # failure injected on repair


def test_exhausts_and_reports_failure(tmp_path: Path):
    repo = _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [_write_failing, _write_failing])
    res = run_autopilot(agent, "cannot fix", repo_path=str(tmp_path),
                        max_repairs=1, test_command=PYTEST, verbose=False)
    assert not res.success and res.attempts == 2
    assert res.commit and len(list(repo.iter_commits(res.branch))) >= 2


def test_dirty_tree_is_refused(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError):
        run_autopilot(_FakeAgent(tmp_path, []), "t", repo_path=str(tmp_path), verbose=False)


def test_no_test_command_commits_and_reports(tmp_path: Path):
    _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [lambda p: (p / "feature.py").write_text("x=1\n", encoding="utf-8")])
    res = run_autopilot(agent, "add feature", repo_path=str(tmp_path), verbose=False)
    assert res.test_result.skipped and res.success and res.attempts == 1
