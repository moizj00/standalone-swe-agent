"""Git worktree isolation for mutating sub-agents.

A mutating sub-agent (mode "implement"/"test") must never edit the parent's
working tree directly. Instead it runs inside a dedicated git worktree under
``<root>/.agent/worktrees/<subagent_id>`` so its changes are isolated, can be
inspected as a diff, and can be discarded wholesale. Adopting a sub-agent's
patch into the parent tree is an explicit, separate step (not done here).

These helpers are pure subprocess wrappers around ``git`` -- no new
dependencies -- and degrade to clear errors when git is missing or the target
is not a repository.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Worktrees for sub-agents live here, relative to the repo root.
WORKTREE_SUBDIR = ".agent/worktrees"

_TIMEOUT = 60


def _git(args, cwd: Path, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    """Run a git command, returning the CompletedProcess (never raises for git errors)."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def is_git_repo(cwd: Path) -> bool:
    """True if ``cwd`` is inside a git working tree."""
    try:
        res = _git(["rev-parse", "--is-inside-work-tree"], Path(cwd), timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def create_subagent_worktree(root: Path, subagent_id: str) -> Path:
    """Create a detached git worktree at ``root/.agent/worktrees/<subagent_id>``.

    The worktree is forked from ``HEAD``, so it reflects the last commit and NOT
    the parent's uncommitted/staged/untracked changes -- this keeps the child on a
    clean, isolated base. Seeding the parent's in-progress changes is intentionally
    out of scope.

    Raises RuntimeError if git is unavailable or the worktree cannot be created
    (e.g. the repo has no commits yet, so HEAD does not resolve).
    """
    root = Path(root)
    container = root / WORKTREE_SUBDIR
    workspace = container / subagent_id
    container.mkdir(parents=True, exist_ok=True)
    # Keep the whole worktree container out of the parent's index so a stray
    # `git add -A` in the parent tree never stages worktree checkouts.
    gitignore = container / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    try:
        res = _git(["worktree", "add", "--detach", str(workspace), "HEAD"], root)
    except FileNotFoundError:
        raise RuntimeError("git is not installed (needed for worktree isolation)")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git worktree add timed out")
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {res.stderr.strip() or res.stdout.strip()}")
    return workspace


def repo_subdir_prefix(cwd: Path) -> str:
    """Return the repo-root-relative prefix of ``cwd`` (e.g. 'src/' or '').

    Used to run a sub-agent at the same relative location inside its worktree
    that the parent was launched from, so relative paths in tasks still resolve.
    """
    try:
        res = _git(["rev-parse", "--show-prefix"], Path(cwd), timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def collect_worktree_diff(workspace: Path) -> str:
    """Return the diff of all changes in ``workspace`` (including new and staged files).

    ``git add -A -N`` marks untracked files with intent-to-add so they appear in
    the diff without staging content; diffing against HEAD then captures both
    staged and unstaged changes a sub-agent may have made. Returns an empty
    string when there are no changes.
    """
    workspace = Path(workspace)
    try:
        _git(["add", "-A", "-N"], workspace)
        res = _git(["diff", "HEAD"], workspace)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"Error collecting diff: {e}"
    return res.stdout or ""


def remove_subagent_worktree(root: Path, workspace: Path) -> str:
    """Remove a sub-agent worktree and prune git's bookkeeping.

    Returns a short human-readable status string.
    """
    root = Path(root)
    workspace = Path(workspace)
    try:
        res = _git(["worktree", "remove", "--force", str(workspace)], root)
        if res.returncode == 0:
            return f"Removed worktree {workspace}"
        # Fall back: prune git's registry, then delete the directory ourselves.
        _git(["worktree", "prune"], root)
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        return f"Force-removed worktree {workspace} ({res.stderr.strip()})"
    except FileNotFoundError:
        return "Error: git is not installed (needed to remove worktree)"
    except subprocess.TimeoutExpired:
        return "Error: git worktree remove timed out"
