"""Runtime loop detection and escalating interventions for the agent loop.

Observes tool-call fingerprints step-by-step and intervenes when the model is
thrashing (repeated reads, oscillation, failed edit retries, no progress).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import (
    ESCALATE_API_KEY,
    ESCALATE_BASE_URL,
    ESCALATE_MODEL,
    LOOP_EDIT_RETRY_THRESHOLD,
    LOOP_EXACT_REPEAT_THRESHOLD,
    LOOP_EXACT_REPEAT_WINDOW,
    LOOP_GUARD_ENABLED,
    LOOP_MAX_ESCALATIONS_PER_SESSION,
    LOOP_NO_PROGRESS_STEPS,
    LOOP_OSCILLATION_CYCLES,
    LOOP_READ_THRASH_MIN,
    LOOP_READ_THRASH_WINDOW,
    LOOP_SUBAGENT_POLL_THRESHOLD,
)
from .tools.base import ToolSpec

READ_ONLY_TOOLS = frozenset({
    "read_file", "view_file", "cat", "read_multiple_files",
    "grep", "ls", "list_dir", "glob", "get_file_info",
    "get_project_overview", "get_directory_tree",
    "git_status", "git_diff", "git_log", "git_show",
    "todo_read", "web_search", "web_fetch", "open_page",
})

VERIFICATION_TOOLS = frozenset({
    "run_tests", "run_linter", "run_type_checker",
})

EDIT_TOOLS = frozenset({"edit", "search_replace", "multi_edit", "apply_patch"})


@dataclass
class StepRecord:
    step: int
    tool_name: str
    fingerprint: str
    mutating: bool
    verification: bool
    todo_progress: bool
    failed_edit: bool


@dataclass
class StuckSignal:
    signal: str
    severity: str  # medium | high
    reason: str
    evidence: str
    suggested_action: str


@dataclass
class LoopGuard:
    """Detect thrashing tool patterns and escalate interventions."""

    enabled: bool = LOOP_GUARD_ENABLED
    original_task: str = ""
    verbose: bool = True
    yolo: bool = False
    user_prompt_cb: Optional[Callable[[str], str]] = None
    cloud_escalate_cb: Optional[Callable[[str, str], Optional[str]]] = None

    records: List[StepRecord] = field(default_factory=list)
    loop_events: List[dict] = field(default_factory=list)
    episode_level: int = 0
    escalations_used: int = 0
    no_progress_steps: int = 0
    block_readonly_turns: int = 0
    last_signal: Optional[StuckSignal] = None
    _last_todo_hash: Optional[str] = None
    _subagent_polls: int = 0
    _had_subagent_spawn: bool = False

    # ------------------------------------------------------------------ record

    def _fingerprint(self, name: str, args: dict) -> str:
        norm: Dict[str, Any] = {}
        for k, v in sorted((args or {}).items()):
            if k in ("description", "timeout", "cwd"):
                continue
            norm[k] = v
        payload = json.dumps({"tool": name, "args": norm}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _todo_hash(self, ctx) -> Optional[str]:
        p = ctx.cwd / ".agent_todos.json"
        if not p.exists():
            return None
        try:
            return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except Exception:
            return None

    def _is_failed_edit(self, name: str, result: str) -> bool:
        if name not in EDIT_TOOLS:
            return False
        r = (result or "").strip()
        return r.startswith("Error") or "not found" in r.lower()

    def record(self, *, step: int, name: str, args: dict, result: str,
               spec: Optional[ToolSpec], ctx) -> None:
        if not self.enabled:
            return

        todo_before = self._last_todo_hash
        todo_now = self._todo_hash(ctx)
        todo_progress = (
            name == "todo_write"
            and todo_now is not None
            and todo_now != todo_before
        )
        if name == "todo_write":
            self._last_todo_hash = todo_now

        mutating = bool(spec and spec.mutating)
        verification = name in VERIFICATION_TOOLS
        failed_edit = self._is_failed_edit(name, result)

        if name == "spawn_subagent":
            self._had_subagent_spawn = True
            self._subagent_polls = 0
        elif name == "get_subagent_result":
            self._subagent_polls += 1
        else:
            if name not in ("list_active_subagents",):
                self._subagent_polls = 0

        rec = StepRecord(
            step=step,
            tool_name=name,
            fingerprint=self._fingerprint(name, args),
            mutating=mutating,
            verification=verification,
            todo_progress=todo_progress,
            failed_edit=failed_edit,
        )
        self.records.append(rec)

        made_progress = mutating or todo_progress or verification or name == "task_complete"
        if made_progress:
            self.mark_progress()
        else:
            self.no_progress_steps += 1

    def mark_progress(self) -> None:
        self.episode_level = 0
        self.no_progress_steps = 0
        self.block_readonly_turns = 0
        self.last_signal = None
        self._subagent_polls = 0
        self._had_subagent_spawn = False

    # ------------------------------------------------------------------ detect

    def _window(self, n: int) -> List[StepRecord]:
        return self.records[-n:] if self.records else []

    def _count_fingerprint(self, fp: str, window: List[StepRecord]) -> int:
        return sum(1 for r in window if r.fingerprint == fp)

    def _format_evidence(self, window: List[StepRecord]) -> str:
        counts: Dict[str, int] = {}
        for r in window:
            key = r.tool_name
            counts[key] = counts.get(key, 0) + 1
        parts = [f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
        return ", ".join(parts[:6])

    def detect(self) -> Optional[StuckSignal]:
        if not self.enabled or not self.records:
            return None

        window8 = self._window(LOOP_EXACT_REPEAT_WINDOW)
        last = self.records[-1]
        fp_count = self._count_fingerprint(last.fingerprint, window8)
        if fp_count >= LOOP_EXACT_REPEAT_THRESHOLD:
            return StuckSignal(
                signal="exact_repeat",
                severity="medium",
                reason=f"Same tool+args repeated {fp_count} times in last {len(window8)} steps",
                evidence=self._format_evidence(window8),
                suggested_action="Take a concrete action (edit, run_tests) or explain the blocker.",
            )

        window10 = self._window(LOOP_READ_THRASH_WINDOW)
        read_only = sum(1 for r in window10 if r.tool_name in READ_ONLY_TOOLS)
        mutations = sum(1 for r in window10 if r.mutating)
        if read_only >= LOOP_READ_THRASH_MIN and mutations == 0:
            return StuckSignal(
                signal="read_thrash",
                severity="medium",
                reason=f"{read_only} read-only calls, 0 edits in last {len(window10)} steps",
                evidence=self._format_evidence(window10),
                suggested_action="Stop exploring and make an edit, or call todo_write with a plan.",
            )

        if len(window8) >= 4:
            names = [r.tool_name for r in window8[-4:]]
            if names[0] == names[2] and names[1] == names[3] and names[0] != names[1]:
                cycles = 1
                for i in range(len(window8) - 3):
                    a, b, c, d = window8[i].tool_name, window8[i + 1].tool_name, window8[i + 2].tool_name, window8[i + 3].tool_name
                    if a == c and b == d and a != b:
                        cycles += 1
                if cycles >= LOOP_OSCILLATION_CYCLES:
                    return StuckSignal(
                        signal="oscillation",
                        severity="high",
                        reason=f"Alternating {names[0]} ↔ {names[1]} pattern detected",
                        evidence=self._format_evidence(window8),
                        suggested_action="Break the cycle: edit a file or summarize findings and proceed.",
                    )

        edit_window = self._window(6)
        failed = [r for r in edit_window if r.failed_edit]
        if len(failed) >= LOOP_EDIT_RETRY_THRESHOLD:
            same_fp = self._count_fingerprint(failed[-1].fingerprint, edit_window)
            if same_fp >= LOOP_EDIT_RETRY_THRESHOLD:
                return StuckSignal(
                    signal="edit_retry",
                    severity="high",
                    reason=f"Failed edit retried {same_fp} times",
                    evidence=self._format_evidence(edit_window),
                    suggested_action="Re-read the target file and fix old_string, or use write_file.",
                )

        if self.no_progress_steps >= LOOP_NO_PROGRESS_STEPS:
            return StuckSignal(
                signal="no_progress",
                severity="high",
                reason=f"{self.no_progress_steps} steps without mutation, todo progress, or verification",
                evidence=self._format_evidence(self._window(12)),
                suggested_action="Execute the next todo item or call task_complete if blocked.",
            )

        if self._had_subagent_spawn and self._subagent_polls >= LOOP_SUBAGENT_POLL_THRESHOLD:
            return StuckSignal(
                signal="subagent_spin",
                severity="medium",
                reason=f"Polled subagent results {self._subagent_polls} times without other action",
                evidence="get_subagent_result repeated polling",
                suggested_action="Use the subagent summary and continue, or spawn a new subagent.",
            )

        return None

    # ------------------------------------------------------------------ escalate

    def _next_level(self, signal: StuckSignal) -> int:
        bump = 2 if signal.severity == "high" else 1
        return min(5, max(self.episode_level + 1, bump))

    def _build_intervention(self, level: int, signal: StuckSignal) -> str:
        lines = [
            f"[LOOP_GUARD level={level}]",
            f"Detected: {signal.signal} — {signal.reason}.",
            f"Evidence: {signal.evidence}",
        ]
        if level == 1:
            lines.append(f"Nudge: {signal.suggested_action}")
        elif level == 2:
            lines.append(
                "Required: Call todo_write with a concrete 3-step plan, then execute step 1. "
                "Read-only exploration tools are blocked for this turn."
            )
        elif level == 3:
            lines.append("The agent appears stuck. Provide a hint or type 'abort' to stop.")
        elif level == 4:
            lines.append("Cloud unstick plan injected below. Follow it on the next steps.")
        else:
            lines.append("Maximum interventions exhausted. Summarize blockers and stop.")
        return "\n".join(lines)

    def check_before_model(self, *, step: int) -> Tuple[Optional[dict], Optional[str]]:
        """Return (intervention_user_message, abort_final_answer)."""
        if not self.enabled:
            return None, None

        signal = self.detect()
        if signal is None:
            return None, None

        if self.last_signal and self.last_signal.signal == signal.signal:
            level = min(5, self.episode_level + 1)
        else:
            level = self._next_level(signal)
        self.episode_level = level
        self.last_signal = signal

        self.loop_events.append({
            "step": step,
            "signal": signal.signal,
            "level": level,
            "reason": signal.reason,
        })

        if self.verbose:
            print(f"  \033[33m⚠ loop guard level {level}: {signal.signal} — {signal.reason}\033[0m")

        if level == 2:
            self.block_readonly_turns = 1

        if level == 3 and not self.yolo:
            hint = ""
            if self.user_prompt_cb:
                try:
                    hint = self.user_prompt_cb(
                        f"Loop detected ({signal.signal}): {signal.reason}\n"
                        "Continue? [c]ontinue / [a]bort / or type a hint: "
                    )
                except Exception:
                    hint = "c"
            hint = (hint or "c").strip().lower()
            if hint in ("a", "abort", "quit", "q"):
                return None, self._abort_summary(signal)
            if hint and hint not in ("c", "continue", "y", "yes"):
                return {"role": "user", "content": f"[USER_HINT]\n{hint}"}, None
            level = min(5, level + 1)
            self.episode_level = level

        if level == 4:
            cloud = self._try_cloud_escalation(signal)
            if cloud:
                return {"role": "user", "content": cloud}, None
            level = 5
            self.episode_level = level

        if level >= 5:
            return None, self._abort_summary(signal)

        return {"role": "user", "content": self._build_intervention(level, signal)}, None

    def _try_cloud_escalation(self, signal: StuckSignal) -> Optional[str]:
        if self.escalations_used >= LOOP_MAX_ESCALATIONS_PER_SESSION:
            return None
        if not self.cloud_escalate_cb:
            return None
        try:
            plan = self.cloud_escalate_cb(self.original_task, signal.reason)
        except Exception as e:
            if self.verbose:
                print(f"  \033[2mcloud escalation failed: {e}\033[0m")
            return None
        if not plan:
            return None
        self.escalations_used += 1
        return (
            f"[LOOP_GUARD level=4]\n[CLOUD_UNSTICK]\n{plan.strip()}\n\n"
            f"Original loop: {signal.signal} — {signal.reason}"
        )

    def _abort_summary(self, signal: StuckSignal) -> str:
        return (
            "[LOOP_GUARD abort]\n"
            f"Stopped after repeated {signal.signal}: {signal.reason}\n"
            f"Evidence: {signal.evidence}\n"
            f"Interventions tried: {len(self.loop_events)}; escalations: {self.escalations_used}\n"
            "Try rephrasing the task, narrowing scope, or providing a hint."
        )

    # ------------------------------------------------------------------ gate

    def should_block_tool(self, name: str, spec: Optional[ToolSpec]) -> Tuple[bool, str]:
        if not self.enabled or self.block_readonly_turns <= 0:
            return False, ""
        is_readonly = (
            name in READ_ONLY_TOOLS
            or (spec is not None and spec.category == "read" and not spec.mutating)
        )
        if not is_readonly:
            return False, ""
        return True, (
            f"[blocked: loop guard] '{name}' is read-only exploration and blocked this turn. "
            f"Call todo_write with a revised plan first."
        )

    def consume_readonly_block(self) -> None:
        if self.block_readonly_turns > 0:
            self.block_readonly_turns -= 1

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "steps_recorded": len(self.records),
            "no_progress_steps": self.no_progress_steps,
            "episode_level": self.episode_level,
            "loop_events": len(self.loop_events),
            "escalations_used": self.escalations_used,
            "last_signal": self.last_signal.signal if self.last_signal else None,
            "block_readonly_turns": self.block_readonly_turns,
        }

    def meta(self) -> dict:
        return {
            "loop_events": list(self.loop_events),
            "escalations_used": self.escalations_used,
        }


def make_cloud_escalate_cb() -> Optional[Callable[[str, str], Optional[str]]]:
    """Build a cloud escalation callback if env vars are configured."""
    if not ESCALATE_MODEL or not ESCALATE_API_KEY:
        return None
    from .providers import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        model=ESCALATE_MODEL,
        base_url=ESCALATE_BASE_URL,
        api_key=ESCALATE_API_KEY,
    )

    def cb(task: str, loop_reason: str) -> Optional[str]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You help an autonomous coding agent that is stuck in a tool loop. "
                    "Return a concise 3-5 step unstick plan. No tool calls. Be specific."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {task or '(unknown)'}\n"
                    f"Loop: {loop_reason}\n"
                    "What should the agent do next to break the loop and make progress?"
                ),
            },
        ]
        content, _ = provider.chat(messages, tools=[], temperature=0.1, stream=False)
        return content.strip() or None

    return cb