"""run_linter and run_type_checker: auto-detect and run the project's linter / type checker.

Both delegate to run_command (so output formatting and timeouts are shared). They are
category="exec" but non-mutating: running read-only analysis is allowed even in plan mode.
"""
from __future__ import annotations

import shutil
from typing import Optional

from .base import ToolContext, ToolSpec, register
from .exec import run_command


def run_linter(ctx: ToolContext, cwd: Optional[str] = None) -> str:
    workdir = ctx.resolve(cwd) if cwd else ctx.cwd
    if (workdir / "package.json").exists() and shutil.which("npx"):
        return run_command(ctx, "npx --no-install eslint .", cwd=str(workdir), description="eslint")
    if shutil.which("ruff"):
        return run_command(ctx, "ruff check .", cwd=str(workdir), description="ruff check")
    if shutil.which("flake8"):
        return run_command(ctx, "flake8", cwd=str(workdir), description="flake8")
    if shutil.which("pylint"):
        return run_command(ctx, "pylint .", cwd=str(workdir), description="pylint")
    return ("No supported linter found (looked for eslint via npx, ruff, flake8, pylint). "
            "Install one or run a specific command with run_command.")


def run_type_checker(ctx: ToolContext, cwd: Optional[str] = None) -> str:
    workdir = ctx.resolve(cwd) if cwd else ctx.cwd
    if (workdir / "tsconfig.json").exists() and shutil.which("npx"):
        return run_command(ctx, "npx --no-install tsc --noEmit", cwd=str(workdir), description="tsc --noEmit")
    if shutil.which("mypy"):
        return run_command(ctx, "mypy .", cwd=str(workdir), description="mypy")
    if shutil.which("pyright"):
        return run_command(ctx, "pyright", cwd=str(workdir), description="pyright")
    return ("No supported type checker found (looked for tsc via npx, mypy, pyright). "
            "Install one or run a specific command with run_command.")


register(ToolSpec(
    name="run_linter",
    description="Auto-detect and run the project's linter (eslint for JS/TS; ruff/flake8/pylint for Python) to surface "
                "style and lint errors. Read-only; use after edits to catch issues before finishing.",
    parameters={"type": "object", "properties": {"cwd": {"type": "string", "description": "Directory to lint (default: agent cwd)"}}, "required": []},
    impl=run_linter, category="exec",
))

register(ToolSpec(
    name="run_type_checker",
    description="Auto-detect and run the project's type checker (tsc for TS; mypy/pyright for Python) to surface type "
                "errors. Read-only; use after edits to verify types still check out.",
    parameters={"type": "object", "properties": {"cwd": {"type": "string", "description": "Directory to type-check (default: agent cwd)"}}, "required": []},
    impl=run_type_checker, category="exec",
))
