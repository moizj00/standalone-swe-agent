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


def create_directory(ctx: ToolContext, path: str) -> str:
    p = ctx.resolve(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return f"Created directory {path}"
    except Exception as e:
        return f"Error creating directory {path}: {e}"


# --------------------------------------------------------------------------- registration

register(ToolSpec(
    name="read_file",
    description="Read a file's contents with line numbers. Always read a file before editing it, so you can copy an exact old_string. Pass start_line/end_line to read a slice of a large file.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the project root, e.g. 'app/page.tsx'."},
        "start_line": {"type": "integer", "description": "1-based first line to read (optional)"},
        "end_line": {"type": "integer", "description": "1-based last line to read (optional)"},
    }, "required": ["path"]},
    impl=read_file, category="read", aliases=("view_file", "cat"),
))

register(ToolSpec(
    name="read_multiple_files",
    description="Read several files at once. Use when exploring; more efficient than many separate read_file calls. Output is truncated, so prefer read_file when you need a file's exact contents to edit it.",
    parameters={"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"}, "description": "List of file paths relative to the project root."},
    }, "required": ["paths"]},
    impl=read_multiple_files, category="read",
))

register(ToolSpec(
    name="write_file",
    description="Create a new file or completely overwrite an existing one. Destructive: replaces the entire file and cannot be undone. Use only for new files; prefer edit/multi_edit/apply_patch to change existing files.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the project root, e.g. 'app/api/users/route.ts'. Missing parent dirs are created."},
        "content": {"type": "string", "description": "Full contents to write to the file."},
    }, "required": ["path", "content"]},
    impl=write_file, mutating=True, category="write",
))

register(ToolSpec(
    name="edit",
    description="Precise find-and-replace in an existing file; modifies the file in place. Read the file first so old_string matches exactly. old_string must be unique in the file unless replace_all=true.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the project root, e.g. 'app/page.tsx'."},
        "old_string": {"type": "string", "description": "Exact text to find, including indentation and whitespace; copy it verbatim from the file."},
        "new_string": {"type": "string", "description": "Replacement text. Must differ from old_string."},
        "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence instead of requiring old_string to be unique."},
    }, "required": ["path", "old_string", "new_string"]},
    impl=edit, mutating=True, category="write", aliases=("search_replace", "edit_file"),
))

register(ToolSpec(
    name="multi_edit",
    description="Apply several find-and-replace edits to ONE file atomically (all succeed or none are written); modifies the file in place. Use for multiple changes to the same file. Read the file first so each old_string matches exactly. Edits apply in order, so later old_strings must match the text left by earlier ones.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the project root, e.g. 'app/page.tsx'."},
        "edits": {"type": "array", "description": "Edits to apply in order, e.g. [{\"old_string\": \"foo\", \"new_string\": \"bar\"}].", "items": {"type": "object", "properties": {
            "old_string": {"type": "string", "description": "Exact text to find, including whitespace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "default": False, "description": "Replace every occurrence of old_string."},
        }, "required": ["old_string", "new_string"]}},
    }, "required": ["path", "edits"]},
    impl=multi_edit, mutating=True, category="write",
))

register(ToolSpec(
    name="ls",
    description="List the entries in a directory. Use to see what a folder contains; ignores noise dirs (.git, node_modules, venv, ...). Set recursive=true to walk the whole tree, or use glob to match by name pattern.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "description": "Directory to list, relative to the project root (default '.')."},
        "recursive": {"type": "boolean", "default": False, "description": "Walk the directory tree instead of listing one level."},
    }, "required": []},
    impl=ls, category="read", aliases=("list_dir",),
))

register(ToolSpec(
    name="glob",
    description="Find files by name pattern. Use to locate files when you know part of the name or extension; ignores noise dirs. To search file contents instead, use grep.",
    parameters={"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'."},
        "path": {"type": "string", "description": "Base directory to search from, relative to the project root (default '.')."},
    }, "required": ["pattern"]},
    impl=glob_files, category="read",
))

register(ToolSpec(
    name="get_file_info",
    description="Get metadata about a file or directory (size, type, modified time) without reading its contents. Use to check whether a path exists or whether it is a file or directory.",
    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "File or directory path relative to the project root."}}, "required": ["path"]},
    impl=get_file_info, category="read",
))

register(ToolSpec(
    name="delete_file",
    description="Delete a file or an empty directory. Irreversible: the file is removed and cannot be recovered. Non-empty directories are not deleted; clear their contents first.",
    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "File or empty-directory path relative to the project root."}}, "required": ["path"]},
    impl=delete_file, mutating=True, category="write",
))

register(ToolSpec(
    name="move_file",
    description="Move or rename a file or directory. Overwrites the destination if it already exists, so confirm the destination path first.",
    parameters={"type": "object", "properties": {
        "source": {"type": "string", "description": "Existing path to move, relative to the project root."},
        "destination": {"type": "string", "description": "New path, relative to the project root; missing parent dirs are created."},
    }, "required": ["source", "destination"]},
    impl=move_file, mutating=True, category="write",
))

register(ToolSpec(
    name="create_directory",
    description="Create a directory, including any missing parents; writes to the filesystem. No error if it already exists. Not needed before write_file, which creates parent dirs on its own.",
    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "Directory path to create, relative to the project root."}}, "required": ["path"]},
    impl=create_directory, mutating=True, category="write",
))
