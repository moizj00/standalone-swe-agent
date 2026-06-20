"""Quality gate: verify-before-complete and summary template enforcement.

Rejects task_complete when code was mutated without running verification tools,
or when the final summary is too vague to act on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import (
    QUALITY_GATE_ENABLED,
    QUALITY_MIN_SUMMARY_CHARS,
    QUALITY_VERIFY_WINDOW,
)
from .tools.base import ToolSpec

VERIFICATION_TOOLS = frozenset({
    "run_tests", "run_linter", "run_type_checker",
})

_VERIFY_CMD_PATTERNS = re.compile(
    r"\b(pytest|npm\s+test|npx\s+.*(?:test|lint|tsc)|tsc\b|ruff\b|eslint\b|"
    r"mypy\b|pyright\b|vitest\b|cargo\s+test|go\s+test|flake8\b|pnpm\s+test)\b",
    re.I,
)

_VAGUE_SUMMARIES = frozenset({
    "done", "fixed", "fixed it", "complete", "completed", "all done",
    "finished", "ok", "success", "task complete", "done.",
})

_VERIFY_HINTS = re.compile(
    r"\b(verify|verification|confirm|check\s+with|run\s+[`']?(?:npm|npx|pytest|tsc|ruff|eslint|mypy|vitest)|"
    r"run_tests|run_linter|run_type_checker|passed|passing)\b",
    re.I,
)

_SKIP_VERIFY_PHRASE = re.compile(
    r"verification\s+skipped\s+because|skipped\s+verification\s+because",
    re.I,
)

_READONLY_OK = re.compile(
    r"\b(listed|summarized|found|no files changed|read-only|no changes|"
    r"no modifications|survey|overview)\b",
    re.I,
)


@dataclass
class ToolEvent:
    step: int
    name: str
    mutating: bool
    verified: bool


@dataclass
class QualityGate:
    enabled: bool = QUALITY_GATE_ENABLED
    verbose: bool = True

    events: List[ToolEvent] = field(default_factory=list)
    gate_events: List[dict] = field(default_factory=list)
    mutation_count: int = 0

    def record(self, *, step: int, name: str, args: dict, result: str,
               spec: Optional[ToolSpec]) -> None:
        if not self.enabled:
            return

        mutating = bool(spec and spec.mutating)
        verified = self._is_verification(name, args, result)

        if mutating:
            self.mutation_count += 1

        self.events.append(ToolEvent(step=step, name=name, mutating=mutating, verified=verified))

    def _is_verification(self, name: str, args: dict, result: str) -> bool:
        if name in VERIFICATION_TOOLS:
            return not result.strip().lower().startswith("error")
        if name in ("run_command", "bash", "shell"):
            cmd = args.get("command", "")
            if _VERIFY_CMD_PATTERNS.search(cmd):
                return "exit code" not in result.lower() or "exit code: 0" in result.lower()
        return False

    def _recent_verification(self) -> bool:
        window = self.events[-QUALITY_VERIFY_WINDOW:]
        return any(e.verified for e in window)

    def validate_summary(self, final_summary: str, *, had_mutations: bool) -> Optional[str]:
        text = (final_summary or "").strip()
        if not text:
            return "[QUALITY_GATE] final_summary is required and cannot be empty."

        lower = text.lower()
        if lower in _VAGUE_SUMMARIES or len(text) < 20:
            return (
                "[QUALITY_GATE] final_summary is too vague. State what changed and how to verify it "
                "(e.g. 'Added type hint to foo(); verify with: npx tsc --noEmit')."
            )

        if had_mutations and len(text) < QUALITY_MIN_SUMMARY_CHARS:
            return (
                f"[QUALITY_GATE] final_summary must be at least {QUALITY_MIN_SUMMARY_CHARS} characters "
                "when code was modified. Include what changed and a verify command."
            )

        if had_mutations:
            if not _VERIFY_HINTS.search(text):
                return (
                    "[QUALITY_GATE] final_summary must include how to verify the work "
                    "(e.g. 'verify with run_tests' or a concrete command)."
                )
        else:
            if not (_VERIFY_HINTS.search(text) or _READONLY_OK.search(text)):
                return (
                    "[QUALITY_GATE] final_summary must describe what you found or did "
                    "(e.g. 'Listed 5 routers with procedure counts; read-only, no files changed')."
                )

        return None

    def check_task_complete(self, args: dict) -> Tuple[bool, str]:
        """Return (allowed, observation). If not allowed, observation is the rejection message."""
        if not self.enabled:
            return True, ""

        final_summary = args.get("final_summary", "")
        confidence = (args.get("confidence") or "").lower()
        files_changed = args.get("files_changed") or []

        had_mutations = self.mutation_count > 0 or bool(files_changed)

        summary_err = self.validate_summary(final_summary, had_mutations=had_mutations)
        if summary_err:
            self._log_rejection("summary", summary_err)
            return False, summary_err

        if had_mutations and not self._recent_verification():
            if confidence == "low" and _SKIP_VERIFY_PHRASE.search(final_summary):
                return True, ""
            msg = (
                "[QUALITY_GATE] Code was modified but not verified. "
                "Run run_tests, run_linter, or run_type_checker (or a test/lint shell command) "
                "before task_complete. To skip, use confidence='low' and explain "
                "'verification skipped because ...' in final_summary."
            )
            self._log_rejection("no_verification", msg)
            return False, msg

        return True, ""

    def _log_rejection(self, reason: str, message: str) -> None:
        self.gate_events.append({"reason": reason, "message": message[:200]})
        if self.verbose:
            print(f"  \033[33m⊘ quality gate: {reason}\033[0m")

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "mutation_count": self.mutation_count,
            "gate_events": len(self.gate_events),
            "verified_in_window": self._recent_verification(),
            "tool_events": len(self.events),
        }

    def meta(self) -> dict:
        return {
            "quality_gate_events": list(self.gate_events),
            "mutation_count": self.mutation_count,
        }