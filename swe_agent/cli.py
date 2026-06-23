"""Command-line interface: one-shot tasks and an interactive REPL with slash commands."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from . import llm, prompts
from .agent import Agent
from .audit import AuditLog
from .config import (AUDIT_ENABLED, ApprovalMode, DEFAULT_MODEL, DEFAULT_NUM_CTX,
                     DEFAULT_OLLAMA_BASE, DEFAULT_OLLAMA_MODEL, DEFAULT_PROVIDER,
                     DEFAULT_TEMPERATURE, INTENT_GATE_ENABLED, LOOP_GUARD_ENABLED,
                     MAX_STEPS, QUALITY_GATE_ENABLED, REDACT_ENABLED)
from .intent_gate import IntentGate
from .loop_guard import LoopGuard, make_cloud_escalate_cb
from .project_config import ProjectConfig, load_project_config, merge_into_args
from .providers import CLOUD_PROVIDER_NAMES, check_cloud_provider, get_provider, is_cloud_provider
from .quality_gate import QualityGate
from .session import (Session, build_env_context, estimate_tokens, load_project_instructions)
from .tools import ADVERTISED
from .tools.base import ToolContext
from .tools.exec import BackgroundRegistry

BANNER = """\033[1mSWE Agent\033[0m — interactive mode.
Type a task, or a slash command. /help for commands, /exit to quit."""

HELP_TEXT = """Commands:
  /help              show this help
  /tools             list available tools
  /model [name]      show or switch the model
  /provider [name]   show or switch provider (minimax, kimi, nemotron, openai, ollama)
  /plan              enter plan mode (read-only)
  /approve           leave plan mode (default approval)
  /compact           summarize & shrink the conversation
  /clear             clear the conversation (keep system prompt)
  /resume [id]       resume a saved session (no id: list sessions)
  /cost              show context size / step stats
  /loop              show loop-guard detector state (alias: /loop-stats)
  /loop-stats        show loop-guard detector state
  /exit              quit"""


# --------------------------------------------------------------------------- approval

def make_approval_cb(state: dict):
    def cb(name: str, args: dict, reason: str) -> bool:
        if name in state["always_tools"]:
            return True
        cmd = args.get("command", "")
        if cmd:
            for prefix in state["always_prefixes"]:
                if cmd.startswith(prefix):
                    return True

        detail = f": {cmd}" if (cmd and name in ("run_command", "bash", "shell")) else ""
        warn = f"  \033[31m⚠ {reason}\033[0m" if reason else ""
        opts = "[y]es / [n]o / [a]lways-this-tool"
        if cmd:
            opts += " / [p]=always-this-prefix"
        sys.stdout.write(f"\n\033[33mApprove {name}{detail}?\033[0m{warn}\n  {opts}\n> ")
        sys.stdout.flush()
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("a", "always"):
            state["always_tools"].add(name)
            return True
        if ans == "p" and cmd:
            toks = cmd.split()
            state["always_prefixes"].add(" ".join(toks[:2]) if len(toks) >= 2 else cmd)
            return True
        return False
    return cb


def resolve_runtime_config(args) -> None:
    """Fill model/base_url/api_key from provider preset when using a cloud backend."""
    provider = (args.provider or DEFAULT_PROVIDER).lower()
    args.provider = provider

    if is_cloud_provider(provider):
        spec = get_provider(provider)
        if args.model in (DEFAULT_MODEL, DEFAULT_OLLAMA_MODEL):
            args.model = spec.default_model
        if args.base_url == DEFAULT_OLLAMA_BASE:
            args.base_url = spec.base_url
        args.api_key = spec.resolve_api_key()
        return

    args.api_key = ""
    if args.base_url == DEFAULT_OLLAMA_BASE and provider == "ollama":
        return
    if provider == "ollama":
        args.base_url = DEFAULT_OLLAMA_BASE


# --------------------------------------------------------------------------- build

def build_agent(args, approval: ApprovalMode, mock=None, original_task: str = ""):
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    # Load project config (.agent/config.yaml)
    project_cfg = load_project_config(cwd)
    merge_into_args(args, project_cfg)
    # Project config can set the provider, which arrives after the initial
    # resolve_runtime_config in main(); re-resolve so a project-selected cloud
    # provider gets its base_url/model/api_key instead of the Ollama defaults.
    resolve_runtime_config(args)

    env = build_env_context(cwd)
    proj, proj_path = load_project_instructions(cwd)
    system = prompts.build_system_prompt(
        env_context=env, project_instructions=proj,
        plan_mode=(approval == ApprovalMode.READ_ONLY),
        provider=args.provider,
    )
    state = {"always_tools": set(), "always_prefixes": set()}
    json_mode = getattr(args, "json_mode", False)
    ctx = ToolContext(
        cwd=cwd, approval=approval, approve_cb=make_approval_cb(state),
        bg_registry=BackgroundRegistry(), model=args.model, base_url=args.base_url,
        num_ctx=args.num_ctx, temperature=args.temperature,
        provider=args.provider, api_key=args.api_key,
    )
    if getattr(args, "loop_guard", LOOP_GUARD_ENABLED):
        loop_guard = LoopGuard(
            enabled=True,
            original_task=original_task,
            verbose=not json_mode,
            yolo=(approval == ApprovalMode.YOLO),
            cloud_escalate_cb=make_cloud_escalate_cb(),
        )
    else:
        loop_guard = LoopGuard(enabled=False)
    if getattr(args, "quality_gate", QUALITY_GATE_ENABLED):
        quality_gate = QualityGate(enabled=True, verbose=not json_mode)
    else:
        quality_gate = QualityGate(enabled=False)
    if getattr(args, "intent_gate", INTENT_GATE_ENABLED):
        intent_gate = IntentGate(enabled=True, original_task=original_task, verbose=not json_mode)
    else:
        intent_gate = IntentGate(enabled=False)

    audit_log = AuditLog(cwd, enabled=AUDIT_ENABLED and not getattr(args, "no_audit", False))

    agent = Agent(
        model=args.model, ctx=ctx, system_prompt=system,
        stream=not args.no_stream and not json_mode,
        verbose=not json_mode, max_steps=args.max_steps, base_url=args.base_url,
        num_ctx=args.num_ctx, temperature=args.temperature, mock=mock,
        provider=args.provider, api_key=args.api_key,
        loop_guard=loop_guard, quality_gate=quality_gate, intent_gate=intent_gate,
        original_task=original_task, audit_log=audit_log,
        redact=REDACT_ENABLED, json_mode=json_mode,
    )
    if proj_path and not json_mode:
        print(f"\033[2mLoaded project instructions from {proj_path}\033[0m")
    if project_cfg._source and not json_mode:
        print(f"\033[2mLoaded project config from {project_cfg._source}\033[0m")
    return agent, ctx, project_cfg


# --------------------------------------------------------------------------- slash commands

def handle_slash(agent: Agent, ctx: ToolContext, line: str):
    parts = line.strip().split(maxsplit=1)
    cmd = parts[0][1:].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("exit", "quit", "q"):
        return "exit"
    if cmd == "help":
        print(HELP_TEXT)
    elif cmd == "tools":
        print("Available tools:\n  " + "\n  ".join(ADVERTISED))
    elif cmd == "model":
        if arg:
            agent.model = ctx.model = arg
            print(f"Model set to {arg}")
        else:
            print(f"Current model: {agent.model}")
    elif cmd == "provider":
        if arg:
            agent.provider = ctx.provider = arg.lower()
            spec = get_provider(arg)
            if spec:
                agent.base_url = ctx.base_url = spec.base_url
                agent.api_key = ctx.api_key = spec.resolve_api_key()
                agent.model = ctx.model = spec.default_model
                print(f"Provider set to {spec.label} (model={spec.default_model})")
            else:
                agent.base_url = ctx.base_url = DEFAULT_OLLAMA_BASE
                agent.api_key = ctx.api_key = ""
                print(f"Provider set to ollama")
        else:
            print(f"Current provider: {agent.provider}")
    elif cmd == "plan":
        ctx.approval = ApprovalMode.READ_ONLY
        print("Plan mode ON (read-only — no mutations).")
    elif cmd == "approve":
        ctx.approval = ApprovalMode.DEFAULT
        print("Plan mode OFF (default approval).")
    elif cmd == "compact":
        print(agent.compact())
    elif cmd == "clear":
        agent.messages = agent.messages[:1]
        print("Conversation cleared (system prompt kept).")
    elif cmd == "cost":
        est = estimate_tokens(agent.messages)
        loop_info = ""
        if agent.loop_guard:
            lg = agent.loop_guard.stats()
            loop_info = (
                f"; loop_events={lg['loop_events']}, "
                f"escalations={lg['escalations_used']}, "
                f"no_progress={lg['no_progress_steps']}"
            )
        print(f"~{est} tokens in context / num_ctx={agent.num_ctx}; "
              f"steps={agent.steps}; messages={len(agent.messages)}{loop_info}")
    elif cmd in ("loop", "loop-stats"):
        if not agent.loop_guard:
            print("Loop guard is disabled.")
        else:
            import json
            print(json.dumps(agent.loop_guard.stats(), indent=2))
            if agent.loop_guard.loop_events:
                print("Recent events:")
                for ev in agent.loop_guard.loop_events[-5:]:
                    print(f"  step {ev['step']}: {ev['signal']} (level {ev['level']})")
    elif cmd == "resume":
        if not arg:
            sessions = Session.list_all()
            if not sessions:
                print("No saved sessions.")
            else:
                print("Saved sessions:")
                for s in sessions[:20]:
                    print(f"  {s['id']}  ({s['size']} bytes)")
        else:
            sess = Session.load(arg)
            if not sess:
                print(f"No session {arg}.")
            else:
                prior = [m for m in sess.read_messages() if m.get("role") != "system"]
                agent.messages = agent.messages[:1] + prior
                print(f"Resumed session {arg} ({len(prior)} messages).")
    else:
        print(f"Unknown command: /{cmd}. Try /help")
    return True


# --------------------------------------------------------------------------- loops

def interactive_loop(agent: Agent, ctx: ToolContext, session: Session):
    print(BANNER)
    while True:
        try:
            line = input("\n\033[1m›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break
        if not line:
            continue
        if line.startswith("/"):
            if handle_slash(agent, ctx, line) == "exit":
                break
            continue
        agent.add_user(line)
        try:
            agent.run_turn()
        except KeyboardInterrupt:
            print("\n\033[33m[interrupted — returning to prompt]\033[0m")
        try:
            meta = {"model": agent.model, "provider": agent.provider}
            if agent.loop_guard:
                meta.update(agent.loop_guard.meta())
            if agent.quality_gate:
                meta.update(agent.quality_gate.meta())
            session.save(agent.messages, meta=meta)
        except Exception:
            pass


# --------------------------------------------------------------------------- dry-run mock

def scripted_mock():
    """A deterministic tool-call sequence to exercise the loop without Ollama."""
    script = [
        ("I'll explore the project first.",
         [{"function": {"name": "ls", "arguments": {"path": "."}}}]),
        ("Now let me list the Python files.",
         [{"function": {"name": "glob", "arguments": {"pattern": "*.py"}}}]),
        ("Dry run complete: I explored the directory and listed Python files.", []),
    ]
    state = {"i": 0}

    def mock(messages):
        i = state["i"]
        state["i"] += 1
        return script[i] if i < len(script) else ("Done.", [])
    return mock


# --------------------------------------------------------------------------- cli

def parse_args(argv=None):
    cloud_choices = list(CLOUD_PROVIDER_NAMES) + ["ollama"]
    p = argparse.ArgumentParser(
        prog="swe_agent",
        description="Software-engineering coding agent (cloud or local Ollama).",
    )
    p.add_argument("task", nargs="?", default=None,
                   help="Task to perform. Omit for interactive mode.")
    p.add_argument("--provider", "-p", default=DEFAULT_PROVIDER,
                   choices=cloud_choices,
                   help=f"LLM backend (default: {DEFAULT_PROVIDER})")
    p.add_argument("--model", "-m", default=None, help="Model name (provider default if omitted)")
    p.add_argument("--base-url", default=DEFAULT_OLLAMA_BASE,
                   help="API base URL (provider default for cloud)")
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="Context window tokens")
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--max-steps", type=int, default=MAX_STEPS)
    p.add_argument("--cwd", default=None, help="Working directory for the agent")
    p.add_argument("--plan", action="store_true", help="Plan mode: read-only, present a plan")
    p.add_argument("--auto", action="store_true", help="Auto-accept file edits (still prompt for shell)")
    p.add_argument("--yolo", action="store_true", help="Run everything without prompts (dangerous)")
    p.add_argument("--no-stream", action="store_true", help="Disable token streaming")
    p.add_argument("--resume", metavar="ID", default=None, help="Resume a saved session by id")
    p.add_argument("--continue", dest="continue_", action="store_true",
                   help="Resume the most recent session")
    p.add_argument("--list-sessions", action="store_true", help="List saved sessions and exit")
    p.add_argument("--dry-run", action="store_true", help="Run a scripted mock loop (no LLM)")
    p.add_argument("--no-preflight", action="store_true", help="Skip provider/API key check")
    p.add_argument("--no-loop-guard", action="store_true", help="Disable runtime loop detection")
    p.add_argument("--no-quality-gate", action="store_true", help="Disable verify-before-complete gate")
    p.add_argument("--no-intent-gate", action="store_true", help="Disable explore-before-mutate gate")
    # New flags from the spec
    p.add_argument("--json", dest="json_mode", action="store_true",
                   help="Headless/CI mode: suppress ANSI, output structured JSON report")
    p.add_argument("--auto-branch", action="store_true",
                   help="Auto-create agent/<slug>-<timestamp> branch for one-shot tasks")
    p.add_argument("--no-audit", action="store_true", help="Disable audit log (.agent/audit.log)")
    # Subcommands that bypass agent loop
    p.add_argument("--diff", action="store_true", help="Show pending uncommitted changes and exit")
    p.add_argument("--apply", action="store_true", help="Stage and commit all pending changes, then exit")
    p.add_argument("--revert", action="store_true", help="Discard all uncommitted changes, then exit (requires --force)")
    p.add_argument("--force", action="store_true",
                   help="Confirm destructive operations such as --revert")
    p.add_argument("--test", action="store_true", dest="run_test",
                   help="Run configured test command and exit")
    p.add_argument("--config-get", metavar="KEY", default=None,
                   help="Read a value from .agent/config.yaml")
    p.add_argument("--config-set", nargs=2, metavar=("KEY", "VALUE"), default=None,
                   help="Set a value in .agent/config.yaml")
    p.add_argument("--export", metavar="SESSION_ID", default=None,
                   help="Export a session transcript to JSON and exit")
    args = p.parse_args(argv)
    if args.model is None:
        args.model = DEFAULT_OLLAMA_MODEL
    args.api_key = ""
    # Track which args are still at defaults (for project config merging)
    args._model_defaulted = args.model == DEFAULT_OLLAMA_MODEL
    args._provider_defaulted = args.provider == DEFAULT_PROVIDER
    args._temp_defaulted = args.temperature == DEFAULT_TEMPERATURE
    args._steps_defaulted = args.max_steps == MAX_STEPS
    return args


def main(argv=None):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = parse_args(argv)
    resolve_runtime_config(args)

    # Subcommand dispatch (these exit early without spinning up an agent)
    if args.diff:
        return cmd_diff(args)
    if args.apply:
        return cmd_apply(args)
    if args.revert:
        return cmd_revert(args)
    if args.run_test:
        return cmd_test(args)
    if args.config_get:
        return cmd_config_get(args)
    if args.config_set:
        return cmd_config_set(args)
    if args.export:
        return cmd_export(args)

    if args.list_sessions:
        sessions = Session.list_all()
        if not sessions:
            print("No saved sessions.")
        for s in sessions:
            print(f"{s['id']}  ({s['size']} bytes)")
        return 0

    approval = ApprovalMode.from_flags(plan=args.plan, auto=args.auto, yolo=args.yolo)

    mock = None
    if args.dry_run:
        mock = scripted_mock()
        if not args.plan:
            approval = ApprovalMode.YOLO

    if not mock and not args.no_preflight:
        if is_cloud_provider(args.provider):
            ok, msg = check_cloud_provider(args.provider)
            if not ok:
                print(f"\033[31m{msg}\033[0m")
                return 1
            if msg != "ok":
                print(f"\033[2m{msg}\033[0m")
        else:
            resolved, msg = llm.resolve_model(args.base_url, args.model)
            if not resolved:
                print(f"\033[31m{msg}\033[0m")
                return 1
            if resolved != args.model:
                print(f"\033[33m{msg}\033[0m")
                args.model = resolved
            elif msg != "ok":
                print(f"\033[2m{msg}\033[0m")
            mem_hint = llm.low_memory_hint()
            if mem_hint:
                print(f"\033[33m{mem_hint}\033[0m")

    session = None
    resume_msgs = None
    if args.resume:
        session = Session.load(args.resume)
        if session:
            resume_msgs = session.read_messages()
        else:
            print(f"No session {args.resume}; starting a new one.")
    elif args.continue_:
        session = Session.latest()
        if session:
            resume_msgs = session.read_messages()
            print(f"Continuing session {session.sid}.")
    if session is None:
        session = Session.create()

    args.loop_guard = LOOP_GUARD_ENABLED and not args.no_loop_guard
    args.quality_gate = QUALITY_GATE_ENABLED and not args.no_quality_gate
    args.intent_gate = INTENT_GATE_ENABLED and not args.no_intent_gate
    json_mode = getattr(args, "json_mode", False)
    agent, ctx, project_cfg = build_agent(args, approval, mock=mock, original_task=args.task or "")
    if resume_msgs:
        prior = [m for m in resume_msgs if m.get("role") != "system"]
        agent.messages += prior
        if not json_mode:
            print(f"Resumed {len(prior)} prior messages.")

    if not json_mode:
        print(f"\033[2mprovider={agent.provider} model={agent.model} approval={approval.value} "
              f"num_ctx={agent.num_ctx} cwd={ctx.cwd} session={session.sid}\033[0m")

    # Auto-branch creation for one-shot tasks
    if args.task and getattr(args, "auto_branch", False):
        _auto_create_branch(ctx.cwd, args.task)

    try:
        if args.task:
            agent.add_user(args.task)
            try:
                result = agent.run_turn()
                if json_mode:
                    _emit_json_report(ctx.cwd, result, session.sid)
                elif result:
                    print(f"\n\033[1mresult>\033[0m {result}")
            except KeyboardInterrupt:
                if not json_mode:
                    print("\n\033[33m[interrupted]\033[0m")
            try:
                meta = {"model": agent.model, "provider": agent.provider}
                if agent.loop_guard:
                    meta.update(agent.loop_guard.meta())
                if agent.quality_gate:
                    meta.update(agent.quality_gate.meta())
                session.save(agent.messages, meta=meta)
            except Exception:
                pass
        else:
            interactive_loop(agent, ctx, session)
    finally:
        try:
            ctx.bg_registry.cleanup()
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------- subcommands

class _GitResult(NamedTuple):
    """Structured outcome of a git invocation so callers can branch on returncode."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        """stdout, plus stderr appended when the command failed (for printing)."""
        out = (self.stdout or "").strip()
        if not self.ok and self.stderr.strip():
            out = (out + "\n" + self.stderr.strip()).strip()
        return out


def _git_run(cwd: Path, args: list) -> _GitResult:
    """Run a git command and return its returncode, stdout, and stderr."""
    try:
        res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=30)
        return _GitResult(res.returncode, res.stdout or "", res.stderr or "")
    except FileNotFoundError:
        return _GitResult(127, "", "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return _GitResult(124, "", "git command timed out")
    except Exception as e:  # noqa: BLE001
        return _GitResult(1, "", str(e))


def _resolve_cwd(args) -> Path:
    return Path(args.cwd).resolve() if args.cwd else Path.cwd()


def cmd_diff(args) -> int:
    """Show uncommitted changes (git diff + git diff --staged)."""
    cwd = _resolve_cwd(args)
    staged_res = _git_run(cwd, ["diff", "--staged"])
    unstaged_res = _git_run(cwd, ["diff"])
    if not staged_res.ok or not unstaged_res.ok:
        # e.g. not a git repo, or git missing -- report honestly instead of
        # silently claiming a clean tree.
        msg = (staged_res.stderr or unstaged_res.stderr).strip()
        print(msg or "git diff failed.")
        return 1
    staged = staged_res.stdout.strip()
    unstaged = unstaged_res.stdout.strip()
    if staged:
        print("=== Staged ===")
        print(staged)
    if unstaged:
        print("=== Unstaged ===")
        print(unstaged)
    if not staged and not unstaged:
        print("No pending changes.")
    return 0


def cmd_apply(args) -> int:
    """Stage and commit all pending changes."""
    cwd = _resolve_cwd(args)
    add = _git_run(cwd, ["add", "-A"])
    if not add.ok:
        print(add.text())
        return 1
    commit = _git_run(cwd, ["commit", "-m", "agent: apply pending changes"])
    print(commit.text())
    return 0 if commit.ok else 1


def cmd_revert(args) -> int:
    """Discard all uncommitted changes. Destructive -- requires --force."""
    if not getattr(args, "force", False):
        print("Refusing to revert without --force: this discards ALL uncommitted and "
              "untracked changes (git checkout -- . && git clean -fd). Re-run with --force "
              "to proceed.")
        return 1
    cwd = _resolve_cwd(args)
    checkout = _git_run(cwd, ["checkout", "--", "."])
    clean = _git_run(cwd, ["clean", "-fd"])
    if not checkout.ok or not clean.ok:
        msg = (checkout.text() + "\n" + clean.text()).strip()
        print(msg or "Revert failed.")
        return 1
    print("All uncommitted changes reverted.")
    return 0


def cmd_test(args) -> int:
    """Run configured test command."""
    cwd = _resolve_cwd(args)
    project_cfg = load_project_config(cwd)
    cmd = project_cfg.test_command
    if not cmd:
        # Auto-detect
        if (cwd / "package.json").exists():
            cmd = "npm test"
        elif any((cwd / f).exists() for f in ("pyproject.toml", "pytest.ini", "setup.py")):
            cmd = "python -m pytest -q"
        elif (cwd / "Cargo.toml").exists():
            cmd = "cargo test"
        elif (cwd / "go.mod").exists():
            cmd = "go test ./..."
        else:
            cmd = "python -m pytest -q"
    print(f"Running: {cmd}")
    try:
        res = subprocess.run(cmd, shell=True, cwd=str(cwd))
        return res.returncode
    except Exception as e:
        print(f"Error: {e}")
        return 1


def cmd_config_get(args) -> int:
    """Read a value from .agent/config.yaml."""
    cwd = _resolve_cwd(args)
    project_cfg = load_project_config(cwd)
    key = args.config_get
    val = getattr(project_cfg, key, None)
    if val is None:
        print(f"{key}: (not set)")
    else:
        print(f"{key}: {val}")
    return 0


def cmd_config_set(args) -> int:
    """Set a value in .agent/config.yaml."""
    cwd = _resolve_cwd(args)
    key, value = args.config_set
    config_dir = cwd / ".agent"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"

    lines = []
    if config_path.exists():
        lines = config_path.read_text(encoding="utf-8").splitlines()

    # Simple key: value replacement or append
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}:"):
            lines[i] = f"{key}: {value}"
            found = True
            break
    if not found:
        lines.append(f"{key}: {value}")

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Set {key} = {value}")
    return 0


def cmd_export(args) -> int:
    """Export a session transcript to JSON."""
    session = Session.load(args.export)
    if not session:
        print(f"Session '{args.export}' not found.")
        return 1
    messages = session.read_messages()
    print(json.dumps(messages, indent=2, default=str))
    return 0


def _auto_create_branch(cwd: Path, task: str) -> None:
    """Create and checkout agent/<slug>-<timestamp> branch."""
    # Generate a slug from the task
    slug = "".join(c if c.isalnum() else "-" for c in task[:30]).strip("-").lower()
    slug = slug[:20] or "task"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"agent/{slug}-{timestamp}"
    res = _git_run(cwd, ["checkout", "-b", branch])
    if not res.ok:
        # non-fatal: agent can still work on the current branch. Write to stderr so
        # `--json` runs keep stdout valid JSON for headless/CI consumers.
        print(f"(could not auto-create branch {branch}: {res.stderr.strip()})", file=sys.stderr)


def _emit_json_report(cwd: Path, result: str, session_id: str) -> None:
    """In --json mode, print the structured report to stdout."""
    report_path = cwd / ".agent" / "report.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["session_id"] = session_id
            print(json.dumps(report, indent=2))
            return
        except Exception:
            pass
    # Fallback: emit a minimal report
    print(json.dumps({
        "status": "success" if result else "failed",
        "summary": result or "",
        "session_id": session_id,
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
