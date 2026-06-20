"""Intent gate: explore-before-mutate and scope discipline.

Ensures the agent orients on the codebase before editing, and discourages
mutations to files outside the stated task scope that were not read first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from .config import (
    INTENT_GATE_ENABLED,
    INTENT_SCOPE_BLOCK_AFTER,
    INTENT_SCOPE_READ_WINDOW,
)
from .tools.base import ToolSpec

EXPLORE_TOOLS = frozenset({
    "get_project_overview", "get_directory_tree",
    "read_file", "view_file", "cat", "read_multiple_files",
    "grep", "git_status", "glob", "ls", "list_dir",
})

_PATH_ARG_KEYS = ("path", "file", "target", "destination", "source")

_TASK_PATH_RE = re.compile(
    r"(?:[`'\"]([^`'\"]+\.[a-zA-Z0-9]+)[`'\"]|"
    r"\b([\w./-]+\.(?:py|ts|tsx|js|jsx|md|json|yaml|yml|toml|rs|go))\b)",
    re.I,
)

_NARROW_TASK_RE = re.compile(
    r"\b(edit|fix|change|update|modify|patch|replace)\b.*"
    r"(?:[`'\"]([^`'\"]+)[`'\"]|\b([\w./-]+\.[a-zA-Z0-9]+)\b)|"
    r"\b(?:in|file)\s+[`'\"]?([\w./-]+\.[a-zA-Z0-9]+)[`'\"]?|"
    r"\bline\s+\d+",
    re.I,
)


def _norm_path(path: str) -> str:
    p = path.replace("\\", "/").strip().strip("'\"`")
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def parse_task_paths(task: str) -> Set[str]:
    if not task:
        return set()
    found: Set[str] = set()
    for m in _TASK_PATH_RE.finditer(task):
        for g in m.groups():
            if g:
                found.add(_norm_path(g))
    for m in _NARROW_TASK_RE.finditer(task):
        for g in m.groups():
            if g and "." in g:
                found.add(_norm_path(g))
    return found


def is_narrow_task(task: str) -> bool:
    """Task names a specific file — exploration can be skipped."""
    if not task:
        return False
    return bool(_NARROW_TASK_RE.search(task))


def extract_mutation_path(name: str, args: dict) -> Optional[str]:
    for key in _PATH_ARG_KEYS:
        val = args.get(key)
        if val and isinstance(val, str):
            return _norm_path(val)
    return None


def extract_read_path(name: str, args: dict) -> Optional[str]:
    if name in ("read_file", "view_file", "cat"):
        return extract_mutation_path(name, args)
    if name == "grep" and args.get("path"):
        return _norm_path(str(args["path"]))
    return None


@dataclass
class ReadEvent:
    step: int
    path: str


@dataclass
class IntentGate:
    enabled: bool = INTENT_GATE_ENABLED
    original_task: str = ""
    verbose: bool = True

    explored: bool = False
    task_paths: Set[str] = field(default_factory=set)
    files_read: List[ReadEvent] = field(default_factory=list)
    files_mutated: Set[str] = field(default_factory=set)
    scope_violations: dict = field(default_factory=dict)  # path -> count
    gate_events: List[dict] = field(default_factory=list)
    mutation_count: int = 0

    def __post_init__(self) -> None:
        if self.original_task and not self.task_paths:
            self.task_paths = parse_task_paths(self.original_task)
        if is_narrow_task(self.original_task):
            self.explored = True

    def record(self, *, step: int, name: str, args: dict, spec: Optional[ToolSpec]) -> None:
        if not self.enabled:
            return

        if name in EXPLORE_TOOLS:
            self.explored = True

        read_path = extract_read_path(name, args)
        if read_path:
            self.files_read.append(ReadEvent(step=step, path=read_path))

        if spec and spec.mutating:
            self.mutation_count += 1
            mpath = extract_mutation_path(name, args)
            if mpath:
                self.files_mutated.add(mpath)

    def _read_recently(self, path: str, step: int) -> bool:
        window_start = step - INTENT_SCOPE_READ_WINDOW
        return any(
            e.path == path or e.path.endswith("/" + path) or path.endswith("/" + e.path)
            for e in self.files_read
            if e.step >= window_start
        )

    def _in_task_scope(self, path: str) -> bool:
        if not self.task_paths:
            return False
        for hint in self.task_paths:
            if path == hint or path.endswith("/" + hint) or hint.endswith("/" + path):
                return True
            if hint in path or path in hint:
                return True
        return False

    def check_mutation(self, name: str, args: dict, spec: Optional[ToolSpec], *, step: int) -> Tuple[bool, str]:
        """Return (allowed, block_message)."""
        if not self.enabled or spec is None or not spec.mutating:
            return True, ""

        if not self.explored:
            msg = (
                "[INTENT_GATE] Explore the codebase before editing. "
                "Run get_project_overview, read_file, grep, or git_status first."
            )
            self._log("explore_required", msg)
            return False, msg

        path = extract_mutation_path(name, args)
        if not path:
            return True, ""

        if self._in_task_scope(path) or self._read_recently(path, step):
            return True, ""

        count = self.scope_violations.get(path, 0) + 1
        self.scope_violations[path] = count

        if count >= INTENT_SCOPE_BLOCK_AFTER:
            msg = (
                f"[INTENT_GATE level=2] Mutation blocked: '{path}' is outside the task scope "
                f"and was not read in the last {INTENT_SCOPE_READ_WINDOW} steps. "
                f"read_file('{path}') first or narrow the task."
            )
            self._log("scope_block", msg, path=path)
            return False, msg

        msg = (
            f"[INTENT_GATE] '{path}' was not in the task and was not read recently. "
            f"read_file('{path}') before editing, or confirm it is intentional."
        )
        self._log("scope_warn", msg, path=path)
        return False, msg

    def _log(self, reason: str, message: str, **extra) -> None:
        event = {"reason": reason, "message": message[:200], **extra}
        self.gate_events.append(event)
        if self.verbose:
            print(f"  \033[33m⊘ intent gate: {reason}\033[0m")

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "explored": self.explored,
            "task_paths": sorted(self.task_paths),
            "mutation_count": self.mutation_count,
            "files_mutated": sorted(self.files_mutated),
            "gate_events": len(self.gate_events),
            "scope_violations": dict(self.scope_violations),
        }

    def meta(self) -> dict:
        return {
            "intent_gate_events": list(self.gate_events),
            "task_paths": sorted(self.task_paths),
            "files_mutated": sorted(self.files_mutated),
        }