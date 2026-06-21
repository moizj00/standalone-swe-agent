"""Run a project's own test suite in a subprocess and report a structured result.

The autopilot loop uses this to decide whether an attempt is "green". Pure stdlib;
no Docker, no network. The interface is intentionally small so a container-backed
backend can replace ``run_tests`` later without touching callers.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import MAX_OBSERVATION_CHARS

_OUTPUT_CAP = MAX_OBSERVATION_CHARS  # truncate test output fed back to the model


@dataclass
class TestResult:
    passed: bool
    exit_code: int
    output: str
    duration_s: float
    command: List[str]
    skipped: bool = False

    __test__ = False  # not a pytest test class despite the Test* name


def detect_test_command(repo_path) -> Optional[List[str]]:
    """Best-effort guess of the project's test command, or None if unknown."""
    root = Path(repo_path)
    if (root / "pytest.ini").exists() or (root / "tests").is_dir():
        return ["python", "-m", "pytest", "-q"]
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="replace"):
        return ["python", "-m", "pytest", "-q"]
    if (root / "package.json").exists():
        return ["npm", "test", "--silent"]
    return None


def _truncate(text: str) -> str:
    if len(text) <= _OUTPUT_CAP:
        return text
    half = _OUTPUT_CAP // 2
    return f"{text[:half]}\n...[{len(text) - _OUTPUT_CAP} chars truncated]...\n{text[-half:]}"


def run_tests(repo_path, command: Optional[List[str]] = None, timeout: int = 300) -> TestResult:
    """Run ``command`` (or the detected one) in ``repo_path``; passed = exit code 0."""
    cmd = command or detect_test_command(repo_path)
    if cmd is None:
        return TestResult(passed=False, exit_code=0, output="(no test command detected)",
                          duration_s=0.0, command=[], skipped=True)
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.output or "") if isinstance(e.output, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        return TestResult(passed=False, exit_code=124,
                          output=_truncate(f"{out}{err}\n[tests timed out after {timeout}s]"),
                          duration_s=time.monotonic() - start, command=cmd)
    return TestResult(passed=(proc.returncode == 0), exit_code=proc.returncode,
                      output=_truncate((proc.stdout or "") + (proc.stderr or "")),
                      duration_s=time.monotonic() - start, command=cmd)
