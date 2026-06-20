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
    description="Signal that the task is fully complete and verified. final_summary must state what changed "
                "AND how to verify (concrete command or run_tests/run_linter). If code was modified, run "
                "verification before calling this. Calling this ENDS the run.",
    parameters={"type": "object", "properties": {
        "final_summary": {
            "type": "string",
            "description": "What changed + how to verify (e.g. 'Added type hint to auth.ts; verify with: npx tsc --noEmit')",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "files_changed": {"type": "array", "items": {"type": "string"}},
    }, "required": ["final_summary"]},
    impl=task_complete, category="meta",
))
