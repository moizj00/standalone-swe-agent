"""Configuration, constants, and environment parsing for the SWE agent.

Everything tunable lives here so the rest of the package imports a single source
of truth. Environment variables override the built-in defaults.
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

# ----- Provider selection ---------------------------------------------------
# ollama | minimax | kimi | nemotron | openai
DEFAULT_PROVIDER = os.environ.get("SWE_AGENT_PROVIDER", "nemotron").lower()

# ----- Ollama transport -----------------------------------------------------
# NOTE: this is the NATIVE Ollama endpoint base (no trailing /v1). We use
# /api/chat because the OpenAI-compatible /v1 endpoint cannot set num_ctx and
# has buggy streaming-with-tools.
DEFAULT_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_AGENT_MODEL", "qwen2.5-coder:7b")
MODEL_PREFERENCES = ["qwen2.5-coder:7b"]
DEFAULT_MODEL = DEFAULT_OLLAMA_MODEL  # backward-compatible alias
# 32k is ideal on GPU boxes; 8k is safer on ~8GB WSL/CPU-only hosts.
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
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

# ----- Loop guard -----------------------------------------------------------
LOOP_GUARD_ENABLED = os.environ.get("LOOP_GUARD_ENABLED", "true").lower() in ("1", "true", "yes")
LOOP_EXACT_REPEAT_THRESHOLD = int(os.environ.get("LOOP_EXACT_REPEAT_THRESHOLD", "3"))
LOOP_EXACT_REPEAT_WINDOW = int(os.environ.get("LOOP_EXACT_REPEAT_WINDOW", "8"))
LOOP_READ_THRASH_MIN = int(os.environ.get("LOOP_READ_THRASH_MIN", "5"))
LOOP_READ_THRASH_WINDOW = int(os.environ.get("LOOP_READ_THRASH_WINDOW", "10"))
LOOP_NO_PROGRESS_STEPS = int(os.environ.get("LOOP_NO_PROGRESS_STEPS", "12"))
LOOP_OSCILLATION_CYCLES = int(os.environ.get("LOOP_OSCILLATION_CYCLES", "2"))
LOOP_EDIT_RETRY_THRESHOLD = int(os.environ.get("LOOP_EDIT_RETRY_THRESHOLD", "2"))
LOOP_SUBAGENT_POLL_THRESHOLD = int(os.environ.get("LOOP_SUBAGENT_POLL_THRESHOLD", "4"))
LOOP_MAX_ESCALATIONS_PER_SESSION = int(os.environ.get("LOOP_MAX_ESCALATIONS_PER_SESSION", "3"))

# Optional cloud escalation (planning-only unstick turn at level 4)
ESCALATE_MODEL = os.environ.get("SWE_AGENT_ESCALATE_MODEL", "")
ESCALATE_BASE_URL = os.environ.get("SWE_AGENT_ESCALATE_BASE_URL", "https://api.openai.com/v1")
ESCALATE_API_KEY = os.environ.get("SWE_AGENT_ESCALATE_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

# ----- Quality gate ---------------------------------------------------------
QUALITY_GATE_ENABLED = os.environ.get("QUALITY_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
QUALITY_VERIFY_WINDOW = int(os.environ.get("QUALITY_VERIFY_WINDOW", "20"))
QUALITY_MIN_SUMMARY_CHARS = int(os.environ.get("QUALITY_MIN_SUMMARY_CHARS", "40"))

# ----- Intent gate ----------------------------------------------------------
INTENT_GATE_ENABLED = os.environ.get("INTENT_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
INTENT_SCOPE_READ_WINDOW = int(os.environ.get("INTENT_SCOPE_READ_WINDOW", "10"))
INTENT_SCOPE_BLOCK_AFTER = int(os.environ.get("INTENT_SCOPE_BLOCK_AFTER", "2"))

# ----- Audit log ------------------------------------------------------------
AUDIT_ENABLED = os.environ.get("SWE_AGENT_AUDIT", "true").lower() in ("1", "true", "yes")

# ----- Secret redaction -----------------------------------------------------
REDACT_ENABLED = os.environ.get("SWE_AGENT_REDACT", "true").lower() in ("1", "true", "yes")
REDACT_ENTROPY = os.environ.get("SWE_AGENT_REDACT_ENTROPY", "true").lower() in ("1", "true", "yes")

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
