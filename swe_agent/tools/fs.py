"""Filesystem tools: read, write, edit, multi_edit, ls, glob, info, move, delete.

All paths are resolved against ``ctx.cwd``; nothing here mutates process cwd.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..config import IGNORE_DIRS, MAX_FILE_READ_LINES
from .base import ToolContext, ToolSpec, register
from ._util import number_lines, should_ignore, truncate


# --------------------------------------------------------------------------- read

def read_file(ctx: ToolContext, path: str, start_line: Optional[int] = None,
              end_line: Optional[int] = None) -> str:
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    if p.is_dir():
        return f"Error: {path} is a directory (use ls)"
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading {path}: {e}"

    lines = content.split("\n")
    total = len(lines)

    if start_line is not None or end_line is not None:
        s = max(1, start_line or 1)
        e = min(total, end_line or total)
        if s > total:
            return f"(start_line {s} is past end of file; file has {total} lines)"
        chosen = "\n".join(lines[s - 1:e])
        return number_lines(chosen, start=s) if chosen else "(empty range)"

    if not content:
        return "(empty file)"
    if total > MAX_FILE_READ_LINES:
        body = number_lines("\n".join(lines[:MAX_FILE_READ_LINES]), start=1)
        return body + (
            f"\n... ({total - MAX_FILE_READ_LINES} more lines; "
            f"pass start_line/end_line to read further)"
        )
    return number_lines(content, start=1)


def read_multiple_files(ctx: ToolContext, paths: List[str]) -> str:
    out = []
    for raw in paths:
        p = ctx.resolve(raw)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            out.append(f"===== {raw} =====\n{truncate(text, 8000)}")
        except Exception as e:
            out.append(f"===== {raw} =====\nERROR: {e}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- write / edit

def write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = ctx.resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def edit(ctx: ToolContext, path: str, old_string: str, new_string: str,
         replace_all: bool = False) -> str:
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading {path}: {e}"
    count = original.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}"
    if count > 1 and not replace_all:
        return (f"Error: old_string appears {count} times in {path}; "
                f"make it unique or set replace_all=true")
    new_content = (original.replace(old_string, new_string) if replace_all
                   else original.replace(old_string, new_string, 1))
    try:
        p.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return f"Error writing {path}: {e}"
    return f"Edited {path} ({count if replace_all else 1} occurrence(s) replaced)"


def multi_edit(ctx: ToolContext, path: str, edits: List[dict]) -> str:
    """Apply several edits to one file atomically (validate all, then one write)."""
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    if not edits:
        return "Error: no edits provided"
    try:
        working = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading {path}: {e}"

    for i, ed in enumerate(edits, 1):
        old = ed.get("old_string")
        new = ed.get("new_string", "")
        replace_all = ed.get("replace_all", False)
        if old is None:
            return f"Error: edit #{i} is missing old_string (no changes written)"
        count = working.count(old)
        if count == 0:
            snippet = old[:60].replace("\n", "\\n")
            return f"Error: edit #{i}: old_string not found (after prior edits): '{snippet}' (no changes written)"
        if count > 1 and not replace_all:
            return (f"Error: edit #{i}: old_string appears {count} times; "
                    f"make it unique or set replace_all=true (no changes written)")
        working = (working.replace(old, new) if replace_all
                   else working.replace(old, new, 1))

    try:
        p.write_text(working, encoding="utf-8")
    except Exception as e:
        return f"Error writing {path}: {e}"
    return f"Applied {len(edits)} edit(s) to {path} atomically"


# --------------------------------------------------------------------------- listing

def ls(ctx: ToolContext, path: str = ".", recursive: bool = False) -> str:
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: path does not exist: {path}"
    if p.is_file():
        return f"[FILE] {p.name} ({p.stat().st_size} bytes)"
    entries: List[str] = []
    try:
        if recursive:
            for entry in sorted(p.rglob("*")):
                rel = entry.relative_to(p)
                if should_ignore(rel):
                    continue
                if entry.is_dir():
                    entries.append(f"[DIR]  {rel.as_posix()}/")
                else:
                    entries.append(f"[FILE] {rel.as_posix()} ({entry.stat().st_size}b)")
        else:
            for entry in sorted(p.iterdir()):
                if entry.name in IGNORE_DIRS:
                    continue
                if entry.is_dir():
                    entries.append(f"[DIR]  {entry.name}/")
                else:
                    entries.append(f"[FILE] {entry.name} ({entry.stat().st_size}b)")
    except Exception as e:
        return f"Error listing {path}: {e}"
    return "\n".join(entries) if entries else "(empty)"


def glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    base = ctx.resolve(path)
    try:
        matches = sorted(
            m.relative_to(base).as_posix()
            for m in base.glob(pattern)
            if not should_ignore(m.relative_to(base))
        )
    except Exception as e:
        return f"Error in glob: {e}"
    if not matches:
        return f"No files matched: {pattern}"
    if len(matches) > 200:
        return "\n".join(matches[:200]) + f"\n... ({len(matches) - 200} more matches)"
    return "\n".join(matches)


def get_file_info(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: {path} does not exist"
    try:
        st = p.stat()
        return json.dumps({
            "path": str(p),
            "type": "dir" if p.is_dir() else "file",
            "size": st.st_size if p.is_file() else None,
            "modified": st.st_mtime,
            "exists": True,
        }, indent=2)
    except Exception as e:
        return f"Error getting info for {path}: {e}"


# --------------------------------------------------------------------------- move / delete

def delete_file(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: {path} does not exist"
    try:
        if p.is_dir():
            p.rmdir()  # only removes empty dirs (intentional safety)
            return f"Deleted empty directory {path}"
        p.unlink()
        return f"Deleted {path}"
    except OSError as e:
        if p.is_dir():
            return f"Error: directory not empty: {path} ({e}). Remove contents first or use run_command."
        return f"Error deleting {path}: {e}"


def move_file(ctx: ToolContext, source: str, destination: str) -> str:
    src = ctx.resolve(source)
    dst = ctx.resolve(destination)
    if not src.exists():
        return f"Error: source does not exist: {source}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        return f"Moved {source} -> {destination}"
    except Exception as e:
        return f"Error moving: {e}"


# --------------------------------------------------------------------------- registration

register(ToolSpec(
    name="read_file",
    description="Read a file's contents with line numbers. Supports start_line/end_line for large files. Use this before editing.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "description": "1-based first line (optional)"},
        "end_line": {"type": "integer", "description": "1-based last line (optional)"},
    }, "required": ["path"]},
    impl=read_file, category="read", aliases=("view_file", "cat"),
))

register(ToolSpec(
    name="read_multiple_files",
    description="Read several files at once. More efficient than multiple read_file calls for exploration.",
    parameters={"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"}},
    }, "required": ["paths"]},
    impl=read_multiple_files, category="read",
))

register(ToolSpec(
    name="write_file",
    description="Create or completely overwrite a file. Prefer edit/multi_edit/apply_patch for changes to existing files.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    }, "required": ["path", "content"]},
    impl=write_file, mutating=True, category="write",
))

register(ToolSpec(
    name="edit",
    description="Precise find-and-replace in a file. old_string must be unique unless replace_all=true.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean", "default": False},
    }, "required": ["path", "old_string", "new_string"]},
    impl=edit, mutating=True, category="write", aliases=("search_replace",),
))

register(ToolSpec(
    name="multi_edit",
    description="Apply multiple find-and-replace edits to ONE file atomically (all succeed or none are written).",
    parameters={"type": "object", "properties": {
        "path": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "object", "properties": {
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        }, "required": ["old_string", "new_string"]}},
    }, "required": ["path", "edits"]},
    impl=multi_edit, mutating=True, category="write",
))

register(ToolSpec(
    name="ls",
    description="List a directory. Ignores noise dirs (.git, node_modules, venv, ...). Set recursive=true to walk the tree.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "Directory (default '.')"},
        "recursive": {"type": "boolean", "default": False},
    }, "required": []},
    impl=ls, category="read", aliases=("list_dir",),
))

register(ToolSpec(
    name="glob",
    description="Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Ignores noise dirs.",
    parameters={"type": "object", "properties": {
        "pattern": {"type": "string"},
        "path": {"type": "string", "description": "Base directory (default '.')"},
    }, "required": ["pattern"]},
    impl=glob_files, category="read",
))

register(ToolSpec(
    name="get_file_info",
    description="Get metadata about a file or directory (size, type, modified time).",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    impl=get_file_info, category="read",
))

register(ToolSpec(
    name="delete_file",
    description="Delete a file or an empty directory.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    impl=delete_file, mutating=True, category="write",
))

register(ToolSpec(
    name="move_file",
    description="Move or rename a file or directory.",
    parameters={"type": "object", "properties": {
        "source": {"type": "string"},
        "destination": {"type": "string"},
    }, "required": ["source", "destination"]},
    impl=move_file, mutating=True, category="write",
))
