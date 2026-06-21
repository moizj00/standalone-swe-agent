"""task_complete: the explicit, structured signal that the task is finished.

The agent loop (agent.py) special-cases a call to this tool: it renders the summary,
returns it as the turn's final answer, and stops. This gives small local models a clear,
reliable way to terminate instead of relying on "emit no tool calls".

The structured report (§16 of the spec) is written to .agent/report.json for
machine consumption (CI, headless mode, downstream tools).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .base import ToolContext, ToolSpec, register

REPORT_DIR = ".agent"
REPORT_FILENAME = "report.json"


def task_complete(ctx: ToolContext, final_summary: str,
                  confidence: Optional[str] = None,
                  files_changed: Optional[List[str]] = None,
                  status: Optional[str] = None,
                  plan: Optional[List[str]] = None,
                  tests: Optional[Dict] = None,
                  next_actions: Optional[List[str]] = None,
                  assumptions: Optional[List[str]] = None) -> str:
    """Build and persist a structured completion report."""
    report = {
        "status": status or "success",
        "summary": final_summary.strip(),
        "confidence": confidence or "medium",
        "plan": plan or [],
        "changes": [
            {"path": f, "action": "modify", "why": ""}
            for f in (files_changed or [])
        ],
        "tests": tests or {"ran": False, "passed": False, "failed": 0, "output": ""},
        "next_actions": next_actions or [],
        "assumptions": assumptions or [],
    }

    # Persist the structured report
    report_path = ctx.cwd / REPORT_DIR / REPORT_FILENAME
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass  # report persistence is best-effort

    # Human-readable output (returned to agent loop as the final answer)
    lines = [final_summary.strip()]
    if files_changed:
        lines.append("")
        lines.append("Files changed:")
        lines.extend(f"  - {f}" for f in files_changed)
    if confidence:
        lines.append("")
        lines.append(f"Confidence: {confidence}")
    if next_actions:
        lines.append("")
        lines.append("Next actions:")
        lines.extend(f"  - {a}" for a in next_actions)
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
        "files_changed": {"type": "array", "items": {"type": "string"},
                          "description": "List of files created or modified during the task"},
        "status": {"type": "string", "enum": ["success", "needs_human", "failed"],
                   "description": "Overall task outcome"},
        "plan": {"type": "array", "items": {"type": "string"},
                 "description": "Steps that were executed"},
        "tests": {"type": "object", "properties": {
            "ran": {"type": "boolean"}, "passed": {"type": "boolean"},
            "failed": {"type": "integer"}, "output": {"type": "string"},
        }, "description": "Test execution results"},
        "next_actions": {"type": "array", "items": {"type": "string"},
                         "description": "Suggested follow-up actions for the user"},
        "assumptions": {"type": "array", "items": {"type": "string"},
                        "description": "Assumptions made during the task"},
    }, "required": ["final_summary"]},
    impl=task_complete, category="meta",
))
