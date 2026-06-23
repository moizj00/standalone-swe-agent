"""CLI git command reliability (hermetic -- real git in a tmp repo, no network)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from swe_agent.cli import _git_run, cmd_apply, cmd_diff, cmd_revert


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    (path / "readme.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


class _Args:
    def __init__(self, cwd, force=False):
        self.cwd = str(cwd)
        self.force = force


def test_git_run_returns_structured_result(tmp_path):
    _init_git_repo(tmp_path)
    res = _git_run(tmp_path, ["status", "--porcelain"])
    assert res.returncode == 0
    assert res.ok
    assert res.stderr == ""


def test_git_run_reports_failure_returncode(tmp_path):
    _init_git_repo(tmp_path)
    res = _git_run(tmp_path, ["checkout", "does-not-exist"])
    assert res.returncode != 0
    assert not res.ok
    assert res.stderr.strip()


def test_cmd_apply_success_commits(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "readme.txt").write_text("changed", encoding="utf-8")
    ret = cmd_apply(_Args(tmp_path))
    assert ret == 0


def test_cmd_apply_returns_failure_when_commit_fails(tmp_path, capsys):
    """Clean repo -> nothing to commit -> git commit exits non-zero -> cmd_apply fails."""
    _init_git_repo(tmp_path)  # no pending changes
    ret = cmd_apply(_Args(tmp_path))
    assert ret == 1


def test_cmd_diff_clean_repo_reports_no_changes(tmp_path, capsys):
    _init_git_repo(tmp_path)
    ret = cmd_diff(_Args(tmp_path))
    assert ret == 0
    assert "No pending changes" in capsys.readouterr().out


def test_cmd_diff_non_repo_reports_error(tmp_path, capsys):
    # tmp_path is NOT a git repo -> git diff fails; must not claim a clean tree.
    ret = cmd_diff(_Args(tmp_path))
    assert ret == 1
    out = capsys.readouterr().out
    assert "No pending changes" not in out


def test_cmd_revert_refuses_without_force(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "readme.txt").write_text("dirty", encoding="utf-8")
    ret = cmd_revert(_Args(tmp_path, force=False))
    assert ret == 1
    out = capsys.readouterr().out
    assert "--force" in out
    # The destructive op did NOT run: the change is still on disk.
    assert (tmp_path / "readme.txt").read_text(encoding="utf-8") == "dirty"


def test_cmd_revert_with_force_discards_changes(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "readme.txt").write_text("dirty", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("junk", encoding="utf-8")
    ret = cmd_revert(_Args(tmp_path, force=True))
    assert ret == 0
    assert (tmp_path / "readme.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "untracked.txt").exists()
