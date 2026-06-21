"""Tests for CLI subcommands (--diff, --apply, --revert, --test, --config-*, --export)."""
import json
import subprocess
import tempfile
from pathlib import Path

from swe_agent.cli import (cmd_config_get, cmd_config_set, cmd_diff, cmd_export,
                            cmd_test, _auto_create_branch)


class FakeArgs:
    """Minimal args namespace for subcommand tests."""
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.config_get = None
        self.config_set = None
        self.export = None


def _init_git_repo(path: Path):
    """Initialize a git repo with one commit."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    (path / "readme.txt").write_text("hello")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


def test_cmd_diff_clean_repo(capsys):
    """Shows no pending changes for a clean repo."""
    with tempfile.TemporaryDirectory() as td:
        _init_git_repo(Path(td))
        args = FakeArgs(td)
        ret = cmd_diff(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "No pending changes" in out


def test_cmd_diff_shows_changes(capsys):
    """Shows uncommitted changes."""
    with tempfile.TemporaryDirectory() as td:
        _init_git_repo(Path(td))
        (Path(td) / "readme.txt").write_text("modified")
        args = FakeArgs(td)
        ret = cmd_diff(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "Unstaged" in out


def test_cmd_config_set_and_get(capsys):
    """Set and then get a config value."""
    with tempfile.TemporaryDirectory() as td:
        args = FakeArgs(td)
        args.config_set = ("model", "gpt-4")
        cmd_config_set(args)
        # Verify file was created
        config_path = Path(td) / ".agent" / "config.yaml"
        assert config_path.exists()
        assert "model: gpt-4" in config_path.read_text()

        # Now get it
        args.config_get = "model"
        cmd_config_get(args)
        out = capsys.readouterr().out
        assert "gpt-4" in out


def test_cmd_test_auto_detect(capsys):
    """Detects test framework from manifest files."""
    with tempfile.TemporaryDirectory() as td:
        # Create a pyproject.toml so pytest is detected
        (Path(td) / "pyproject.toml").write_text("[tool.pytest]")
        args = FakeArgs(td)
        # cmd_test will try to run pytest which may fail, but it should detect it
        ret = cmd_test(args)
        out = capsys.readouterr().out
        assert "pytest" in out


def test_auto_create_branch():
    """Creates a branch with the expected naming pattern."""
    with tempfile.TemporaryDirectory() as td:
        _init_git_repo(Path(td))
        _auto_create_branch(Path(td), "fix login bug")
        res = subprocess.run(["git", "branch", "--show-current"], cwd=td,
                             capture_output=True, text=True)
        branch = res.stdout.strip()
        assert branch.startswith("agent/")
        assert "fix-login-bug" in branch
