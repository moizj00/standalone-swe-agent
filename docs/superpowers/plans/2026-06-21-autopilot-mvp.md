# Autopilot MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an autonomous outer loop that turns one task into a committed, test-green branch by wrapping the existing ReAct agent with branch → commit → sandboxed test → repair.

**Architecture:** A subprocess test-runner (`sandbox.py`) reports pass/fail; a dedicated loop (`autopilot.py`) drives `agent.run_turn()` on a fresh branch, commits after each attempt, runs the tests, and re-runs the agent with the failure output injected until green or a repair cap. Git branch/commit plumbing is shared with `patcher.py`. Ralph stays a separate module.

**Tech Stack:** Python 3.10+ stdlib (`subprocess`, `dataclasses`), GitPython, the existing `swe_agent` package (`agent.py`, `config.py`, `patcher.py`), pytest.

## Global Constraints

- Python 3.10+; pure stdlib for the sandbox (no Docker, no network in code or tests).
- New code lives in the flat `swe_agent/` package (modules `sandbox.py`, `autopilot.py`); launcher is a repo-root sibling of `ollama-agent`/`ralph-agent`. Match this naming — no `worker/` tree.
- Tests are hermetic and live in `tests/test_*.py`.
- Canonical test command (the venv has no pytest; use an ephemeral uv env, no venv mutation):
  `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest <paths> -q`
- Execute on a feature branch off `main` (e.g. `feat/autopilot-mvp`); do not commit to `main`.
- Autopilot never touches `main`, never pushes to a remote, and refuses a dirty working tree.
- `success` is only `True` when tests are green (or no test command exists and edits were made). Never claim unverified success.

---

### Task 1: Sandbox test-runner (`swe_agent/sandbox.py`)

**Files:**
- Create: `swe_agent/sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `swe_agent.config.MAX_OBSERVATION_CHARS` (output cap).
- Produces:
  - `TestResult(passed: bool, exit_code: int, output: str, duration_s: float, command: list[str], skipped: bool=False)`
  - `detect_test_command(repo_path) -> list[str] | None`
  - `run_tests(repo_path, command: list[str] | None = None, timeout: int = 300) -> TestResult`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sandbox.py
"""Hermetic tests for the subprocess sandbox test-runner (no network, no Docker)."""
from __future__ import annotations

from pathlib import Path

from swe_agent.sandbox import TestResult, detect_test_command, run_tests

PYTEST = ["python", "-m", "pytest", "-q"]


def test_run_tests_passes_for_green_suite(tmp_path: Path):
    (tmp_path / "test_green.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    r = run_tests(tmp_path, PYTEST)
    assert isinstance(r, TestResult)
    assert r.passed and r.exit_code == 0 and not r.skipped


def test_run_tests_fails_for_red_suite(tmp_path: Path):
    (tmp_path / "test_red.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    r = run_tests(tmp_path, PYTEST)
    assert not r.passed and r.exit_code != 0


def test_run_tests_times_out(tmp_path: Path):
    r = run_tests(tmp_path, ["python", "-c", "import time; time.sleep(5)"], timeout=1)
    assert not r.passed and r.exit_code == 124 and "timed out" in r.output.lower()


def test_detect_pytest_from_tests_dir(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path) == ["python", "-m", "pytest", "-q"]


def test_detect_npm_from_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["npm", "test", "--silent"]


def test_detect_none_when_no_signals(tmp_path: Path):
    assert detect_test_command(tmp_path) is None


def test_run_tests_skipped_when_no_command(tmp_path: Path):
    r = run_tests(tmp_path)
    assert r.skipped and not r.passed and r.command == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest tests/test_sandbox.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'swe_agent.sandbox'`.

- [ ] **Step 3: Write the implementation**

```python
# swe_agent/sandbox.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest tests/test_sandbox.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add swe_agent/sandbox.py tests/test_sandbox.py
git commit -m "feat: subprocess sandbox test-runner (detect + run project tests)"
```

---

### Task 2: Shared git helpers in `swe_agent/patcher.py`

Extract the branch-create and worktree-commit plumbing from `apply_patch` so `autopilot` reuses it instead of duplicating GitPython calls. Behavior of `apply_patch` is unchanged — its existing tests are the safety net.

**Files:**
- Modify: `swe_agent/patcher.py` (add `new_branch`, `commit_worktree`; refactor `apply_patch` to call them)
- Test: `tests/test_patcher.py` (add two helper tests; existing 8 must stay green)

**Interfaces:**
- Produces:
  - `new_branch(repo, branch_name: str | None = None) -> str` — pick a unique name (collision → suffix; none → `auto/patch-<ts>-<rand>`), create off `HEAD`, check it out, return the name.
  - `commit_worktree(repo, message: str, author_name: str = "bot", author_email: str = "bot@example.com") -> str | None` — stage all; if the index tree equals HEAD's tree, return `None` (no-op); else commit with the given author and return the sha.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_patcher.py`)

```python
from swe_agent.patcher import commit_worktree, new_branch


def test_new_branch_creates_and_checks_out(tmp_path: Path):
    repo = _init_repo(tmp_path)
    name = new_branch(repo, "feature/y")
    assert name == "feature/y"
    assert repo.active_branch.name == "feature/y"


def test_commit_worktree_returns_none_on_noop(tmp_path: Path):
    repo = _init_repo(tmp_path)
    new_branch(repo, "wip")
    assert commit_worktree(repo, "nothing changed") is None  # clean tree -> no commit
    (tmp_path / "new.txt").write_text("hi\n", encoding="utf-8")
    sha = commit_worktree(repo, "add new.txt")
    assert sha and repo.head.commit.hexsha == sha
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest tests/test_patcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'new_branch'`.

- [ ] **Step 3: Refactor `patcher.py`**

Add these two functions (place above `apply_patch`):

```python
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
```

Then replace the branch/commit body of `apply_patch` (everything after validation) with:

```python
    base = repo.head.commit
    branch = new_branch(repo, branch_name)

    if isinstance(patch, str):
        _git_apply(root, patch, check_only=False)
    else:
        _apply_file_updates(root, patch)

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sha = commit_worktree(repo, f"Apply LLM patch: {branch} {ts}")
    if sha is None:
        log.info("apply_patch: patch is a no-op; returning base commit %s", base.hexsha[:8])
        return {"branch": branch, "commit": base.hexsha}
    return {"branch": branch, "commit": sha}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest tests/test_patcher.py tests/test_apply_patch.py -q`
Expected: PASS (8 existing + 2 new + 1 spec-path = 11 passed).

- [ ] **Step 5: Commit**

```bash
git add swe_agent/patcher.py tests/test_patcher.py
git commit -m "refactor: extract new_branch/commit_worktree helpers in patcher"
```

---

### Task 3: Autopilot loop (`swe_agent/autopilot.py`)

**Files:**
- Create: `swe_agent/autopilot.py`
- Test: `tests/test_autopilot.py`

**Interfaces:**
- Consumes: `swe_agent.patcher.new_branch`, `swe_agent.patcher.commit_worktree`, `swe_agent.sandbox.run_tests`, `swe_agent.sandbox.TestResult`; an `agent` exposing `.ctx.cwd`, `.add_user(str)`, `.run_turn() -> str`.
- Produces:
  - `AutopilotResult(branch: str, commit: str | None, success: bool, attempts: int, test_result: TestResult | None, summary: str)`
  - `run_autopilot(agent, task, *, repo_path, max_repairs=3, test_command=None, branch_name=None, verbose=True) -> AutopilotResult`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_autopilot.py
"""Hermetic tests for the autopilot loop (a FakeAgent edits files; a real tmp git repo)."""
from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from swe_agent.autopilot import AutopilotResult, run_autopilot

PYTEST = ["python", "-m", "pytest", "-q"]


class _Ctx:
    def __init__(self, cwd):
        self.cwd = cwd


class _FakeAgent:
    """Each run_turn() pops the next 'edit' (a function writing files into cwd)."""

    def __init__(self, cwd: Path, edits):
        self.ctx = _Ctx(cwd)
        self._edits = list(edits)
        self.seen = []
        self.turns = 0

    def add_user(self, text: str):
        self.seen.append(text)

    def run_turn(self) -> str:
        if self._edits:
            self._edits.pop(0)(self.ctx.cwd)
        self.turns += 1
        return "did an edit"


def _init_repo(path: Path) -> Repo:
    repo = Repo.init(path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "T")
        cw.set_value("user", "email", "t@e.com")
    (path / "README.md").write_text("# p\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    return repo


def _write_passing(p: Path):
    (p / "test_g.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")


def _write_failing(p: Path):
    (p / "test_g.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")


def test_green_on_first_try(tmp_path: Path):
    repo = _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [_write_passing])
    res = run_autopilot(agent, "add a test", repo_path=str(tmp_path),
                        test_command=PYTEST, verbose=False)
    assert isinstance(res, AutopilotResult)
    assert res.success and res.attempts == 1
    assert res.branch in {h.name for h in repo.heads} and res.commit


def test_repairs_then_passes(tmp_path: Path):
    _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [_write_failing, _write_failing, _write_passing])
    res = run_autopilot(agent, "make it pass", repo_path=str(tmp_path),
                        max_repairs=3, test_command=PYTEST, verbose=False)
    assert res.success and res.attempts == 3
    assert any("failing" in s.lower() for s in agent.seen[1:])  # failure injected on repair


def test_exhausts_and_reports_failure(tmp_path: Path):
    repo = _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [_write_failing, _write_failing])
    res = run_autopilot(agent, "cannot fix", repo_path=str(tmp_path),
                        max_repairs=1, test_command=PYTEST, verbose=False)
    assert not res.success and res.attempts == 2
    assert res.commit and len(list(repo.iter_commits(res.branch))) >= 2


def test_dirty_tree_is_refused(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError):
        run_autopilot(_FakeAgent(tmp_path, []), "t", repo_path=str(tmp_path), verbose=False)


def test_no_test_command_commits_and_reports(tmp_path: Path):
    _init_repo(tmp_path)
    agent = _FakeAgent(tmp_path, [lambda p: (p / "feature.py").write_text("x=1\n", encoding="utf-8")])
    res = run_autopilot(agent, "add feature", repo_path=str(tmp_path), verbose=False)
    assert res.test_result.skipped and res.success and res.attempts == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest tests/test_autopilot.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'swe_agent.autopilot'`.

- [ ] **Step 3: Write the implementation**

```python
# swe_agent/autopilot.py
"""Autopilot: turn one task into a committed, test-green branch (or the best attempt).

Wraps the existing ReAct agent: create a fresh branch, let the agent edit, commit,
run the project's tests, and re-run the agent with the failure output injected until
green or a repair cap. Never touches main, never pushes, refuses a dirty tree.
"""
from __future__ import annotations

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
    branch: str
    commit: Optional[str]
    success: bool
    attempts: int
    test_result: Optional[TestResult]
    summary: str


def run_autopilot(agent, task, *, repo_path, max_repairs: int = 3,
                  test_command: Optional[List[str]] = None,
                  branch_name: Optional[str] = None, verbose: bool = True) -> AutopilotResult:
    from git import Repo  # local import keeps module import-time light

    root = Path(repo_path).resolve()
    repo = Repo(str(root))
    if repo.is_dirty(untracked_files=True):
        raise RuntimeError("autopilot requires a clean working tree; commit or stash first.")

    branch = new_branch(repo, branch_name)

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
        say(f"\n\033[1m🤖 autopilot attempt {attempts}/{max_attempts}\033[0m on {branch}")
        agent.add_user(prompt)
        try:
            agent.run_turn()
        except KeyboardInterrupt:
            say("\n\033[33m🛑 autopilot: interrupted.\033[0m")
            break

        sha = commit_worktree(repo, f"autopilot attempt {attempts}: {task[:60]}",
                              "autopilot", "autopilot@local")
        if sha:
            last_commit = sha

        test_result = run_tests(root, test_command)
        if test_result.skipped:
            return AutopilotResult(branch, last_commit, success=last_commit is not None,
                                   attempts=attempts, test_result=test_result,
                                   summary="No test command detected; committed agent edits.")
        if test_result.passed:
            return AutopilotResult(branch, last_commit, success=True, attempts=attempts,
                                   test_result=test_result,
                                   summary=f"Tests pass after {attempts} attempt(s) on {branch}.")
        prompt = _REPAIR_PROMPT.format(output=test_result.output)

    return AutopilotResult(branch, last_commit, success=False, attempts=attempts,
                           test_result=test_result,
                           summary=f"Tests still failing after {attempts} attempt(s) on {branch}.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests python -m pytest tests/test_autopilot.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add swe_agent/autopilot.py tests/test_autopilot.py
git commit -m "feat: autopilot loop (branch -> agent edit -> commit -> test -> repair)"
```

---

### Task 4: CLI wiring (`swe_agent/cli.py`)

**Files:**
- Modify: `swe_agent/cli.py` (import, three flags, `main()` branch)

**Interfaces:**
- Consumes: `swe_agent.autopilot.run_autopilot`.
- Produces: CLI flags `--autopilot`, `--max-repairs` (int, default 3), `--test-command` (str, default None); autopilot path in `main()`.

- [ ] **Step 1: Add the import**

Next to `from .ralph import run_ralph`, add:
```python
from .autopilot import run_autopilot
```

- [ ] **Step 2: Add the flags** (in `parse_args`, after the `--completion-promise` argument)

```python
    p.add_argument("--autopilot", action="store_true",
                   help="Autopilot: edit a fresh branch, run the tests, and repair until green")
    p.add_argument("--max-repairs", type=int, default=3,
                   help="Autopilot: max repair attempts after the first edit (default 3)")
    p.add_argument("--test-command", default=None,
                   help="Autopilot: test command to run (shell-split); auto-detected if omitted")
```

- [ ] **Step 3: Wire `main()`**

In the `if args.task:` block, the ralph/normal branch becomes a three-way. Replace the existing run dispatch with:

```python
            try:
                if getattr(args, "autopilot", False):
                    import shlex
                    cmd = shlex.split(args.test_command) if args.test_command else None
                    res = run_autopilot(agent, args.task, repo_path=str(ctx.cwd),
                                        max_repairs=args.max_repairs, test_command=cmd)
                    print(f"\n\033[1mautopilot>\033[0m {res.summary}")
                    print(f"  branch={res.branch} commit={res.commit} "
                          f"success={res.success} attempts={res.attempts}")
                    result = res.summary
                elif getattr(args, "ralph", False):
                    result = run_ralph(
                        agent, args.task,
                        max_iterations=args.max_iterations,
                        completion_promise=args.completion_promise,
                    )
                else:
                    agent.add_user(args.task)
                    result = agent.run_turn()
                if result and not getattr(args, "autopilot", False):
                    print(f"\n\033[1mresult>\033[0m {result}")
            except KeyboardInterrupt:
                print("\n\033[33m[interrupted]\033[0m")
```

(Note: this assumes Task from the Ralph build-out already wired `--ralph`. If `--ralph` is not yet present in `main()`, add only the `autopilot`/`else` branches.)

- [ ] **Step 4: Verify syntax + smoke-test the flag**

Run:
```bash
cd "$PWD"
python3 -c "import ast; ast.parse(open('swe_agent/cli.py').read()); print('cli.py syntax OK')"
PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests \
  python -c "from swe_agent.cli import parse_args; a=parse_args(['--autopilot','--max-repairs','2','t']); print(a.autopilot, a.max_repairs, a.test_command)"
```
Expected: `cli.py syntax OK` then `True 2 None`.

- [ ] **Step 5: Commit**

```bash
git add swe_agent/cli.py
git commit -m "feat: wire --autopilot/--max-repairs/--test-command into the CLI"
```

---

### Task 5: `auto-agent` launcher

**Files:**
- Create: `auto-agent` (repo root, executable)

**Interfaces:**
- Consumes: `swe_agent.py` (`--autopilot`), `ensure-ollama.sh`.

- [ ] **Step 1: Write the launcher**

```bash
#!/usr/bin/env bash
# auto-agent — run the SWE agent in AUTOPILOT mode: edit a fresh branch, run the
# project's tests, and repair until green (or the repair cap).
#
# Usage:
#   ./auto-agent "make the failing tests pass"
#   ./auto-agent --max-repairs 5 --test-command "python -m pytest -q" "fix the parser"
#   SWE_AGENT_PROVIDER=nemotron ./auto-agent "task"     # use a cloud provider
#
# Defaults to local Ollama (repair loops run several passes). All flags after the
# script name are forwarded to swe_agent.py --autopilot.
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
AGENT_DIR="$(dirname "$SCRIPT_PATH")"
PYTHON_AGENT="$AGENT_DIR/swe_agent.py"
ENSURE_SCRIPT="$AGENT_DIR/ensure-ollama.sh"

PROVIDER="${SWE_AGENT_PROVIDER:-ollama}"
MODEL="${OLLAMA_AGENT_MODEL:-qwen2.5-coder:7b}"

if [[ "$PROVIDER" == "ollama" ]]; then
  [[ -x "$ENSURE_SCRIPT" ]] && "$ENSURE_SCRIPT" >/dev/null 2>&1 || true
  exec python3 "$PYTHON_AGENT" --provider ollama --model "$MODEL" --auto --autopilot "$@"
fi
exec python3 "$PYTHON_AGENT" --provider "$PROVIDER" --auto --autopilot "$@"
```

- [ ] **Step 2: Make it executable + syntax-check**

Run: `chmod +x auto-agent && bash -n auto-agent && echo AUTO_OK`
Expected: `AUTO_OK`.

- [ ] **Step 3: Commit**

```bash
git add auto-agent
git commit -m "feat: auto-agent launcher for autopilot mode"
```

---

## Self-Review

**Spec coverage:** sandbox (Task 1), autopilot loop incl. clean-tree refusal / repair injection / no-tests path / exhaustion (Task 3), shared git helpers (Task 2), CLI flags + AUTO approval via `--auto` in launcher (Tasks 4–5), launcher (Task 5), hermetic tests (Tasks 1 & 3). GitHub/Docker/orchestrator remain out of scope per spec. ✅

**Placeholder scan:** every code step contains real code; test commands have expected output. The only conditional note is the `--ralph` coexistence in Task 4 Step 3, which is explicit. ✅

**Type consistency:** `TestResult`/`AutopilotResult` field names and `new_branch`/`commit_worktree`/`run_tests`/`run_autopilot` signatures are identical across the tasks that define and consume them. ✅
