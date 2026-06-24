"""Hardening of the high-risk tool surface (hermetic -- no network).

- apply_patch must not let a confined (network-driven) agent write outside the
  workspace, including via the `patch -p1` fallback.
- detect_danger flags additional unambiguous destructive commands.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from swe_agent.config import ApprovalMode
from swe_agent.tools.base import ToolContext
from swe_agent.tools.exec import detect_danger
from swe_agent.tools.git import _patch_target_paths, apply_patch


def _confined_ctx(cwd: Path) -> ToolContext:
    return ToolContext(cwd=cwd, approval=ApprovalMode.YOLO, confine=True)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), capture_output=True)


# --------------------------------------------------------------- patch path parsing

def test_patch_target_paths_extracts_b_side_and_renames():
    patch = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
        "rename from old.py\nrename to new.py\n"
    )
    paths = _patch_target_paths(patch)
    assert "foo.py" in paths
    assert "new.py" in paths and "old.py" in paths
    assert "/dev/null" not in paths


# --------------------------------------------------------------- confinement

def test_apply_patch_confined_refuses_parent_escape(tmp_path):
    outside = tmp_path.parent / "escape.txt"
    patch = (
        "--- /dev/null\n"
        "+++ b/../escape.txt\n"
        "@@ -0,0 +1 @@\n+pwned\n"
    )
    ctx = _confined_ctx(tmp_path)
    out = apply_patch(ctx, patch)
    assert "escapes the workspace" in out
    assert not outside.exists()


def test_apply_patch_confined_refuses_absolute_target(tmp_path):
    patch = (
        "--- /dev/null\n"
        "+++ b//etc/evil.conf\n"
        "@@ -0,0 +1 @@\n+pwned\n"
    )
    out = apply_patch(_confined_ctx(tmp_path), patch)
    assert "escapes the workspace" in out


def test_apply_patch_confined_allows_in_workspace(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "f.txt").write_text("two\n", encoding="utf-8")
    patch = subprocess.run(["git", "diff"], cwd=str(tmp_path), capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", "f.txt"], cwd=str(tmp_path), capture_output=True)

    out = apply_patch(_confined_ctx(tmp_path), patch)
    assert "applied successfully" in out
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "two\n"


def test_apply_patch_unconfined_still_allows_relative(tmp_path):
    # Without confinement the escape guard does not run (operator-trusted CLI path).
    patch = "--- /dev/null\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+x\n"
    ctx = ToolContext(cwd=tmp_path, approval=ApprovalMode.YOLO, confine=False)
    out = apply_patch(ctx, patch)
    assert "escapes the workspace" not in out  # not refused by the confinement guard


# --------------------------------------------------------------- danger detector

def test_detect_danger_new_patterns():
    assert detect_danger("chmod -R 777 /") == "recursive world-writable chmod"
    assert detect_danger("echo x > /etc/passwd") == "overwrite of system config under /etc"
    assert detect_danger("sudo rm /var/log/syslog") == "privileged destructive command"


def test_detect_danger_chmod_recursive_either_order():
    # GNU chmod accepts the -R flag before OR after the mode.
    assert detect_danger("chmod 777 -R /srv") == "recursive world-writable chmod"
    assert detect_danger("chmod -fR 0777 dir") == "recursive world-writable chmod"


def test_detect_danger_sudo_with_flags_before_verb():
    assert detect_danger("echo x | sudo -n tee /etc/cron.d/x") == "privileged destructive command"
    assert detect_danger("sudo -E truncate -s 0 /etc/passwd") == "privileged destructive command"
    assert detect_danger("sudo FOO=1 chown -R root:root /etc") == "privileged destructive command"


def test_detect_danger_allows_normal_commands():
    assert detect_danger("npm run build") is None
    assert detect_danger("git status") is None
    assert detect_danger("chmod 644 file.txt") is None
    assert detect_danger("chmod -R 755 dir") is None  # recursive but not world-writable
    assert detect_danger("sudo apt-get install ripgrep") is None  # not a destructive verb
