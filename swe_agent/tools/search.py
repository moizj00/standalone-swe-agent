"""Search tools: grep, backed by ripgrep when available with a pure-python fallback."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .base import ToolContext, ToolSpec, register
from ._util import is_binary, should_ignore

MAX_MATCHES = 100
MAX_OUTPUT_LINES = 300


def grep(ctx: ToolContext, pattern: str, path: str = ".", glob: Optional[str] = None,
         context_lines: int = 0, ignore_case: bool = False) -> str:
    base = ctx.resolve(path)
    if not base.exists():
        return f"Error: path does not exist: {path}"
    rg = shutil.which("rg")
    if rg:
        return _grep_rg(rg, pattern, base, glob, context_lines, ignore_case)
    return _grep_py(pattern, base, glob, context_lines, ignore_case)


def _grep_rg(rg: str, pattern: str, base: Path, glob: Optional[str],
             context_lines: int, ignore_case: bool) -> str:
    cmd = [rg, "--line-number", "--no-heading", "--color=never", "--max-count", "50"]
    if ignore_case:
        cmd.append("-i")
    if context_lines:
        cmd += ["-C", str(int(context_lines))]
    if glob:
        cmd += ["-g", glob]
    cmd += ["--", pattern, str(base)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        return f"Error running ripgrep: {e}"
    out = (res.stdout or "").strip()
    if not out:
        # rg exit codes: 0 = matches, 1 = no matches, 2 = error
        if res.returncode == 2 and res.stderr.strip():
            return f"ripgrep error: {res.stderr.strip()}"
        return f"No matches for {pattern}"
    lines = out.splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        return "\n".join(lines[:MAX_OUTPUT_LINES]) + f"\n... ({len(lines) - MAX_OUTPUT_LINES} more lines)"
    return out


def _iter_files(base: Path, glob: Optional[str]) -> Iterable[Path]:
    if base.is_file():
        yield base
        return
    yield from (base.rglob(glob) if glob else base.rglob("*"))


def _grep_py(pattern: str, base: Path, glob: Optional[str],
             context_lines: int, ignore_case: bool) -> str:
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"Invalid regex: {e}"

    results = []
    count = 0
    for fp in _iter_files(base, glob):
        if not fp.is_file():
            continue
        try:
            rel = fp.relative_to(base) if base.is_dir() else fp
        except ValueError:
            rel = fp
        if should_ignore(rel):
            continue
        if is_binary(fp):
            continue
        try:
            flines = fp.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for i, line in enumerate(flines, 1):
            if not rx.search(line):
                continue
            if context_lines:
                s = max(0, i - 1 - context_lines)
                e = min(len(flines), i + context_lines)
                block = "\n".join(
                    f"{fp.as_posix()}:{j}: {flines[j - 1]}" for j in range(s + 1, e + 1)
                )
                results.append(block)
            else:
                results.append(f"{fp.as_posix()}:{i}: {line}")
            count += 1
            if count >= MAX_MATCHES:
                results.append(f"... (stopped at {MAX_MATCHES} matches)")
                return _join(results, context_lines)
    if not results:
        return f"No matches for {pattern}"
    return _join(results, context_lines)


def _join(results, context_lines: int) -> str:
    return "\n---\n".join(results) if context_lines else "\n".join(results)


register(ToolSpec(
    name="grep",
    description="Search file contents with a regular expression (ripgrep-backed). Read-only; respects .gitignore. "
                "Use to find where code lives across the repo. Narrow results with the glob filter and add context_lines to see surrounding code.",
    parameters={"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Regex pattern to search for, e.g. 'def \\w+' to find Python function definitions"},
        "path": {"type": "string", "description": "Directory or file to search (default '.')"},
        "glob": {"type": "string", "description": "File glob filter to limit which files are searched, e.g. '*.py'"},
        "context_lines": {"type": "integer", "description": "Lines of context to show before/after each match", "default": 0},
        "ignore_case": {"type": "boolean", "description": "Case-insensitive matching when true", "default": False},
    }, "required": ["pattern"]},
    impl=grep, category="read",
))
