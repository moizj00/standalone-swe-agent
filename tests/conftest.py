"""Shared pytest fixtures and helpers.

Every test here runs WITHOUT a live Ollama server: the agent loop is driven
through ``Agent(mock=...)`` and the transport/parsing functions are exercised
directly. That keeps the suite fast and hermetic (CI-safe).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from swe_agent.agent import Agent
from swe_agent.config import ApprovalMode
from swe_agent.tools.base import ToolContext
from swe_agent.tools.exec import BackgroundRegistry


def scripted(*responses: Tuple[str, List[Tuple[str, dict]]]) -> Callable:
    """Build a mock model from a script of turns.

    Each response is ``(content, [(tool_name, args_dict), ...])``. The returned
    callable matches ``Agent``'s ``mock`` signature -- it yields raw tool calls
    in Ollama-native shape so they flow through ``llm.normalize`` exactly like a
    real response. Once the script is exhausted it returns no tool calls, which
    ends the turn.
    """
    turns = list(responses)
    state = {"i": 0}

    def _mock(messages: List[dict]) -> Tuple[str, List[dict]]:
        i = state["i"]
        state["i"] += 1
        if i >= len(turns):
            return "", []
        content, calls = turns[i]
        raw = [{"function": {"name": n, "arguments": a}} for (n, a) in calls]
        return content, raw

    return _mock


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    """A YOLO context rooted in a temp dir -- tools execute without prompts."""
    return ToolContext(
        cwd=tmp_path,
        approval=ApprovalMode.YOLO,
        approve_cb=lambda name, args, reason: True,
        bg_registry=BackgroundRegistry(),
    )


def make_agent(ctx: ToolContext, mock: Callable, **kw) -> Agent:
    return Agent(model="mock", ctx=ctx, system_prompt="test", stream=False,
                 verbose=False, mock=mock, **kw)
