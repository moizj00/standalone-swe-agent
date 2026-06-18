"""Standalone Ollama-powered SWE coding agent.

A local, autonomous software-engineering agent with a full tool suite (file ops,
ripgrep search, cross-platform shell, git, web, sub-agents), approval/plan modes,
persistent sessions, and context compaction — driven by a local Ollama model via
the native /api/chat endpoint.

The package is the implementation; the top-level ``swe_agent.py`` file is a thin
shim so ``python swe_agent.py "task"`` and the ``ollama-agent`` launcher keep working.
"""
from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["__version__"]
