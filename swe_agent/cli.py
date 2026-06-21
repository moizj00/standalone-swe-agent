"""Command-line interface: one-shot tasks and an interactive REPL with slash commands."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import llm, prompts
from .agent import Agent
from .config import (ApprovalMode, DEFAULT_MODEL, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_BASE,
                     DEFAULT_OLLAMA_MODEL, DEFAULT_PROVIDER, DEFAULT_TEMPERATURE,
                     INTENT_GATE_ENABLED, LOOP_GUARD_ENABLED, MAX_STEPS, QUALITY_GATE_ENABLED,
                     RALPH_STATE_FILE)
from .intent_gate import IntentGate
from .loop_guard import LoopGuard, make_cloud_escalate_cb
from .autopilot import run_autopilot
from .ralph import run_ralph
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
  /ralph <prompt>    Ralph loop: re-run a task each pass until done [--max-iterations N --completion-promise TEXT]
  /cancel-ralph      cancel an active Ralph loop
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
    env = build_env_context(cwd)
    proj, proj_path = load_project_instructions(cwd)
    system = prompts.build_system_prompt(
        env_context=env, project_instructions=proj,
        plan_mode=(approval == ApprovalMode.READ_ONLY),
        provider=args.provider,
    )
    state = {"always_tools": set(), "always_prefixes": set()}
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
            verbose=True,
            yolo=(approval == ApprovalMode.YOLO),
            cloud_escalate_cb=make_cloud_escalate_cb(),
        )
    else:
        loop_guard = LoopGuard(enabled=False)
    if getattr(args, "quality_gate", QUALITY_GATE_ENABLED):
        quality_gate = QualityGate(enabled=True, verbose=True)
    else:
        quality_gate = QualityGate(enabled=False)
    if getattr(args, "intent_gate", INTENT_GATE_ENABLED):
        intent_gate = IntentGate(enabled=True, original_task=original_task, verbose=True)
    else:
        intent_gate = IntentGate(enabled=False)
    agent = Agent(
        model=args.model, ctx=ctx, system_prompt=system, stream=not args.no_stream,
        verbose=True, max_steps=args.max_steps, base_url=args.base_url,
        num_ctx=args.num_ctx, temperature=args.temperature, mock=mock,
        provider=args.provider, api_key=args.api_key,
        loop_guard=loop_guard, quality_gate=quality_gate, intent_gate=intent_gate,
        original_task=original_task,
    )
    if proj_path:
        print(f"\033[2mLoaded project instructions from {proj_path}\033[0m")
    return agent, ctx


# --------------------------------------------------------------------------- slash commands

def _parse_ralph_args(arg: str):
    """Parse '<prompt words> [--max-iterations N] [--completion-promise TEXT]' from /ralph."""
    import shlex
    try:
        tokens = shlex.split(arg)
    except ValueError:
        tokens = arg.split()
    max_it, promise, parts, i = 0, None, [], 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--max-iterations" and i + 1 < len(tokens):
            try:
                max_it = int(tokens[i + 1])
            except ValueError:
                max_it = 0
            i += 2
        elif t == "--completion-promise" and i + 1 < len(tokens):
            promise = tokens[i + 1]
            i += 2
        else:
            parts.append(t)
            i += 1
    return " ".join(parts), max_it, promise


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
    elif cmd == "ralph":
        if not arg:
            print("Usage: /ralph <prompt> [--max-iterations N] [--completion-promise TEXT]")
        else:
            prompt, max_it, promise = _parse_ralph_args(arg)
            if not prompt:
                print("Ralph needs a task prompt.")
            else:
                run_ralph(agent, prompt, max_iterations=max_it, completion_promise=promise)
    elif cmd == "cancel-ralph":
        sf = ctx.cwd / RALPH_STATE_FILE
        if sf.exists():
            sf.unlink()
            print("Cancelled Ralph loop.")
        else:
            print("No active Ralph loop.")
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
    p.add_argument("--ralph", action="store_true",
                   help="Ralph loop: re-feed the SAME task each iteration until a completion promise or limit")
    p.add_argument("--max-iterations", type=int, default=0,
                   help="Ralph: max outer-loop iterations (0 = unlimited, capped by RALPH_HARD_CAP)")
    p.add_argument("--completion-promise", default=None,
                   help="Ralph: phrase that, inside <promise>...</promise>, ends the loop")
    p.add_argument("--autopilot", action="store_true",
                   help="Autopilot: edit a fresh branch, run the tests, and repair until green")
    p.add_argument("--max-repairs", type=int, default=3,
                   help="Autopilot: max repair attempts after the first edit (default 3)")
    p.add_argument("--test-command", default=None,
                   help="Autopilot: test command to run (shell-split); auto-detected if omitted")
    args = p.parse_args(argv)
    if args.model is None:
        args.model = DEFAULT_OLLAMA_MODEL
    args.api_key = ""
    return args


def main(argv=None):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = parse_args(argv)
    resolve_runtime_config(args)

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
    agent, ctx = build_agent(args, approval, mock=mock, original_task=args.task or "")
    if resume_msgs:
        prior = [m for m in resume_msgs if m.get("role") != "system"]
        agent.messages += prior
        print(f"Resumed {len(prior)} prior messages.")

    print(f"\033[2mprovider={agent.provider} model={agent.model} approval={approval.value} "
          f"num_ctx={agent.num_ctx} cwd={ctx.cwd} session={session.sid}\033[0m")

    try:
        if args.task:
            try:
                if getattr(args, "autopilot", False):
                    import shlex
                    cmd = shlex.split(args.test_command) if args.test_command else None
                    res = run_autopilot(agent, args.task, repo_path=str(ctx.cwd),
                                        max_repairs=args.max_repairs, test_command=cmd)
                    print(f"\n\033[1mautopilot>\033[0m {res.summary}")
                    print(f"  run_id={res.run_id} branch={res.branch} commit={res.commit} "
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


if __name__ == "__main__":
    sys.exit(main())