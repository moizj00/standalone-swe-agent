"""Session persistence, environment context, and project-config loading."""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import SESSION_DIR

PROJECT_CONFIG_NAMES = ("AGENTS.md", "CLAUDE.md", ".agent.md")
MAX_PROJECT_CONFIG_CHARS = 8000


def estimate_tokens(messages: List[dict]) -> int:
    """Rough token estimate (~4 chars/token) over all message content + tool calls."""
    total = 0
    for m in messages:
        total += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            total += len(json.dumps(tc.get("function", tc), default=str))
    return total // 4


def build_env_context(cwd: Path) -> str:
    """A compact environment block injected into the system prompt."""
    lines = [
        f"OS: {platform.platform()}",
        f"Python: {platform.python_version()}",
        f"Working directory: {cwd}",
        f"Date: {datetime.now().strftime('%Y-%m-%d')}",
    ]
    try:
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(cwd),
                                capture_output=True, text=True, timeout=10)
        if branch.returncode == 0 and branch.stdout.strip():
            lines.append(f"Git branch: {branch.stdout.strip()}")
            status = subprocess.run(["git", "status", "--porcelain"], cwd=str(cwd),
                                    capture_output=True, text=True, timeout=10)
            changed = [ln for ln in status.stdout.splitlines() if ln.strip()]
            lines.append(f"Git: {'clean' if not changed else f'{len(changed)} changed file(s)'}")
    except Exception:
        pass
    return "\n".join(lines)


def load_project_instructions(cwd: Path) -> tuple[str, Optional[Path]]:
    """Search from cwd upward for AGENTS.md / CLAUDE.md; return (text, path)."""
    here = cwd.resolve()
    for d in [here, *here.parents]:
        for name in PROJECT_CONFIG_NAMES:
            candidate = d / name
            if candidate.is_file():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if len(text) > MAX_PROJECT_CONFIG_CHARS:
                    text = text[:MAX_PROJECT_CONFIG_CHARS] + "\n... (project config truncated)"
                return text, candidate
    return "", None


class Session:
    """One conversation persisted as JSONL: a _meta line followed by message lines."""

    def __init__(self, sid: str, path: Path):
        self.sid = sid
        self.path = path

    @staticmethod
    def _new_id() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    @classmethod
    def create(cls) -> "Session":
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        sid = cls._new_id()
        path = SESSION_DIR / f"{sid}.jsonl"
        # Avoid collisions if two sessions start in the same second.
        n = 1
        while path.exists():
            sid = f"{cls._new_id()}-{n}"
            path = SESSION_DIR / f"{sid}.jsonl"
            n += 1
        return cls(sid, path)

    @classmethod
    def load(cls, sid: str) -> Optional["Session"]:
        path = SESSION_DIR / f"{sid}.jsonl"
        return cls(sid, path) if path.exists() else None

    @classmethod
    def latest(cls) -> Optional["Session"]:
        if not SESSION_DIR.exists():
            return None
        files = sorted(SESSION_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return None
        return cls(files[0].stem, files[0])

    @classmethod
    def list_all(cls) -> List[dict]:
        if not SESSION_DIR.exists():
            return []
        out = []
        for p in sorted(SESSION_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            out.append({"id": p.stem, "mtime": p.stat().st_mtime, "size": p.stat().st_size})
        return out

    def save(self, messages: List[dict], meta: Optional[dict] = None) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"_meta": meta or {}, "id": self.sid}) + "\n")
            for m in messages:
                fh.write(json.dumps(m, default=str) + "\n")
        tmp.replace(self.path)

    def read_messages(self) -> List[dict]:
        messages = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if "_meta" in obj and "role" not in obj:
                        continue
                    messages.append(obj)
        except Exception:
            pass
        return messages
