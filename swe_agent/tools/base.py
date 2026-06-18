"""Shared primitives for tools: ToolContext, ToolSpec, and the registry.

This module has NO dependency on the rest of the package so tool modules can
import it freely without circular-import headaches. Tool implementations all
take a ToolContext as their first argument and resolve relative paths against
``ctx.cwd`` -- nothing in this codebase ever calls ``os.chdir`` (that would be
a process-global mutation and would corrupt concurrent subagents).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class ToolContext:
    """Per-invocation execution context threaded into every tool call."""

    cwd: Path
    approval: Any = None          # config.ApprovalMode (Any avoids an import cycle)
    approve_cb: Optional[Callable[[str, dict, str], bool]] = None
    bg_registry: Any = None       # exec.BackgroundRegistry
    # Runtime config carried so subagent tools can spawn child agents with the
    # same transport settings (kept here to avoid threading these through every call).
    model: Optional[str] = None
    base_url: Optional[str] = None
    num_ctx: Optional[int] = None
    temperature: Optional[float] = None

    def resolve(self, path: Optional[str]) -> Path:
        """Resolve a possibly-relative path against the logical working dir."""
        p = Path(path) if path else Path(".")
        if not p.is_absolute():
            p = self.cwd / p
        return p


@dataclass
class ToolSpec:
    """A registered tool: its JSON schema plus its Python implementation."""

    name: str
    description: str
    parameters: dict
    impl: Callable[..., str]
    mutating: bool = False
    category: str = "read"        # read | write | exec | meta
    aliases: Tuple[str, ...] = ()

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# name (and alias) -> ToolSpec. Aliases resolve for dispatch but are NOT
# advertised to the model (keeps the advertised tool count down, which matters
# for small models).
REGISTRY: Dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    REGISTRY[spec.name] = spec
    for alias in spec.aliases:
        REGISTRY[alias] = spec
    return spec


def resolve_spec(name: str) -> Optional[ToolSpec]:
    return REGISTRY.get(name)
