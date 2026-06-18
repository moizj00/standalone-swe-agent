"""Project-analysis tools: get_project_overview and get_directory_tree.

Both are read-only and skip the noise dirs in config.IGNORE_DIRS (.git, node_modules,
venv, __pycache__, ...). Use them to orient on an unfamiliar codebase before editing.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from ..config import IGNORE_DIRS
from .base import ToolContext, ToolSpec, register
from ._util import should_ignore

_MANIFESTS = {
    "package.json": "Node/JS", "tsconfig.json": "TypeScript", "pyproject.toml": "Python",
    "setup.py": "Python", "requirements.txt": "Python", "Cargo.toml": "Rust",
    "go.mod": "Go", "pom.xml": "Java/Maven", "build.gradle": "Java/Gradle",
    "Gemfile": "Ruby", "composer.json": "PHP",
}
_READMES = ("README.md", "README.rst", "README.txt", "README")


def get_project_overview(ctx: ToolContext, path: str = ".") -> str:
    base = ctx.resolve(path)
    if not base.exists():
        return f"Error: path does not exist: {path}"
    if base.is_file():
        return f"Error: {path} is a file, not a project directory"

    lines = [f"Project overview: {base}"]

    found = [f"{m} ({lang})" for m, lang in _MANIFESTS.items() if (base / m).exists()]
    lines.append("Manifests: " + (", ".join(found) if found else "none detected"))

    counts: Counter = Counter()
    total = 0
    for f in base.rglob("*"):
        try:
            rel = f.relative_to(base)
        except ValueError:
            continue
        if f.is_dir() or should_ignore(rel):
            continue
        total += 1
        counts[f.suffix or "(no ext)"] += 1
    lines.append(f"Files (excluding ignored dirs): {total}")
    by_ext = ", ".join(f"{ext}:{n}" for ext, n in counts.most_common(12))
    if by_ext:
        lines.append("By extension: " + by_ext)

    try:
        entries = sorted(
            e.name + ("/" if e.is_dir() else "")
            for e in base.iterdir() if e.name not in IGNORE_DIRS
        )
        lines.append("Top level: " + ", ".join(entries[:40]))
    except Exception:
        pass

    for readme in _READMES:
        rp = base / readme
        if rp.is_file():
            try:
                head = "\n".join(rp.read_text(encoding="utf-8", errors="replace").splitlines()[:15])
                lines.append(f"\n--- {readme} (first 15 lines) ---\n{head}")
            except Exception:
                pass
            break

    return "\n".join(lines)


def get_directory_tree(ctx: ToolContext, path: str = ".", max_depth: int = 3) -> str:
    base = ctx.resolve(path)
    if not base.exists():
        return f"Error: path does not exist: {path}"
    if base.is_file():
        return base.name

    out = [base.name + "/"]

    def walk(d, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(
                (c for c in d.iterdir() if c.name not in IGNORE_DIRS),
                key=lambda c: (c.is_file(), c.name.lower()),
            )
        except Exception:
            return
        for i, c in enumerate(children):
            last = i == len(children) - 1
            out.append(prefix + ("`-- " if last else "|-- ") + c.name + ("/" if c.is_dir() else ""))
            if c.is_dir():
                walk(c, prefix + ("    " if last else "|   "), depth + 1)

    walk(base, "", 1)
    if len(out) > 400:
        return "\n".join(out[:400]) + f"\n... ({len(out) - 400} more entries; narrow path or lower max_depth)"
    return "\n".join(out)


register(ToolSpec(
    name="get_project_overview",
    description="High-level summary of a project: detected manifests/languages, file counts by extension, "
                "top-level entries, and the start of any README. Use first on an unfamiliar codebase.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "Project root (default '.')"},
    }, "required": []},
    impl=get_project_overview, category="read",
))

register(ToolSpec(
    name="get_directory_tree",
    description="ASCII tree of the project structure (skips noise dirs), bounded by max_depth.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "Base directory (default '.')"},
        "max_depth": {"type": "integer", "description": "Max depth to descend (default 3)"},
    }, "required": []},
    impl=get_directory_tree, category="read",
))
