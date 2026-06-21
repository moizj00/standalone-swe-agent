"""Audit log: append-only JSONL record of every tool call in a session.

Each line captures: timestamp, step number, tool name, arguments (truncated),
result digest, duration, and approval status. Stored in .agent/audit.log
relative to the workspace root.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


AUDIT_DIR = ".agent"
AUDIT_FILENAME = "audit.log"
MAX_ARG_CHARS = 500
MAX_RESULT_CHARS = 200


class AuditLog:
    """Append-only JSONL audit of every tool call in a session."""

    def __init__(self, cwd: Path, *, enabled: bool = True):
        self.enabled = enabled
        self.cwd = cwd
        self._path: Optional[Path] = None
        if enabled:
            self._path = cwd / AUDIT_DIR / AUDIT_FILENAME

    def record(
        self,
        step: int,
        tool_name: str,
        args: dict,
        result: str,
        duration_ms: int,
        approved: bool = True,
        blocked_reason: Optional[str] = None,
    ) -> None:
        """Append one audit entry. Best-effort: never raises."""
        if not self.enabled or self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "step": step,
                "tool": tool_name,
                "args": _truncate_dict(args, MAX_ARG_CHARS),
                "result_digest": result[:MAX_RESULT_CHARS] if result else "",
                "duration_ms": duration_ms,
                "approved": approved,
            }
            if blocked_reason:
                entry["blocked"] = blocked_reason
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass  # audit must never break the agent loop

    def path_str(self) -> Optional[str]:
        return str(self._path) if self._path else None


def _truncate_dict(d: dict, max_chars: int) -> dict:
    """Return a copy with string values truncated for audit brevity."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + "..."
        else:
            out[k] = v
    return out


class Timer:
    """Simple context-manager timer for measuring tool execution duration."""

    def __init__(self) -> None:
        self.start: float = 0
        self.elapsed_ms: int = 0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *_args) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)
