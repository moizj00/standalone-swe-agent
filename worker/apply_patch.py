"""Spec-compatibility entry point for ``apply_patch``.

The canonical implementation lives in :mod:`swe_agent.patcher` — this repo keeps
agent code inside the ``swe_agent`` package. This module re-exports it at the
``worker/apply_patch.py`` path the task spec requires, so either import works:

    from worker.apply_patch import apply_patch          # spec path
    from swe_agent.patcher import apply_patch           # canonical path

Both names refer to the same function object; there is a single source of truth.
"""
from __future__ import annotations

from swe_agent.patcher import Patch, apply_patch

__all__ = ["apply_patch", "Patch"]
