"""Autopilot: turn one task into a committed, test-green branch (or the best attempt).

Wraps the existing ReAct agent: create a fresh branch, let the agent edit, commit,
run the project's tests, and re-run the agent with the failure output injected until
green or a repair cap. Never touches main, never pushes, refuses a dirty tree.

Every run carries a unique ``run_id`` (also used as the branch name) so each
autonomous run is identifiable and its branch is traceable back to it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .patcher import commit_worktree, new_branch
from .sandbox import TestResult, run_tests

_REPAIR_PROMPT = (
    "The tests are still failing. Latest test output:\n\n{output}\n\n"
    "Diagnose the root cause and fix it. Do NOT delete or skip tests to make them pass."
)


@dataclass
class AutopilotResult:
    run_id: str
    branch: str
    commit: Optional[str]
    success: bool
    attempts: int
    test_result: Optional[TestResult]
    summary: str


def _new_run_id() -> str:
    """A short, unique, branch-safe id for one autopilot run."""
    return f"ap-{uuid.uuid4().hex[:10]}"


def run_autopilot(agent, task, *, repo_path, max_repairs: int = 3,
                  test_command: Optional[List[str]] = None, branch_name: Optional[str] = None,
                  run_id: Optional[str] = None, verbose: bool = True) -> AutopilotResult:
    from git import Repo  # local import keeps module import-time light

    run_id = run_id or _new_run_id()
    root = Path(repo_path).resolve()
    repo = Repo(str(root))
    if repo.is_dirty(untracked_files=True):
        raise RuntimeError("autopilot requires a clean working tree; commit or stash first.")

    branch = new_branch(repo, branch_name or f"autopilot/{run_id}")

    def say(m: str) -> None:
        if verbose:
            print(m, flush=True)

    last_commit: Optional[str] = None
    test_result: Optional[TestResult] = None
    attempts = 0
    max_attempts = 1 + max(0, max_repairs)
    prompt = task

    while attempts < max_attempts:
        attempts += 1
        say(f"\n\033[1m🤖 autopilot {run_id} — attempt {attempts}/{max_attempts}\033[0m on {branch}")
        agent.add_user(prompt)
        try:
            agent.run_turn()
        except KeyboardInterrupt:
            say("\n\033[33m🛑 autopilot: interrupted.\033[0m")
            break

        sha = commit_worktree(repo, f"autopilot {run_id} attempt {attempts}: {task[:60]}",
                              "autopilot", "autopilot@local")
        if sha:
            last_commit = sha

        test_result = run_tests(root, test_command)
        if test_result.skipped:
            return AutopilotResult(run_id=run_id, branch=branch, commit=last_commit,
                                   success=last_commit is not None, attempts=attempts,
                                   test_result=test_result,
                                   summary="No test command detected; committed agent edits.")
        if test_result.passed:
            return AutopilotResult(run_id=run_id, branch=branch, commit=last_commit,
                                   success=True, attempts=attempts, test_result=test_result,
                                   summary=f"Tests pass after {attempts} attempt(s) on {branch}.")
        prompt = _REPAIR_PROMPT.format(output=test_result.output)

    return AutopilotResult(run_id=run_id, branch=branch, commit=last_commit, success=False,
                           attempts=attempts, test_result=test_result,
                           summary=f"Tests still failing after {attempts} attempt(s) on {branch}.")
