"""Small shared helpers used by multiple tool modules."""
from __future__ import annotations

from pathlib import Path

from ..config import IGNORE_DIRS


def should_ignore(rel_path: Path) -> bool:
    """True if any path component is an ignored directory (e.g. .git, node_modules)."""
    return any(part in IGNORE_DIRS for part in rel_path.parts)


def is_binary(path: Path, sniff: int = 2048) -> bool:
    """Cheap binary sniff: presence of a NUL byte in the first chunk."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(sniff)
    except Exception:
        return True


def number_lines(text: str, start: int = 1) -> str:
    """Render text with right-aligned line numbers (cat -n style)."""
    lines = text.split("\n")
    last = start + len(lines) - 1
    width = max(len(str(last)), 1)
    return "\n".join(f"{i:>{width}}\t{line}" for i, line in enumerate(lines, start))


def truncate(text: str, limit: int, note: str = "output truncated") -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({note}; {len(text) - limit} more chars)"
