"""Git convenience tools and apply_patch. Commit messages are passed as argv
(never interpolated into a shell string) so quotes/newlines can't break out.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional

from .base import ToolContext, ToolSpec, register


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_git(ctx: ToolContext, args, cwd: Optional[str] = None, input_text: Optional[str] = None) -> _Result:
    workdir = str(ctx.resolve(cwd)) if cwd else str(ctx.cwd)
    try:
        res = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", input=input_text, timeout=120)
        return _Result(res.returncode, res.stdout or "", res.stderr or "")
    except FileNotFoundError:
        return _Result(127, "", "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return _Result(124, "", "git command timed out")


def _fmt(res: _Result) -> str:
    out = (res.stdout or "").rstrip()
    err = (res.stderr or "").rstrip()
    if err:
        return (out + "\n[stderr]\n" + err) if out else err
    return out


def git_status(ctx: ToolContext, cwd: Optional[str] = None) -> str:
    res = _run_git(ctx, ["status", "--branch", "--porcelain"], cwd)
    if res.returncode == 127:
        return "Error: " + res.stderr
    return _fmt(res) or "(clean working tree)"


def git_diff(ctx: ToolContext, cwd: Optional[str] = None, staged: bool = False,
             path: Optional[str] = None) -> str:
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        args += ["--", path]
    res = _run_git(ctx, args, cwd)
    if res.returncode == 127:
        return "Error: " + res.stderr
    return _fmt(res) or "(no differences)"


def git_log(ctx: ToolContext, cwd: Optional[str] = None, max_count: int = 10) -> str:
    res = _run_git(ctx, ["log", "--oneline", "-n", str(int(max_count))], cwd)
    if res.returncode == 127:
        return "Error: " + res.stderr
    return _fmt(res) or "(no commits)"


def git_show(ctx: ToolContext, ref: str = "HEAD", cwd: Optional[str] = None) -> str:
    res = _run_git(ctx, ["show", str(ref)], cwd)
    if res.returncode == 127:
        return "Error: " + res.stderr
    if res.returncode != 0:
        return _fmt(res) or f"Error: could not show '{ref}'"
    return _fmt(res) or f"(no output for {ref})"


def git_commit(ctx: ToolContext, message: str, cwd: Optional[str] = None) -> str:
    add = _run_git(ctx, ["add", "-A"], cwd)
    if add.returncode == 127:
        return "Error: " + add.stderr
    if add.returncode != 0:
        return f"git add failed:\n{_fmt(add)}"
    res = _run_git(ctx, ["commit", "-m", message], cwd)
    out = _fmt(res)
    if res.returncode != 0:
        return f"git commit failed (exit {res.returncode}):\n{out}"
    return out or "Committed."


def apply_patch(ctx: ToolContext, patch: str, path: Optional[str] = None) -> str:
    workdir = str(ctx.resolve(path)) if path else str(ctx.cwd)
    if not patch.endswith("\n"):
        patch += "\n"
    try:
        res = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=workdir,
                             input=patch, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError:
        return "Error: git is not installed (needed for apply_patch)"
    except subprocess.TimeoutExpired:
        return "Error: git apply timed out"
    if res.returncode == 0:
        return "Patch applied successfully (git apply)."

    patchbin = shutil.which("patch")
    if patchbin:
        res2 = subprocess.run([patchbin, "-p1"], cwd=workdir, input=patch, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
        if res2.returncode == 0:
            return "Patch applied successfully (patch -p1)."
        return (f"Patch failed.\n[git apply] {res.stderr.strip()}\n"
                f"[patch -p1] {res2.stderr.strip()}")
    return f"Patch failed (git apply):\n{res.stderr.strip()}"


register(ToolSpec(
    name="git_status",
    description="Show git status: current branch plus staged, unstaged, and untracked files. Read-only. Use to see what has changed before staging or committing.",
    parameters={"type": "object", "properties": {"cwd": {"type": "string"}}, "required": []},
    impl=git_status, category="read",
))

register(ToolSpec(
    name="git_diff",
    description="Show the git diff of working-tree changes. Read-only. Set staged=true to diff staged changes instead; pass path to limit to one file.",
    parameters={"type": "object", "properties": {
        "cwd": {"type": "string"},
        "staged": {"type": "boolean", "default": False},
        "path": {"type": "string", "description": "Limit the diff to one file, e.g. 'src/app.py'"},
    }, "required": []},
    impl=git_diff, category="read",
))

register(ToolSpec(
    name="git_log",
    description="Show recent git commits, one line each (hash + subject). Read-only. Use to review recent history.",
    parameters={"type": "object", "properties": {
        "cwd": {"type": "string"},
        "max_count": {"type": "integer", "description": "Number of recent commits to show", "default": 10},
    }, "required": []},
    impl=git_log, category="read",
))

register(ToolSpec(
    name="git_show",
    description="Show one commit's message, author, and full diff. Read-only. Use to inspect a specific commit; defaults to HEAD.",
    parameters={"type": "object", "properties": {
        "ref": {"type": "string", "description": "Commit ref to show, e.g. 'HEAD' or a commit hash (default: HEAD)"},
        "cwd": {"type": "string"},
    }, "required": []},
    impl=git_show, category="read",
))

register(ToolSpec(
    name="git_commit",
    description="Stage all changes (git add -A) and create a commit with the given message. SAFETY: this writes a commit to git history; use only when the user asks to commit.",
    parameters={"type": "object", "properties": {
        "message": {"type": "string", "description": "Commit message, e.g. 'Fix off-by-one in pagination'"},
        "cwd": {"type": "string"},
    }, "required": ["message"]},
    impl=git_commit, mutating=True, category="write",
))

register(ToolSpec(
    name="apply_patch",
    description="Apply a unified diff / git-style patch to files (via git apply, falling back to patch -p1). SAFETY: this mutates files on disk and is not auto-reversible. For small edits to a known file, prefer edit/multi_edit; use this for multi-file or hunk-based patches.",
    parameters={"type": "object", "properties": {
        "patch": {"type": "string", "description": "Unified diff text, e.g. starting with '--- a/file.py' / '+++ b/file.py' and '@@' hunks"},
        "path": {"type": "string", "description": "Directory to apply the patch in (default: agent cwd)"},
    }, "required": ["patch"]},
    impl=apply_patch, mutating=True, category="write",
))
