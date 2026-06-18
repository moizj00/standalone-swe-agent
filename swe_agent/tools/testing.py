"""run_tests: auto-detect the project's test framework and run it."""
from __future__ import annotations

from typing import Optional

from .base import ToolContext, ToolSpec, register
from .exec import run_command


def run_tests(ctx: ToolContext, cwd: Optional[str] = None, command: Optional[str] = None) -> str:
    workdir = ctx.resolve(cwd) if cwd else ctx.cwd
    if command:
        return run_command(ctx, command, cwd=str(workdir), description="run tests (override)")

    if (workdir / "package.json").exists():
        cmd, desc = "npm test", "npm test"
    elif (workdir / "Cargo.toml").exists():
        cmd, desc = "cargo test", "cargo test"
    elif (workdir / "go.mod").exists():
        cmd, desc = "go test ./...", "go test"
    elif any((workdir / f).exists() for f in ("pyproject.toml", "pytest.ini", "setup.py", "tox.ini")):
        cmd, desc = "python -m pytest -q", "pytest"
    else:
        cmd, desc = "python -m pytest -q", "pytest (default)"
    return run_command(ctx, cmd, cwd=str(workdir), description=desc)


register(ToolSpec(
    name="run_tests",
    description="Auto-detect the project's test framework (pytest, npm, cargo, go) and run the tests. "
                "Pass an explicit command to override detection.",
    parameters={"type": "object", "properties": {
        "cwd": {"type": "string"},
        "command": {"type": "string", "description": "Optional explicit test command"},
    }, "required": []},
    impl=run_tests, mutating=True, category="exec",
))
