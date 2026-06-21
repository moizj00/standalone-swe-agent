# Autopilot MVP — Design

## Context

`standalone-swe-agent` already contains a capable ReAct coding agent (`swe_agent/agent.py`)
that edits files and runs commands in a multi-step loop, a git patch helper
(`swe_agent/patcher.py`, `apply_patch` — branch + commit), and a Ralph loop
(`swe_agent/ralph.py` — re-feed the same task until a completion promise). What it
lacks is an **autonomous outer loop** that turns a single task into a *verified*
result: edit → commit → run the project's tests → repair on failure → stop when green.

This spec covers the **first slice** of a larger "autonomous repo-automation agent."
The larger system (task intake server, LLM single-shot patch generation, GitHub/PR
integration, a planner/implementer/tester/reviewer orchestrator) is explicitly **out
of scope** here and will each get their own spec. We chose the *wrap-and-reuse*
architecture: the existing ReAct agent does the real editing; this layer adds the
scaffolding around it.

## Goal

One non-interactive command turns a task into a **committed, test-green branch** — or,
if it can't get green within a bounded number of attempts, the **best honest attempt**
(branch still committed, with a truthful failure report). Never touches `main`, never
pushes.

## Scope

**In scope**
- `swe_agent/sandbox.py` — detect and run the project's tests in a subprocess; return a structured result.
- `swe_agent/autopilot.py` — the orchestrator loop (branch → edit → commit → test → repair).
- A small shared git helper extracted from `patcher.py` (`new_branch`, `commit_worktree`) so autopilot and `apply_patch` don't duplicate plumbing.
- `auto-agent` launcher + `--autopilot` / `--max-repairs` / `--test-command` CLI flags wired into `swe_agent/cli.py` (mirroring the existing `--ralph` wiring).
- Hermetic tests: `tests/test_sandbox.py`, `tests/test_autopilot.py`.

**Out of scope (separate later sub-projects)**
- GitHub auth, push, and draft-PR creation.
- Docker-based sandbox isolation (the sandbox interface is designed so a Docker backend can drop in later).
- A planner/orchestrator across multiple tickets.
- A REST/server intake (CLI only for now).

## Components

### 1. `swe_agent/sandbox.py`

```python
@dataclass
class TestResult:
    passed: bool
    exit_code: int
    output: str            # combined stdout+stderr, truncated to a cap for LLM feedback
    duration_s: float
    command: list[str]     # the command actually run
    skipped: bool = False  # True when no test command could be detected

def detect_test_command(repo_path) -> list[str] | None:
    """pytest (pytest.ini / pyproject [tool.pytest] / a tests/ dir) → ['python','-m','pytest','-q'];
    else npm test (package.json) → ['npm','test']; else None."""

def run_tests(repo_path, command=None, timeout=300) -> TestResult:
    """Run `command` (or the detected one) via subprocess in repo_path, capture
    stdout+stderr, passed = (exit_code == 0). A timeout yields passed=False with a
    clear '...timed out...' output. No detected command and none supplied → skipped=True."""
```

- Output is truncated to a sandbox cap (reuse/share `MAX_OBSERVATION_CHARS` from `config.py`) so it is safe to feed back into the model.
- Pure stdlib (`subprocess`); no Docker, no network.

### 2. `swe_agent/autopilot.py`

```python
@dataclass
class AutopilotResult:
    branch: str
    commit: str | None         # last commit sha on the branch (None if nothing committed)
    success: bool              # tests green, or edits made when no tests are detected
    attempts: int              # agent runs performed (1 + repairs)
    test_result: TestResult | None
    summary: str

def run_autopilot(agent, task, *, repo_path, max_repairs=3, test_command=None,
                  branch_name=None, verbose=True) -> AutopilotResult: ...
```

Control flow:
1. Open the repo; **require a clean working tree** (else raise with a clear message — never clobber uncommitted work). Create a fresh branch off `HEAD` (`new_branch`).
2. **Attempt 1:** `agent.add_user(task)` → `agent.run_turn()` (the ReAct agent edits the branch) → `commit_worktree(...)` (skip if no changes).
3. `run_tests(repo_path, test_command)`.
   - `passed` (or `skipped` with edits made) → `success=True`, return.
4. **Repair:** while attempts ≤ `1 + max_repairs` and not green: inject the failure —
   `agent.add_user("The tests are still failing:\n<output>\nFix the cause and re-run.")` →
   `run_turn()` → commit → re-test.
5. Exhausted without green → return `success=False` with the **best-attempt branch still committed** and the last `TestResult`.

Reuses `Agent.run_turn`, `sandbox.run_tests`, and the shared git helpers. It is
Ralph-flavored (prior work persists on the branch across attempts) but its stop
condition is **tests-green**, not a `<promise>`; Ralph remains its own module.

### 3. CLI / launcher

- `auto-agent` (sibling of `ollama-agent`/`cloud-agent`/`ralph-agent`/`hybrid-agent`):
  `exec python3 swe_agent.py --autopilot "$@"`, defaulting to a cheap local provider
  (`ollama`) since repair loops run several passes; overridable via `--provider`/env.
- `swe_agent/cli.py`: add `--autopilot`, `--max-repairs N` (default 3),
  `--test-command "..."`. In `main()`, when `--autopilot` is set, call `run_autopilot`
  instead of `run_turn` (same shape as the existing `--ralph` branch). Runs the agent
  in **AUTO** approval so it is non-interactive.

## Error handling & safety

- Operates only on a **fresh branch**; never `main`, never pushes to a remote.
- **Dirty working tree → hard refuse** (clear error) so uncommitted work is never lost.
- Agent exception / `KeyboardInterrupt` / test timeout → stop gracefully, leave the
  branch as-is, return a truthful `AutopilotResult`.
- No test command detected → run the agent once, commit, report
  `success` = (edits were made), and do **not** enter the repair loop.
- The truthfulness rule: a non-green run reports `success=False` — autopilot never
  claims success it cannot demonstrate.

## Testing (TDD, hermetic — no network, no Docker)

- `tests/test_sandbox.py`: in `tmp_path`, a passing test file → `passed`; a failing
  one → not passed; a `sleep` longer than a tiny timeout → timeout path with
  `passed=False`; `detect_test_command` returns pytest vs npm vs `None` for the right
  fixtures.
- `tests/test_autopilot.py`: a real `tmp_path` git repo (GitPython, as in
  `test_patcher.py`) + a `FakeAgent` (as in `test_ralph.py`) that "edits" by writing
  files. Cases: pass-on-first-try → `attempts == 1`; fail-twice-then-pass →
  `attempts == 3` and the failure output was injected into the agent's prompts;
  exhaust → `success is False` but the branch exists with commits.

## Verification

```bash
# unit tests (ephemeral env; no venv mutation)
PYTHONPATH="$PWD/tests" uv run --no-project --with pytest --with gitpython --with requests \
  python -m pytest tests/test_sandbox.py tests/test_autopilot.py -q

# launcher + flag smoke (hermetic dry-run; no LLM)
bash -n auto-agent
python3 swe_agent.py --autopilot --max-repairs 1 --dry-run --cwd /tmp/at-demo "add a docstring"
```

Manual end-to-end (real model) is a non-hermetic check, run by hand.

## Naming

Follows repo conventions (flat `swe_agent/` modules + sibling launcher), consistent
with `patcher.py`/`ralph.py` and the `*-agent` launchers, per the maintainer's
naming preference.
