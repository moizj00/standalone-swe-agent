"""Apply an LLM-produced patch onto a fresh git branch, then commit it.

This is the "apply" half of an autonomous repo-modification flow: given a patch
and a repo, create a branch off ``HEAD``, validate the patch, apply it, stage the
result, and commit — never pushing to a remote and never executing repo code.

Two patch shapes are accepted:

* a **unified diff** string (validated with ``git apply --check`` before applying), or
* a **file-update list** ``[{"path": "rel/path", "content": "<full new file>"}]``
  (paths are confined to the repo: absolute paths and ``..`` traversal are rejected).

The public entry point is :func:`apply_patch`. It returns
``{"branch": <name>, "commit": <sha>}``. If the patch is a no-op (the resulting
tree matches ``HEAD``), no duplicate commit is made and the base commit is returned.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

from git import Actor, Repo

log = logging.getLogger(__name__)

# A patch is either a unified-diff string or a list of whole-file updates.
Patch = Union[str, List[Dict[str, str]]]

_BOT = "bot"


def _short_rand(n: int = 6) -> str:
    """A short, filesystem/branch-safe random token for de-duplicating names."""
    return secrets.token_hex((n + 1) // 2)[:n]


def _unique_branch(repo: Repo, branch_name: Optional[str]) -> str:
    """Pick a branch name that does not already exist.

    A caller-supplied name that collides gets a short random suffix; with no name,
    we synthesize ``auto/patch-<timestamp>-<rand>``.
    """
    existing = {h.name for h in repo.heads}
    if branch_name:
        if branch_name not in existing:
            return branch_name
        candidate = f"{branch_name}-{_short_rand()}"
        while candidate in existing:
            candidate = f"{branch_name}-{_short_rand()}"
        return candidate
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"auto/patch-{ts}-{_short_rand()}"


def _validate_file_updates(root: Path, updates: List[Dict[str, str]]) -> None:
    """Reject malformed entries and any path that escapes the repo root."""
    root = root.resolve()
    for u in updates:
        if not isinstance(u, dict) or "path" not in u or "content" not in u:
            raise ValueError(f"file update must have 'path' and 'content': {u!r}")
        rel = u["path"]
        if os.path.isabs(rel):
            raise ValueError(f"absolute paths are not allowed: {rel!r}")
        if ".." in Path(rel).parts:
            raise ValueError(f"path traversal ('..') is not allowed: {rel!r}")
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"path escapes the repo root: {rel!r}")


def _apply_file_updates(root: Path, updates: List[Dict[str, str]]) -> None:
    """Write each update's full content, creating parent directories as needed."""
    for u in updates:
        target = (root / u["path"]).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(u["content"], encoding="utf-8")


def _git_apply(root: Path, diff: str, *, check_only: bool) -> None:
    """Run ``git apply`` (optionally ``--check``) reading the diff from stdin."""
    cmd = ["git", "-C", str(root), "apply"]
    if check_only:
        cmd.append("--check")
    proc = subprocess.run(cmd, input=diff, text=True, capture_output=True)
    if proc.returncode != 0:
        verb = "validate" if check_only else "apply"
        raise ValueError(f"git apply failed to {verb} the diff: {proc.stderr.strip()}")


def new_branch(repo, branch_name: Optional[str] = None) -> str:
    """Create a unique branch off HEAD, check it out, and return its name."""
    branch = _unique_branch(repo, branch_name)
    repo.create_head(branch, repo.head.commit).checkout()
    log.info("new_branch: created %s off %s", branch, repo.head.commit.hexsha[:8])
    return branch


def commit_worktree(repo, message: str, author_name: str = _BOT,
                    author_email: str = "bot@example.com") -> Optional[str]:
    """Stage everything and commit. Returns the sha, or None if the tree is unchanged."""
    base = repo.head.commit
    repo.git.add(A=True)
    if repo.index.write_tree().hexsha == base.tree.hexsha:
        return None
    actor = Actor(author_name, author_email)
    return repo.index.commit(message, author=actor, committer=actor).hexsha


def apply_patch(
    repo_path: str,
    patch: Patch,
    author_name: str = _BOT,
    author_email: str = "bot@example.com",
    branch_name: Optional[str] = None,
) -> Dict[str, str]:
    """Apply ``patch`` to ``repo_path`` on a new branch and commit it.

    Returns ``{"branch": <name>, "commit": <sha>}``. Raises ``ValueError`` for an
    invalid/unsafe patch (before any branch is created or files touched), and
    ``FileNotFoundError`` if ``repo_path`` is missing. Never pushes to a remote.
    """
    root = Path(repo_path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"repo_path does not exist: {repo_path}")

    repo = Repo(str(root))
    if repo.bare:
        raise ValueError("cannot apply a patch to a bare repository")

    # --- validate everything BEFORE creating a branch or touching the tree -------
    if isinstance(patch, str):
        if not patch.strip():
            raise ValueError("unified diff is empty")
        _git_apply(root, patch, check_only=True)
    elif isinstance(patch, list):
        if not patch:
            raise ValueError("file-update list is empty")
        _validate_file_updates(root, patch)
    else:
        raise TypeError(f"unsupported patch type: {type(patch).__name__}")

    base = repo.head.commit
    branch = new_branch(repo, branch_name)

    if isinstance(patch, str):
        _git_apply(root, patch, check_only=False)
    else:
        _apply_file_updates(root, patch)

    # --- idempotency: a no-op patch must not create a duplicate commit -----------
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sha = commit_worktree(repo, f"Apply LLM patch: {branch} {ts}", author_name, author_email)
    if sha is None:
        log.info("apply_patch: patch is a no-op; returning base commit %s", base.hexsha[:8])
        return {"branch": branch, "commit": base.hexsha}
    log.info("apply_patch: committed %s on %s", sha[:8], branch)
    return {"branch": branch, "commit": sha}
