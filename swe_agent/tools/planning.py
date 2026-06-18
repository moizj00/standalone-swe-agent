"""todo_write: a visible, structured task list for multi-step work."""
from __future__ import annotations

import json
from typing import Dict, List

from .base import ToolContext, ToolSpec, register

_SYM = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}


def todo_write(ctx: ToolContext, todos: List[Dict]) -> str:
    try:
        (ctx.cwd / ".agent_todos.json").write_text(json.dumps(todos, indent=2), encoding="utf-8")
    except Exception:
        pass  # persistence is best-effort; the formatted list is the real output
    lines = []
    for t in todos:
        status = t.get("status", "pending")
        lines.append(f"{_SYM.get(status, '[ ]')} {t.get('content', '')}")
    return "Todo list updated:\n" + "\n".join(lines) if lines else "Todo list cleared."


def todo_read(ctx: ToolContext) -> str:
    p = ctx.cwd / ".agent_todos.json"
    if not p.exists():
        return "No todo list found (.agent_todos.json does not exist yet). Use todo_write to create one."
    try:
        todos = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Could not read todo list: {e}"
    if not todos:
        return "Todo list is empty."
    lines = [f"{_SYM.get(t.get('status', 'pending'), '[ ]')} {t.get('content', '')}" for t in todos]
    return "Current todo list:\n" + "\n".join(lines)


register(ToolSpec(
    name="todo_write",
    description="Create or update a visible, structured todo list to plan and track multi-step tasks.",
    parameters={"type": "object", "properties": {
        "todos": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
        }, "required": ["content", "status"]}},
    }, "required": ["todos"]},
    impl=todo_write, category="meta",
))

register(ToolSpec(
    name="todo_read",
    description="Read the current visible todo list (from .agent_todos.json).",
    parameters={"type": "object", "properties": {}, "required": []},
    impl=todo_read, category="read",
))
