"""Tests for swe_agent.patcher.apply_patch (hermetic — a throwaway git repo per test).

No network: every test builds its own repo under pytest's ``tmp_path`` and is torn
down automatically. GitPython drives repo setup so the test mirrors real usage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from swe_agent.patcher import apply_patch, commit_worktree, new_branch


def _init_repo(path: Path) -> Repo:
    """A fresh repo with one committed README and a deterministic identity."""
    repo = Repo.init(path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test User")
        cw.set_value("user", "email", "test@example.com")
    (path / "README.md").write_text("# Project\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")
    return repo


def _branch_names(repo: Repo):
    return {h.name for h in repo.heads}


# ----------------------------------------------------------------- file-list patches

def test_file_list_creates_branch_commits_and_writes_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    result = apply_patch(
        str(tmp_path),
        [{"path": "src/hello.py", "content": "print('hello')\n"}],
    )
    assert "branch" in result and "commit" in result
    assert result["branch"] in _branch_names(repo)
    assert (tmp_path / "src" / "hello.py").read_text(encoding="utf-8") == "print('hello')\n"
    # the returned sha is the new commit on the new branch
    assert repo.commit(result["commit"]).hexsha == result["commit"]


def test_named_branch_is_used(tmp_path: Path):
    _init_repo(tmp_path)
    result = apply_patch(
        str(tmp_path),
        [{"path": "a.txt", "content": "a\n"}],
        branch_name="feature/x",
    )
    assert result["branch"] == "feature/x"


def test_existing_branch_name_gets_unique_suffix(tmp_path: Path):
    repo = _init_repo(tmp_path)
    repo.create_head("feature/x")
    result = apply_patch(
        str(tmp_path),
        [{"path": "a.txt", "content": "a\n"}],
        branch_name="feature/x",
    )
    assert result["branch"] != "feature/x"
    assert result["branch"].startswith("feature/x-")


# ----------------------------------------------------------------- unified-diff patches

def test_unified_diff_is_applied(tmp_path: Path):
    _init_repo(tmp_path)
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # Project\n"
        "+second line\n"
    )
    result = apply_patch(str(tmp_path), diff)
    assert "commit" in result
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Project\nsecond line\n"


def test_invalid_unified_diff_raises_and_creates_no_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    before = repo.head.commit.hexsha
    bad = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-this context does not match\n"
        "+whatever\n"
    )
    with pytest.raises(ValueError):
        apply_patch(str(tmp_path), bad)
    assert repo.head.commit.hexsha == before  # HEAD untouched


# ----------------------------------------------------------------- safety / validation

def test_absolute_path_is_rejected(tmp_path: Path):
    _init_repo(tmp_path)
    with pytest.raises(ValueError):
        apply_patch(str(tmp_path), [{"path": "/etc/passwd", "content": "x"}])


def test_parent_traversal_is_rejected(tmp_path: Path):
    _init_repo(tmp_path)
    with pytest.raises(ValueError):
        apply_patch(str(tmp_path), [{"path": "../escape.txt", "content": "x"}])


# ----------------------------------------------------------------- idempotency

def test_noop_patch_returns_base_without_new_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = repo.head.commit.hexsha
    # README already has exactly this content -> applying it changes nothing.
    result = apply_patch(str(tmp_path), [{"path": "README.md", "content": "# Project\n"}])
    assert result["commit"] == base  # no duplicate commit created


# ----------------------------------------------------------------- shared git helpers

def test_new_branch_creates_and_checks_out(tmp_path: Path):
    repo = _init_repo(tmp_path)
    name = new_branch(repo, "feature/y")
    assert name == "feature/y"
    assert repo.active_branch.name == "feature/y"


def test_commit_worktree_returns_none_on_noop(tmp_path: Path):
    repo = _init_repo(tmp_path)
    new_branch(repo, "wip")
    assert commit_worktree(repo, "nothing changed") is None  # clean tree -> no commit
    (tmp_path / "new.txt").write_text("hi\n", encoding="utf-8")
    sha = commit_worktree(repo, "add new.txt")
    assert sha and repo.head.commit.hexsha == sha
