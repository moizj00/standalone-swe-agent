"""Configuration, constants, and environment parsing for the SWE agent.

Everything tunable lives here so the rest of the package imports a single source
of truth. Environment variables override the built-in defaults.
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

# ----- Ollama transport -----------------------------------------------------
# NOTE: this is the NATIVE Ollama endpoint base (no trailing /v1). We use
# /api/chat because the OpenAI-compatible /v1 endpoint cannot set num_ctx and
# has buggy streaming-with-tools.
DEFAULT_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("OLLAMA_AGENT_MODEL", "qwen2.5-coder:7b")
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
DEFAULT_TEMPERATURE = float(os.environ.get("OLLAMA_AGENT_TEMPERATURE", "0.2"))
DEFAULT_TOP_P = float(os.environ.get("OLLAMA_AGENT_TOP_P", "0.9"))
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# ----- Agent loop -----------------------------------------------------------
MAX_STEPS = int(os.environ.get("SWE_AGENT_MAX_STEPS", "50"))
SUBAGENT_MAX_STEPS = int(os.environ.get("SWE_AGENT_SUBAGENT_MAX_STEPS", "20"))
SUBAGENT_MAX_WORKERS = int(os.environ.get("SWE_AGENT_SUBAGENT_WORKERS", "50"))

# ----- Timeouts / retries ---------------------------------------------------
TOOL_TIMEOUT = int(os.environ.get("SWE_AGENT_TOOL_TIMEOUT", "120"))  # shell command default (s)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = int(os.environ.get("SWE_AGENT_READ_TIMEOUT", "600"))  # cold model load can be slow
MAX_RETRIES = 3
BACKOFF_BASE = 1.0

# ----- Output limits --------------------------------------------------------
MAX_OBSERVATION_CHARS = 12000   # truncate tool output fed back to the model
MAX_FILE_READ_LINES = 2000      # default cap when reading a file without a range
WEB_FETCH_MAX_CHARS = 10000

# ----- Context compaction ---------------------------------------------------
COMPACT_THRESHOLD = 0.75        # compact when estimated tokens exceed this fraction of num_ctx
COMPACT_KEEP_RECENT = 6         # most-recent messages always preserved verbatim

# ----- Storage --------------------------------------------------------------
SESSION_DIR = Path(
    os.environ.get("SWE_AGENT_HOME", str(Path.home() / ".swe_agent"))
) / "sessions"

# Directories ignored by ls (recursive), glob, and the pure-python grep fallback.
IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    "build", "dist", ".eggs", ".idea", ".vscode", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "target", ".next", ".turbo", ".tox",
}


class ApprovalMode(str, Enum):
    """How aggressively the agent is allowed to act without asking."""

    READ_ONLY = "read-only"      # plan mode: no mutations at all
    DEFAULT = "default"          # prompt before mutations / shell commands
    AUTO_ACCEPT = "auto-accept"  # auto-accept file edits, still prompt for shell
    YOLO = "yolo"                # allow everything, no prompts

    @classmethod
    def from_flags(cls, plan: bool = False, auto: bool = False, yolo: bool = False) -> "ApprovalMode":
        if plan:
            return cls.READ_ONLY
        if yolo:
            return cls.YOLO
        if auto:
            return cls.AUTO_ACCEPT
        return cls.DEFAULT
