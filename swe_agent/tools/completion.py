"""task_complete: the explicit, structured signal that the task is finished.

The agent loop (agent.py) special-cases a call to this tool: it renders the summary,
returns it as the turn's final answer, and stops. This gives small local models a clear,
reliable way to terminate instead of relying on "emit no tool calls".
"""
from __future__ import annotations

from typing import List, Optional

from .base import ToolContext, ToolSpec, register


def task_complete(ctx: ToolContext, final_summary: str,
                  confidence: Optional[str] = None,
                  files_changed: Optional[List[str]] = None) -> str:
    lines = [final_summary.strip()]
    if files_changed:
        lines.append("")
        lines.append("Files changed:")
        lines.extend(f"  - {f}" for f in files_changed)
    if confidence:
        lines.append("")
        lines.append(f"Confidence: {confidence}")
    return "\n".join(lines)


register(ToolSpec(
    name="task_complete",
    description="Signal that the task is FULLY complete and verified. Call this once, only after all work is done and "
                "checked -- it immediately ENDS the run, so any remaining edits, commands, or checks must happen BEFORE "
                "this call, never after. Provide final_summary, and optionally confidence (low/medium/high) and files_changed.",
    parameters={"type": "object", "properties": {
        "final_summary": {"type": "string", "description": "What you did and how the user can verify it, e.g. 'Added null check in parse(); run npm test'."},
        "confidence": {"type": "string", "description": "Your confidence the task is correct and complete: low, medium, or high.", "enum": ["low", "medium", "high"]},
        "files_changed": {"type": "array", "description": "Paths of files you created or modified, e.g. ['src/app.py'].", "items": {"type": "string"}},
    }, "required": ["final_summary"]},
    impl=task_complete, category="meta",
))
