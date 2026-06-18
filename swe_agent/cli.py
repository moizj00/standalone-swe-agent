"""Command-line interface: one-shot tasks and an interactive REPL with slash commands."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import llm, prompts
from .agent import Agent
from .config import (ApprovalMode, DEFAULT_MODEL, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_BASE,
                     DEFAULT_TEMPERATURE, MAX_STEPS)
from .session import (Session, build_env_context, estimate_tokens, load_project_instructions)
from .tools import ADVERTISED
from .tools.base import ToolContext
from .tools.exec import BackgroundRegistry

BANNER = """\033[1mOllama SWE Agent\033[0m — interactive mode.
Type a task, or a slash command. /help for commands, /exit to quit."""

HELP_TEXT = """Commands:
  /help              show this help
  /tools             list available tools
  /model [name]      show or switch the model
  /plan              enter plan mode (read-only)
  /approve           leave plan mode (default approval)
  /compact           summarize & shrink the conversation
  /clear             clear the conversation (keep system prompt)
  /resume [id]       resume a saved session (no id: list sessions)
  /cost              show context size / step stats
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


# --------------------------------------------------------------------------- build

def build_agent(args, approval: ApprovalMode, mock=None):
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    env = build_env_context(cwd)
    proj, proj_path = load_project_instructions(cwd)
    system = prompts.build_system_prompt(
        env_context=env, project_instructions=proj,
        plan_mode=(approval == ApprovalMode.READ_ONLY),
    )
    state = {"always_tools": set(), "always_prefixes": set()}
    ctx = ToolContext(
        cwd=cwd, approval=approval, approve_cb=make_approval_cb(state),
        bg_registry=BackgroundRegistry(), model=args.model, base_url=args.base_url,
        num_ctx=args.num_ctx, temperature=args.temperature,
    )
    agent = Agent(
        model=args.model, ctx=ctx, system_prompt=system, stream=not args.no_stream,
        verbose=True, max_steps=args.max_steps, base_url=args.base_url,
        num_ctx=args.num_ctx, temperature=args.temperature, mock=mock,
    )
    if proj_path:
        print(f"\033[2mLoaded project instructions from {proj_path}\033[0m")
    return agent, ctx


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
        print(f"~{est} tokens in context / num_ctx={agent.num_ctx}; "
              f"steps={agent.steps}; messages={len(agent.messages)}")
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
            session.save(agent.messages, meta={"model": agent.model})
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
    p = argparse.ArgumentParser(
        prog="swe_agent",
        description="Ollama-powered software-engineering coding agent.",
    )
    p.add_argument("task", nargs="?", default=None,
                   help="Task to perform. Omit for interactive mode.")
    p.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Ollama model")
    p.add_argument("--base-url", default=DEFAULT_OLLAMA_BASE, help="Ollama base URL (native API)")
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
    p.add_argument("--dry-run", action="store_true", help="Run a scripted mock loop (no Ollama)")
    p.add_argument("--no-preflight", action="store_true", help="Skip the Ollama server/model check")
    return p.parse_args(argv)


def main(argv=None):
    # Force UTF-8 output so glyphs/ANSI don't crash on Windows' cp1252 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = parse_args(argv)

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
        if not (args.plan):
            approval = ApprovalMode.YOLO  # avoid prompts in the scripted run

    if not mock and not args.no_preflight:
        ok, msg = llm.check_server(args.base_url, args.model)
        if not ok:
            print(f"\033[31m{msg}\033[0m")
            return 1

    # Resolve / create the session.
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

    agent, ctx = build_agent(args, approval, mock=mock)
    if resume_msgs:
        prior = [m for m in resume_msgs if m.get("role") != "system"]
        agent.messages += prior
        print(f"Resumed {len(prior)} prior messages.")

    print(f"\033[2mmodel={agent.model} approval={approval.value} num_ctx={agent.num_ctx} "
          f"cwd={ctx.cwd} session={session.sid}\033[0m")

    try:
        if args.task:
            agent.add_user(args.task)
            try:
                agent.run_turn()
            except KeyboardInterrupt:
                print("\n\033[33m[interrupted]\033[0m")
            try:
                session.save(agent.messages, meta={"model": agent.model})
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
