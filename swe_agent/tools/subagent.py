"""Sub-agent tools: spawn independent parallel agents and collect their summaries.

Each sub-agent runs a full agent loop in its own ToolContext (its own cwd, its own
private message history). Only the final summary is returned to the parent -- the
sub-agent's intermediate tool calls never pollute the parent context.
"""
from __future__ import annotations

import concurrent.futures
import uuid
from typing import Optional

from ..config import (DEFAULT_MODEL, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_BASE,
                      DEFAULT_PROVIDER, DEFAULT_TEMPERATURE, SUBAGENT_MAX_WORKERS)
from .base import ToolContext, ToolSpec, register

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=SUBAGENT_MAX_WORKERS)
_RESULTS: dict = {}
_META: dict = {}  # sub_id -> short description


def spawn_subagent(ctx: ToolContext, task: str, description: str,
                   model: Optional[str] = None, cwd: Optional[str] = None) -> str:
    from ..agent import run_subagent  # lazy import breaks the tools<->agent cycle

    sub_id = uuid.uuid4().hex[:8]
    use_cwd = str(ctx.resolve(cwd)) if cwd else str(ctx.cwd)
    use_model = model or ctx.model or DEFAULT_MODEL
    base_url = ctx.base_url or DEFAULT_OLLAMA_BASE
    num_ctx = ctx.num_ctx or DEFAULT_NUM_CTX
    temperature = ctx.temperature if ctx.temperature is not None else DEFAULT_TEMPERATURE

    provider = ctx.provider or DEFAULT_PROVIDER
    api_key = ctx.api_key or ""
    fut = _EXECUTOR.submit(
        run_subagent, task, description, use_model, use_cwd,
        base_url, num_ctx, temperature, ctx.approval,
        provider=provider, api_key=api_key,
    )
    _RESULTS[sub_id] = fut
    _META[sub_id] = description
    return (f"Spawned sub-agent {sub_id} ('{description}'), running in parallel. "
            f"Call get_subagent_result(subagent_id='{sub_id}') to collect its summary.")


def get_subagent_result(ctx: ToolContext, subagent_id: str) -> str:
    fut = _RESULTS.get(subagent_id)
    if fut is None:
        return f"No sub-agent with id {subagent_id}. Known ids: {list(_RESULTS.keys())}"
    if not fut.done():
        return f"Sub-agent {subagent_id} is still running. Poll again shortly."
    try:
        return f"Sub-agent {subagent_id} result:\n{fut.result(timeout=5)}"
    except Exception as e:
        return f"Sub-agent {subagent_id} failed: {e}"


register(ToolSpec(
    name="spawn_subagent",
    description="Spawn an independent sub-agent to run a self-contained sub-task in parallel; it gets its "
                "own private context and only its final summary returns to you. Use to delegate independent "
                "work (e.g. explore one module while you read another, or implement separate files at once); "
                "keep to 2-3 running at a time. Returns a subagent_id you later pass to get_subagent_result.",
    parameters={"type": "object", "properties": {
        "task": {"type": "string", "description": "Full, self-contained task prompt for the sub-agent; it cannot see your context, so include all needed detail. E.g. 'Find where auth tokens are validated in src/ and summarize the flow'"},
        "description": {"type": "string", "description": "Short 3-5 word label for tracking, e.g. 'explore auth flow'"},
        "model": {"type": "string", "description": "Model override (default: same model as parent)"},
        "cwd": {"type": "string", "description": "Working directory for the sub-agent (default: parent cwd)"},
    }, "required": ["task", "description"]},
    impl=spawn_subagent, mutating=True, category="exec",
))

def list_active_subagents(ctx: ToolContext) -> str:
    if not _RESULTS:
        return "No sub-agents have been spawned yet."
    lines = []
    for sid, fut in _RESULTS.items():
        if not fut.done():
            status = "running"
        else:
            try:
                fut.result(timeout=0)
                status = "done"
            except Exception as e:
                status = f"failed ({e})"
        desc = _META.get(sid, "")
        lines.append(f"  {sid}: {status}" + (f" - {desc}" if desc else ""))
    return "Sub-agents:\n" + "\n".join(lines)


register(ToolSpec(
    name="get_subagent_result",
    description="Collect the final summary from a sub-agent started with spawn_subagent. If it is still "
                "running, you get a 'still running' notice -- poll again shortly rather than blocking.",
    parameters={"type": "object", "properties": {"subagent_id": {"type": "string", "description": "The id returned by spawn_subagent, e.g. '1a2b3c4d'"}},
                "required": ["subagent_id"]},
    impl=get_subagent_result, category="read",
))

register(ToolSpec(
    name="list_active_subagents",
    description="List every sub-agent spawned this session with its id, status (running/done/failed), and "
                "label. Use to check what is still running before spawning more or collecting results.",
    parameters={"type": "object", "properties": {}, "required": []},
    impl=list_active_subagents, category="read",
))
