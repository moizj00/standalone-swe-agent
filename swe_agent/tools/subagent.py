"""Sub-agent tools: spawn independent parallel agents and collect their summaries.

Each sub-agent runs a full agent loop in its own ToolContext (its own cwd, its own
private message history). Only the final summary is returned to the parent -- the
sub-agent's intermediate tool calls never pollute the parent context.

Sub-agents carry a ``mode`` describing their intent:

  - "audit"/"review"  -- read-only workers; they run in the parent cwd.
  - "implement"/"test" -- mutating workers; they run in an isolated git worktree
    under ``.agent/worktrees/<id>`` so they never edit the parent tree directly.

Mutating modes are refused when the parent is read-only. A mutating sub-agent's
changes can be inspected via get_subagent_diff and thrown away via
discard_subagent_workspace; adopting them into the parent tree is a deliberate,
separate step that this module does not perform automatically.

Known limitations (intentional for this pass):
  - spawn_subagent is registered mutating/exec, so Agent._gate blocks it entirely
    in READ_ONLY mode. Read-only (audit/review) sub-agents are therefore only
    reachable from a non-read-only parent, not under --plan or the server's
    read-only default. Relaxing the gate is deferred to a follow-up.
  - Mutating worktrees are forked from HEAD, so a child does not see the parent's
    uncommitted/staged/untracked changes. This keeps the child isolated on a clean
    committed base; seeding parent changes is out of scope for this pass.
"""
from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..config import (ApprovalMode, DEFAULT_MODEL, DEFAULT_NUM_CTX,
                      DEFAULT_OLLAMA_BASE, DEFAULT_PROVIDER, DEFAULT_TEMPERATURE,
                      SUBAGENT_MAX_WORKERS)
from ..workspaces import (collect_worktree_diff, create_subagent_worktree,
                          is_git_repo, remove_subagent_worktree,
                          repo_subdir_prefix)
from .base import ToolContext, ToolSpec, register

SUBAGENT_MODES = ("audit", "implement", "test", "review")
_READ_ONLY_MODES = {"audit", "review"}
_MUTATING_MODES = {"implement", "test"}

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=SUBAGENT_MAX_WORKERS)


@dataclass
class SubagentRecord:
    """In-memory tracking for one spawned sub-agent."""

    id: str
    description: str
    mode: str
    parent_cwd: str
    workspace: Optional[str]  # isolated worktree path; None for read-only modes
    future: Any = None
    summary: Optional[str] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.future is None:
            return "pending"
        if not self.future.done():
            return "running"
        try:
            self.future.result(timeout=0)
            return "done"
        except Exception:
            return "failed"

    @property
    def has_workspace(self) -> bool:
        return bool(self.workspace)


# sub_id -> SubagentRecord
_SUBAGENTS: dict[str, SubagentRecord] = {}

# Cap on retained records so a long-lived server doesn't grow this map without
# bound. Only finished records that hold no worktree are evictable -- anything
# running, or still owning a worktree (a diff a caller may want), is kept.
_MAX_TRACKED = 256


def _prune_tracked() -> None:
    if len(_SUBAGENTS) <= _MAX_TRACKED:
        return
    for sid in list(_SUBAGENTS.keys()):  # insertion order = oldest first
        if len(_SUBAGENTS) <= _MAX_TRACKED:
            break
        rec = _SUBAGENTS[sid]
        if rec.status in ("done", "failed") and not rec.has_workspace:
            del _SUBAGENTS[sid]


def _refresh(record: SubagentRecord) -> None:
    """Populate summary/error from the future once it has completed."""
    fut = record.future
    if fut is None or not fut.done():
        return
    try:
        record.summary = fut.result(timeout=5)
        record.error = None
    except Exception as e:  # noqa: BLE001 -- surface any failure as text
        record.error = str(e)


def spawn_subagent(ctx: ToolContext, task: str, description: str,
                   model: Optional[str] = None, cwd: Optional[str] = None,
                   mode: str = "audit") -> str:
    from ..agent import run_subagent  # lazy import breaks the tools<->agent cycle

    mode = (mode or "audit").lower()
    if mode not in SUBAGENT_MODES:
        return (f"Invalid mode '{mode}'. Valid modes: {', '.join(SUBAGENT_MODES)} "
                f"(audit/review are read-only; implement/test mutate in an isolated worktree).")

    # Mutating workers require a non-read-only parent.
    if mode in _MUTATING_MODES and ctx.approval == ApprovalMode.READ_ONLY:
        return (f"Refused: cannot spawn a '{mode}' sub-agent while the parent is in read-only "
                f"mode. Use an 'audit' or 'review' sub-agent, or re-run without read-only mode.")

    sub_id = uuid.uuid4().hex[:8]
    parent_cwd = str(ctx.resolve(cwd)) if cwd else str(ctx.cwd)
    workspace: Optional[str] = None
    use_cwd = parent_cwd

    # Mutating workers get an isolated git worktree; never fall back to the parent tree.
    if mode in _MUTATING_MODES:
        root = Path(parent_cwd)
        if not is_git_repo(root):
            return (f"Refused: a '{mode}' sub-agent needs an isolated git worktree, but "
                    f"{root} is not a git repository. Initialize git (git init && commit) or "
                    f"use an 'audit'/'review' sub-agent instead.")
        try:
            workspace = str(create_subagent_worktree(root, sub_id))
        except Exception as e:  # noqa: BLE001
            return f"Refused: could not create an isolated worktree for the sub-agent: {e}"
        # Run the child at the same repo-relative location the parent was launched
        # from, so relative paths in the task still resolve inside the worktree.
        prefix = repo_subdir_prefix(root)
        use_cwd = str(Path(workspace) / prefix) if prefix else workspace

    use_model = model or ctx.model or DEFAULT_MODEL
    base_url = ctx.base_url or DEFAULT_OLLAMA_BASE
    num_ctx = ctx.num_ctx or DEFAULT_NUM_CTX
    temperature = ctx.temperature if ctx.temperature is not None else DEFAULT_TEMPERATURE
    provider = ctx.provider or DEFAULT_PROVIDER
    api_key = ctx.api_key or ""

    fut = _EXECUTOR.submit(
        run_subagent, task, description, use_model, use_cwd,
        base_url, num_ctx, temperature, ctx.approval,
        provider=provider, api_key=api_key, mode=mode,
    )
    _SUBAGENTS[sub_id] = SubagentRecord(
        id=sub_id, description=description, mode=mode,
        parent_cwd=parent_cwd, workspace=workspace, future=fut,
    )
    _prune_tracked()
    where = f" in isolated worktree {workspace}" if workspace else ""
    return (f"Spawned {mode} sub-agent {sub_id} ('{description}'){where}, running in parallel. "
            f"Call get_subagent_result(subagent_id='{sub_id}') to collect its summary.")


def get_subagent_result(ctx: ToolContext, subagent_id: str) -> str:
    record = _SUBAGENTS.get(subagent_id)
    if record is None:
        return f"No sub-agent with id {subagent_id}. Known ids: {list(_SUBAGENTS.keys())}"
    if record.status == "running":
        return f"Sub-agent {subagent_id} ({record.mode}) is still running. Poll again shortly."

    _refresh(record)
    lines = [f"Sub-agent {subagent_id} [mode={record.mode}, status={record.status}]"]
    if record.error:
        lines.append(f"Failed: {record.error}")
    else:
        lines.append("Summary:")
        lines.append(record.summary or "(no summary)")
    if record.has_workspace:
        lines.append(f"A diff is available via get_subagent_diff(subagent_id='{subagent_id}'); "
                     f"discard the worktree with discard_subagent_workspace when done.")
    return "\n".join(lines)


def get_subagent_diff(ctx: ToolContext, subagent_id: str) -> str:
    record = _SUBAGENTS.get(subagent_id)
    if record is None:
        return f"No sub-agent with id {subagent_id}. Known ids: {list(_SUBAGENTS.keys())}"
    if not record.has_workspace:
        return (f"Sub-agent {subagent_id} ({record.mode}) has no isolated workspace "
                f"(only implement/test sub-agents produce a diff).")
    if record.status == "running":
        return (f"Sub-agent {subagent_id} is still running; collecting a diff now could race "
                f"with its own git activity. Wait for get_subagent_result to report completion.")
    diff = collect_worktree_diff(Path(record.workspace))
    if not diff.strip():
        return f"Sub-agent {subagent_id} made no changes in its workspace."
    return f"Diff from sub-agent {subagent_id} workspace ({record.workspace}):\n{diff}"


def discard_subagent_workspace(ctx: ToolContext, subagent_id: str) -> str:
    record = _SUBAGENTS.get(subagent_id)
    if record is None:
        return f"No sub-agent with id {subagent_id}. Known ids: {list(_SUBAGENTS.keys())}"
    if not record.has_workspace:
        return f"Sub-agent {subagent_id} ({record.mode}) has no workspace to discard."
    if record.status == "running":
        return (f"Refused: sub-agent {subagent_id} is still running inside its worktree. "
                f"Discarding now would delete the directory it is working in. Wait for "
                f"get_subagent_result to report completion first.")
    status = remove_subagent_worktree(Path(record.parent_cwd), Path(record.workspace))
    # Only forget the workspace if removal actually succeeded; otherwise keep the
    # reference so the orphaned worktree can be discarded again later.
    if not status.startswith("Error:"):
        record.workspace = None
    return status


def list_active_subagents(ctx: ToolContext) -> str:
    if not _SUBAGENTS:
        return "No sub-agents have been spawned yet."
    lines = []
    for sid, record in _SUBAGENTS.items():
        label = f" - {record.description}" if record.description else ""
        ws = " [worktree]" if record.has_workspace else ""
        lines.append(f"  {sid}: {record.status} ({record.mode}){ws}{label}")
    return "Sub-agents:\n" + "\n".join(lines)


register(ToolSpec(
    name="spawn_subagent",
    description="Spawn an independent sub-agent to run a self-contained sub-task in parallel; it gets its "
                "own private context and only its final summary returns to you. Use to delegate independent "
                "work (e.g. explore one module while you read another, or implement separate files at once); "
                "keep to 2-3 running at a time. Pick a mode: audit/review are read-only workers; "
                "implement/test mutate inside an isolated git worktree (not your tree) -- inspect their work "
                "with get_subagent_diff and clean up with discard_subagent_workspace. Returns a subagent_id "
                "you later pass to get_subagent_result.",
    parameters={"type": "object", "properties": {
        "task": {"type": "string", "description": "Full, self-contained task prompt for the sub-agent; it cannot see your context, so include all needed detail. E.g. 'Find where auth tokens are validated in src/ and summarize the flow'"},
        "description": {"type": "string", "description": "Short 3-5 word label for tracking, e.g. 'explore auth flow'"},
        "model": {"type": "string", "description": "Model override (default: same model as parent)"},
        "cwd": {"type": "string", "description": "Working directory for the sub-agent (default: parent cwd). For implement/test this is the repo whose worktree is forked."},
        "mode": {"type": "string", "enum": list(SUBAGENT_MODES), "description": "Worker intent (default: audit). audit/review are read-only; implement/test mutate in an isolated git worktree."},
    }, "required": ["task", "description"]},
    impl=spawn_subagent, mutating=True, category="exec",
))

register(ToolSpec(
    name="get_subagent_result",
    description="Collect the final summary from a sub-agent started with spawn_subagent. Reports the mode and "
                "status, the summary (or failure), and whether a diff is available. If it is still running, you "
                "get a 'still running' notice -- poll again shortly rather than blocking.",
    parameters={"type": "object", "properties": {"subagent_id": {"type": "string", "description": "The id returned by spawn_subagent, e.g. '1a2b3c4d'"}},
                "required": ["subagent_id"]},
    impl=get_subagent_result, category="read",
))

register(ToolSpec(
    name="get_subagent_diff",
    description="Show the git diff produced inside an implement/test sub-agent's isolated worktree, so you can "
                "review its changes before deciding whether to adopt them. Read-only sub-agents have no diff.",
    parameters={"type": "object", "properties": {"subagent_id": {"type": "string", "description": "The id returned by spawn_subagent"}},
                "required": ["subagent_id"]},
    impl=get_subagent_diff, category="read",
))

register(ToolSpec(
    name="discard_subagent_workspace",
    description="Remove an implement/test sub-agent's isolated git worktree after you have collected its result "
                "and diff. This throws away the sub-agent's uncommitted changes; it does not touch your tree.",
    parameters={"type": "object", "properties": {"subagent_id": {"type": "string", "description": "The id returned by spawn_subagent"}},
                "required": ["subagent_id"]},
    impl=discard_subagent_workspace, mutating=True, category="exec",
))

register(ToolSpec(
    name="list_active_subagents",
    description="List every sub-agent spawned this session with its id, status (running/done/failed), mode, and "
                "label. Use to check what is still running before spawning more or collecting results.",
    parameters={"type": "object", "properties": {}, "required": []},
    impl=list_active_subagents, category="read",
))
