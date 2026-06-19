"""The core agent loop: model call -> tool dispatch -> observe -> repeat,
with approval gating, inline-tool-call recovery, and context compaction.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import llm, prompts
from .config import (ApprovalMode, COMPACT_KEEP_RECENT, COMPACT_THRESHOLD,
                     DEFAULT_NUM_CTX, DEFAULT_OLLAMA_BASE, DEFAULT_TEMPERATURE,
                     MAX_OBSERVATION_CHARS, MAX_STEPS, SUBAGENT_MAX_STEPS)
from .session import estimate_tokens
from .tools import ADVERTISED, TOOLS, VALID_NAMES, resolve_spec
from .tools.base import ToolContext
from .tools.exec import detect_danger


class Agent:
    def __init__(self, model: str, ctx: ToolContext, *, system_prompt: str,
                 stream: bool = True, verbose: bool = True, max_steps: int = MAX_STEPS,
                 base_url: str = DEFAULT_OLLAMA_BASE, num_ctx: int = DEFAULT_NUM_CTX,
                 temperature: float = DEFAULT_TEMPERATURE,
                 mock: Optional[Callable[[List[dict]], Tuple[str, List[dict]]]] = None,
                 event_cb: Optional[Callable[[dict], None]] = None):
        self.model = model
        self.ctx = ctx
        self.stream = stream and mock is None
        self.verbose = verbose
        self.max_steps = max_steps
        self.base_url = base_url
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.mock = mock
        # Optional structured-event sink. When set, the loop emits dicts describing
        # tokens, tool-call lifecycle, steps, and the final answer -- this is how a
        # non-stdout frontend (e.g. the HTTP/SSE server) observes a turn. The CLI
        # leaves it None and keeps its stdout rendering (verbose=True) unchanged.
        self.event_cb = event_cb
        self.messages: List[dict] = [{"role": "system", "content": system_prompt}]
        self.steps = 0
        self._prefix_printed = False

    def _emit(self, event: dict) -> None:
        if self.event_cb is None:
            return
        try:
            self.event_cb(event)
        except Exception:
            pass  # an event sink must never break the agent loop

    # ------------------------------------------------------------------ public

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def run_turn(self) -> str:
        final = ""
        for step in range(1, self.max_steps + 1):
            self.steps += 1
            self._emit({"type": "step", "n": step})
            if self.verbose:
                print(f"\n\033[2m— step {step} —\033[0m")
            try:
                content, calls = self._call_model()
            except Exception as e:
                self._emit({"type": "error", "message": str(e)})
                return f"[error calling model: {e}]"

            if not calls:
                final = content
                break

            completed = None
            for call in calls:
                args = call.get("arguments") or {}
                self._emit({"type": "tool_call", "name": call["name"], "arguments": args})
                obs = self._dispatch(call)
                self.messages.append({"role": "tool", "tool_name": call["name"], "content": obs})
                self._emit({"type": "tool_result", "name": call["name"], "content": obs})
                if call["name"] == "task_complete":
                    completed = obs

            if completed is not None:
                final = completed
                break

            self._maybe_compact()
        else:
            final = "(reached max steps without producing a final answer)"
        self._emit({"type": "final", "text": final})
        return final

    # ------------------------------------------------------------------ model

    def _print_token(self, piece: str) -> None:
        if not self._prefix_printed:
            sys.stdout.write("\033[1massistant>\033[0m ")
            self._prefix_printed = True
        sys.stdout.write(piece)
        sys.stdout.flush()

    def _token_handler(self, piece: str) -> None:
        """Fan a streamed token out to stdout (CLI) and/or the event sink (server)."""
        if self.verbose and self.stream:
            self._print_token(piece)
        self._emit({"type": "token", "text": piece})

    def _call_model(self) -> Tuple[str, List[dict]]:
        self._prefix_printed = False
        want_tokens = False
        if self.mock is not None:
            content, raw = self.mock(self.messages)
            if self.verbose and content.strip():
                print(f"\033[1massistant>\033[0m {content}")
        else:
            want_tokens = self.stream and (self.verbose or self.event_cb is not None)
            content, raw = llm.chat(
                self.messages, self.model, TOOLS, base_url=self.base_url,
                num_ctx=self.num_ctx, temperature=self.temperature, stream=self.stream,
                on_token=self._token_handler if want_tokens else None,
            )
            if self.verbose and self._prefix_printed:
                sys.stdout.write("\n")
                sys.stdout.flush()

        calls = llm.normalize(raw)
        if not calls and content:
            recovered, cleaned = llm.extract_inline_tool_calls(content, VALID_NAMES)
            if recovered:
                calls = recovered
                content = cleaned
                if self.verbose:
                    print(f"  \033[2m(recovered {len(recovered)} tool call(s) from model text)\033[0m")

        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [
                {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls
            ]
        self.messages.append(assistant)
        if content and not want_tokens:
            # The step's full assistant text, for clients that don't consume the
            # token stream. Suppressed when tokens were streamed (want_tokens) so
            # `token` and `assistant` never describe the same text twice.
            self._emit({"type": "assistant", "content": content})
        return content, calls

    # ------------------------------------------------------------------ tools

    def _gate(self, spec, name: str, args: dict) -> Tuple[bool, Optional[str]]:
        mode = self.ctx.approval
        dangerous = ""
        if spec.category == "exec" and name in ("run_command", "bash", "shell"):
            dangerous = detect_danger(args.get("command", "")) or ""

        if mode == ApprovalMode.READ_ONLY:
            # Block mutations AND any code-executing tool. run_linter/run_type_checker
            # are non-mutating but shell out to project-controlled binaries (eslint
            # config, local node_modules/.bin, mypy plugins) — i.e. arbitrary code
            # execution — so "read-only" must refuse them too, not just file writes.
            if spec.mutating or spec.category == "exec":
                return False, (f"[blocked: plan/read-only mode] '{name}' is a mutating or "
                               f"code-executing action and was not executed. Investigate "
                               f"with read-only tools and present a plan instead.")
            return True, None
        if mode == ApprovalMode.YOLO:
            return True, None

        need = False
        if spec.category == "exec":
            need = True
        elif spec.mutating:
            need = (mode == ApprovalMode.DEFAULT)
        if dangerous:
            need = True

        if not need:
            return True, None
        ok = self.ctx.approve_cb(name, args, dangerous) if self.ctx.approve_cb else False
        if ok:
            return True, None
        return False, f"[blocked: not approved by user] '{name}' was not executed."

    def _dispatch(self, call: dict) -> str:
        name = call["name"]
        args = call.get("arguments") or {}
        if self.verbose:
            preview = json.dumps(args, default=str)
            if len(preview) > 300:
                preview = preview[:300] + "...}"
            print(f"\033[36m▶ {name}\033[0m({preview})")

        spec = resolve_spec(name)
        if spec is None:
            return f"Error: unknown tool '{name}'. Available tools: {', '.join(ADVERTISED)}"

        allowed, block = self._gate(spec, name, args)
        if not allowed:
            if self.verbose:
                print(f"  \033[33m⊘ {block}\033[0m")
            return block

        try:
            result = spec.impl(self.ctx, **args)
        except TypeError as e:
            result = f"Tool '{name}' argument error: {e}"
        except Exception as e:
            result = f"Tool '{name}' raised: {e}"

        result = str(result)
        if len(result) > MAX_OBSERVATION_CHARS:
            result = result[:MAX_OBSERVATION_CHARS] + "\n... (observation truncated)"
        if self.verbose:
            print(textwrap.indent(result[:1500], "   "))
        return result

    # ------------------------------------------------------------------ compaction

    def _msg_text(self, m: dict) -> str:
        content = m.get("content") or ""
        tcs = m.get("tool_calls")
        if tcs:
            content += " " + json.dumps([t.get("function", t) for t in tcs], default=str)
        role = m.get("role", "?")
        if role == "tool":
            return f"[tool:{m.get('tool_name', '')}] {content}"
        return f"[{role}] {content}"

    def _maybe_compact(self) -> None:
        if self.num_ctx <= 0:
            return
        est = estimate_tokens(self.messages)
        if est >= COMPACT_THRESHOLD * self.num_ctx:
            if self.verbose:
                print(f"  \033[2m… context ~{est} tokens; compacting\033[0m")
            self.compact()

    def compact(self) -> str:
        if len(self.messages) <= COMPACT_KEEP_RECENT + 2:
            return "Not enough history to compact."
        system = self.messages[0]
        recent = self.messages[-COMPACT_KEEP_RECENT:]
        middle = self.messages[1:-COMPACT_KEEP_RECENT]
        convo = "\n\n".join(self._msg_text(m) for m in middle)
        try:
            summary, _ = llm.chat(
                [
                    {"role": "system", "content":
                        "You compress agent conversations. Summarize the transcript into a dense, "
                        "factual summary preserving: the task, key decisions, files read/edited, "
                        "command results, and open next steps. Be concise."},
                    {"role": "user", "content": convo[:48000]},
                ],
                self.model, [], base_url=self.base_url, num_ctx=self.num_ctx,
                temperature=0.2, stream=False, use_tools=False,
            )
        except Exception as e:
            return f"Compaction failed: {e}"
        self.messages = [system,
                         {"role": "user", "content": "[Summary of earlier conversation]\n" + summary}]
        self.messages += recent
        return "Compacted earlier history into a summary."


# --------------------------------------------------------------------------- subagent runner

def run_subagent(task: str, description: str, model: str, cwd: str, base_url: str,
                 num_ctx: int, temperature: float, parent_approval) -> str:
    """Run a full agent loop for a delegated sub-task in its own ToolContext.

    Subagents run non-interactively (a background thread cannot prompt). They inherit
    read-only mode from a read-only parent; otherwise they auto-accept edits but a
    dangerous shell command is still refused.
    """
    from .tools.exec import BackgroundRegistry

    approval = (ApprovalMode.READ_ONLY if parent_approval == ApprovalMode.READ_ONLY
                else ApprovalMode.AUTO_ACCEPT)

    def cb(name: str, args: dict, reason: str) -> bool:
        return not bool(reason)  # allow unless flagged dangerous

    bg = BackgroundRegistry()
    ctx = ToolContext(cwd=Path(cwd), approval=approval, approve_cb=cb, bg_registry=bg,
                      model=model, base_url=base_url, num_ctx=num_ctx, temperature=temperature)
    system = prompts.SUBAGENT_PROMPT + "\n\n" + prompts.BASE_SYSTEM_PROMPT
    if approval == ApprovalMode.READ_ONLY:
        system += prompts.PLAN_MODE_SUFFIX

    agent = Agent(model=model, ctx=ctx, system_prompt=system, stream=False, verbose=False,
                  max_steps=SUBAGENT_MAX_STEPS, base_url=base_url, num_ctx=num_ctx,
                  temperature=temperature)
    agent.add_user(f"[Sub-task: {description}]\n\n{task}")
    try:
        return agent.run_turn() or "(sub-agent returned no summary)"
    finally:
        bg.cleanup()
