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
                      DEFAULT_TEMPERATURE, SUBAGENT_MAX_WORKERS)
from .base import ToolContext, ToolSpec, register

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=SUBAGENT_MAX_WORKERS)
_RESULTS: dict = {}


def spawn_subagent(ctx: ToolContext, task: str, description: str,
                   model: Optional[str] = None, cwd: Optional[str] = None) -> str:
    from ..agent import run_subagent  # lazy import breaks the tools<->agent cycle

    sub_id = uuid.uuid4().hex[:8]
    use_cwd = str(ctx.resolve(cwd)) if cwd else str(ctx.cwd)
    use_model = model or ctx.model or DEFAULT_MODEL
    base_url = ctx.base_url or DEFAULT_OLLAMA_BASE
    num_ctx = ctx.num_ctx or DEFAULT_NUM_CTX
    temperature = ctx.temperature if ctx.temperature is not None else DEFAULT_TEMPERATURE

    fut = _EXECUTOR.submit(run_subagent, task, description, use_model, use_cwd,
                           base_url, num_ctx, temperature, ctx.approval)
    _RESULTS[sub_id] = fut
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
    description="Spawn an independent sub-agent to handle a sub-task in parallel (its own private "
                "context; only a summary is returned). Use for parallel exploration or implementation.",
    parameters={"type": "object", "properties": {
        "task": {"type": "string", "description": "The full task prompt for the sub-agent"},
        "description": {"type": "string", "description": "Short 3-5 word label"},
        "model": {"type": "string", "description": "Model override (default: same as parent)"},
        "cwd": {"type": "string", "description": "Working directory (default: parent cwd)"},
    }, "required": ["task", "description"]},
    impl=spawn_subagent, mutating=True, category="exec",
))

register(ToolSpec(
    name="get_subagent_result",
    description="Collect the summary from a sub-agent previously started with spawn_subagent.",
    parameters={"type": "object", "properties": {"subagent_id": {"type": "string"}},
                "required": ["subagent_id"]},
    impl=get_subagent_result, category="read",
))
