#!/usr/bin/env python3
"""
Ollama SWE Coding Agent

Implements the classic agent loop:
  System Prompt → Tools → Execute → Observe → repeat

The LLM is told it is an expert software engineer with terminal + filesystem access.
It can only act by calling tools. We execute them locally and feed the results back.

Usage examples:
  python swe_agent.py "Add a README to this directory explaining the project"
  python swe_agent.py --model qwen2.5-coder:7b --yolo "Refactor the main function in foo.py"
  python swe_agent.py  (interactive mode)

Model must be pulled: ollama pull qwen2.5:7b   (or qwen2.5-coder:7b)
Ollama must be running on http://localhost:11434
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ========================== CONFIG ==========================

DEFAULT_OLLAMA_BASE = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:7b"
MAX_STEPS = 25
TIMEOUT = 120  # seconds for commands

# For subagent spawning - support 20+ simultaneous
SUBAGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=50)
SUBAGENT_RESULTS: Dict[str, Any] = {}  # subagent_id -> future or result

# ========================== SYSTEM PROMPT ==========================

SYSTEM_PROMPT = """You are an expert software engineer with full access to a terminal and the filesystem in the current working directory.

Your goal is to complete the user's task as efficiently and correctly as possible using the tools provided.

Core principles:
- Explore first. Never guess paths or contents. Use tools to inspect.
- Make small, correct, verifiable changes.
- After writing or editing files, immediately read them back or run relevant commands to verify.
- Use `run_command` / `bash` for building, testing, git operations, etc.
- Prefer precise edits (search_replace or apply_patch) over rewriting entire files.
- Maintain a todo list when the task is non-trivial so the user (and you) can track progress.
- If a command might be destructive, be careful (the user can run with --yolo).
- When the task is complete, give a short final summary of what you changed and how to verify it. Do not call more tools after you have finished.

You have access to these tools (modeled after Grok and Claude coding agents):

**File System:**
- list_dir, glob, get_file_info, read_multiple_files, view_file, write_file, search_replace, delete_file, move_file

**Search:**
- grep (regex search with context)

**Execution:**
- run_command, bash (shell execution with cwd, timeout, background, description)

**Git & Patching:**
- apply_patch (apply unified diffs / git patches)
- run_command with git (status, diff, add, commit, etc.)

**Testing:**
- run_command for tests (pytest, npm test, cargo test, go test, etc.) or use dedicated patterns

**Planning:**
- todo_write (create and manage a structured todo list visible to the user)

**Other:**
- web_search, open_page (for researching docs, APIs, etc.)

**Sub-agents & Parallelism:**
- spawn_subagent (spawn independent sub-agents for sub-tasks. Supports spawning 20+ simultaneously in parallel using thread pool for concurrent work)

**Dedicated Git Helpers:**
- git_status, git_diff, git_log, git_commit (convenience tools for common git ops)

**Dedicated Testing:**
- run_tests (auto-detects project (pytest, npm, cargo, go, etc.) and runs tests)

You MUST use the exact function call format. Think step-by-step. Use the right tool for the job.

When finished, output a normal message summarizing changes and verification steps.
"""

# ========================== TOOL DEFINITIONS ==========================

TOOLS = [
    # === File System ===
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories. Use this to explore the project structure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (default: '.')"},
                    "recursive": {"type": "boolean", "description": "List recursively", "default": False},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "path": {"type": "string", "description": "Base directory (default: '.')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": "Get metadata about a file or directory (size, modified time, type, etc.).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_file",
            "description": "Read the contents of a file. Supports line ranges for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_multiple_files",
            "description": "Read the contents of multiple files at once. More efficient than multiple calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or completely overwrite a file. Prefer search_replace or apply_patch for modifications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_replace",
            "description": "Precise find-and-replace edit. Use for targeted changes. old_string must be unique unless replace_all=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff / git-style patch. Excellent for larger or multi-file changes. Use `git diff` or `diff -u` output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "The unified diff content"},
                    "path": {"type": "string", "description": "Optional specific file or directory (default: apply in cwd)"},
                },
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file or empty directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file or directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["source", "destination"],
            },
        },
    },
    # === Search ===
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with regex. Supports glob filter and context lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory or file (default: '.')"},
                    "glob": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
                    "context_lines": {"type": "integer", "description": "Lines of context before/after match", "default": 0},
                },
                "required": ["pattern"],
            },
        },
    },
    # === Execution (Grok/Claude style) ===
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command. Use for build, test, git, etc. Supports description for logging and background mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "description": {"type": "string", "description": "Short description of what the command does"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds"},
                    "background": {"type": "boolean", "description": "Run in background", "default": False},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Alternative name for run_command. Same behavior. Preferred in some agent styles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "description": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "background": {"type": "boolean", "default": False},
                },
                "required": ["command"],
            },
        },
    },
    # === Planning (Todo) ===
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Create or update a structured todo list for complex tasks. Use to track progress visibly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    # === Patch / Git ===
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a git-style unified diff patch. Great for complex changes from `git diff`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {"type": "string"},
                    "path": {"type": "string", "description": "Target path (optional)"},
                },
                "required": ["patch"],
            },
        },
    },
    # === Research (Grok style) ===
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information (docs, APIs, solutions).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_page",
            "description": "Fetch and read the content of a URL (useful for documentation).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    # === Sub-agents (for parallelism, >20 simultaneous) ===
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Spawn an independent sub-agent to handle a sub-task autonomously in parallel. You can spawn 20+ simultaneously. Sub-agents have full tool access and run their own loop. Use for parallel exploration, implementation, testing, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The full task prompt for the sub-agent"},
                    "description": {"type": "string", "description": "Short 3-5 word label for this sub-task"},
                    "model": {"type": "string", "description": "Model to use, defaults to qwen2.5-coder:7b"},
                    "cwd": {"type": "string", "description": "Working directory (default current)"},
                },
                "required": ["task", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_subagent_result",
            "description": "Get the result/summary from a previously spawned sub-agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subagent_id": {"type": "string"},
                },
                "required": ["subagent_id"],
            },
        },
    },
    # === Dedicated Git Helpers ===
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git status (staged, unstaged, untracked files).",
            "parameters": {
                "type": "object",
                "properties": {"cwd": {"type": "string", "description": "Optional working dir"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff for changes (staged or unstaged).",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "staged": {"type": "boolean", "default": False},
                    "path": {"type": "string", "description": "Optional specific file"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent git commit log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "max_count": {"type": "integer", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all and commit with message. Use after making changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
    # === Dedicated Test Runner ===
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Auto-detect test framework (pytest, unittest, npm test, cargo test, go test, etc.) and run the tests. Reports pass/fail and output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "command": {"type": "string", "description": "Optional explicit test command to override auto-detect"},
                },
                "required": [],
            },
        },
    },
]


# ========================== TOOL IMPLEMENTATIONS ==========================

# ========================== TOOL IMPLEMENTATIONS (rich set like Grok + Claude) ==========================

def list_dir(path: str = ".", recursive: bool = False) -> str:
    try:
        p = Path(path).resolve()
        if not p.exists():
            return f"Error: Path does not exist: {path}"
        if recursive:
            entries = []
            for entry in sorted(p.rglob("*")):
                rel = entry.relative_to(p)
                if entry.is_dir():
                    entries.append(f"[DIR]  {rel}/")
                else:
                    entries.append(f"[FILE] {rel}")
            return "\n".join(entries) if entries else "(empty)"
        else:
            entries = []
            for entry in sorted(p.iterdir()):
                if entry.is_dir():
                    entries.append(f"[DIR]  {entry.name}/")
                else:
                    entries.append(f"[FILE] {entry.name}")
            return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"Error listing {path}: {e}"


def glob(pattern: str, path: str = ".") -> str:
    try:
        base = Path(path).resolve()
        matches = [str(m.relative_to(base)) for m in base.glob(pattern) if m.is_file() or m.is_dir()]
        matches += [str(m.relative_to(base)) for m in base.rglob(pattern) if m.is_file() or m.is_dir() and "**" in pattern]
        matches = sorted(set(matches))
        return "\n".join(matches) if matches else f"No files matched: {pattern}"
    except Exception as e:
        return f"Error in glob: {e}"


def get_file_info(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: {path} does not exist"
        stat = p.stat()
        return json.dumps({
            "path": str(p),
            "type": "dir" if p.is_dir() else "file",
            "size": stat.st_size if p.is_file() else None,
            "modified": stat.st_mtime,
            "exists": True,
        }, indent=2)
    except Exception as e:
        return f"Error getting info for {path}: {e}"


def view_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: File not found: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)

        if start_line is not None or end_line is not None:
            start = (start_line or 1) - 1
            end = end_line or len(lines)
            selected = "".join(lines[start:end])
            return selected or "(empty range)"
        return content
    except Exception as e:
        return f"Error reading {path}: {e}"


def read_multiple_files(paths: List[str]) -> str:
    results = {}
    for p in paths:
        try:
            results[p] = Path(p).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            results[p] = f"ERROR: {e}"
    return json.dumps(results, indent=2)


def write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def search_replace(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Error: File not found: {path}"
        original = p.read_text(encoding="utf-8", errors="replace")

        if replace_all:
            if old_string not in original:
                return f"Error: old_string not found in {path}"
            count = original.count(old_string)
            new_content = original.replace(old_string, new_string)
            p.write_text(new_content, encoding="utf-8")
            return f"Replaced {count} occurrence(s)"
        else:
            if original.count(old_string) > 1:
                return "Error: old_string appears multiple times. Use replace_all=true or make old_string unique."
            if old_string not in original:
                return f"Error: old_string not found in {path}"
            new_content = original.replace(old_string, new_string, 1)
            p.write_text(new_content, encoding="utf-8")
            return f"Successfully edited {path}"
    except Exception as e:
        return f"Error during search_replace: {e}"


def delete_file(path: str) -> str:
    try:
        p = Path(path)
        if p.is_dir():
            p.rmdir()
        else:
            p.unlink()
        return f"Deleted {path}"
    except Exception as e:
        return f"Error deleting {path}: {e}"


def move_file(source: str, destination: str) -> str:
    try:
        src = Path(source)
        dst = Path(destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f"Moved {source} → {destination}"
    except Exception as e:
        return f"Error moving: {e}"


def grep(pattern: str, path: str = ".", glob: Optional[str] = None, context_lines: int = 0) -> str:
    try:
        base = Path(path)
        results = []
        glob_pattern = glob or "**/*"

        for file_path in base.rglob(glob_pattern if glob else "*"):
            if file_path.is_dir():
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                lines = text.splitlines()
                for i, line in enumerate(lines, 1):
                    if re.search(pattern, line):
                        start = max(0, i - 1 - context_lines)
                        end = min(len(lines), i + context_lines)
                        context = "\n".join(f"{j}: {lines[j-1]}" for j in range(start+1, end+1))
                        results.append(f"{file_path}:{i}\n{context}")
            except Exception:
                continue

        if not results:
            return f"No matches for {pattern}"
        return "\n---\n".join(results[:50])
    except Exception as e:
        return f"Error during grep: {e}"


def run_command(command: str, cwd: Optional[str] = None, description: Optional[str] = None,
                timeout: Optional[int] = None, background: bool = False) -> str:
    try:
        workdir = cwd or os.getcwd()
        effective_timeout = timeout or TIMEOUT
        desc = description or command

        if background:
            # Simple background
            full_cmd = f"cd {workdir} && nohup {command} > /tmp/agent_bg.log 2>&1 & echo $!"
            result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=10)
            return f"[BACKGROUND] {desc}\nStarted with pid: {result.stdout.strip()}\nLog: /tmp/agent_bg.log"

        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
        output = f"$ {command}  # {desc}\n"
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output += f"\n[exit_code={result.returncode}]"
        return output.strip()
    except subprocess.TimeoutExpired:
        return f"Command timed out after {effective_timeout}s: {command}"
    except Exception as e:
        return f"Error running command: {e}"


def bash(command: str, **kwargs) -> str:
    """Alias for run_command for compatibility with some agent styles."""
    return run_command(command, **kwargs)


def todo_write(todos: List[Dict]) -> str:
    """Write/update a todo list. Persists to .agent_todos.json in cwd."""
    try:
        todo_file = Path(".agent_todos.json")
        with open(todo_file, "w") as f:
            json.dump(todos, f, indent=2)
        formatted = "\n".join(
            f"[{t['status'].upper():<12}] {t['id']}: {t['content']}" for t in todos
        )
        return f"Todo list updated:\n{formatted}\n(Saved to .agent_todos.json)"
    except Exception as e:
        return f"Error writing todos: {e}"


def apply_patch(patch: str, path: Optional[str] = None) -> str:
    """Apply a unified diff patch using `patch` or `git apply` if available."""
    try:
        workdir = path or "."
        # Try git apply first
        cmd = f"cd {workdir} && git apply -"
        result = subprocess.run(
            cmd, input=patch, shell=True, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return "Patch applied successfully with git apply"
        # Fallback to patch command
        cmd = f"cd {workdir} && patch -p1"
        result = subprocess.run(
            cmd, input=patch, shell=True, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return "Patch applied with patch -p1"
        return f"Patch failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    except Exception as e:
        return f"Error applying patch: {e}"


def web_search(query: str) -> str:
    """Simple web search using DuckDuckGo (no API key needed)."""
    try:
        url = "https://api.duckduckgo.com/"
        resp = requests.get(url, params={"q": query, "format": "json"}, timeout=15)
        data = resp.json()
        results = []
        if data.get("AbstractText"):
            results.append(f"Abstract: {data['AbstractText']}")
        for r in data.get("RelatedTopics", [])[:5]:
            if isinstance(r, dict) and "Text" in r:
                results.append(f"- {r['Text']}")
        return "\n".join(results) or f"No good results for: {query}. Try a more specific query."
    except Exception as e:
        return f"Web search error: {e}. You can use run_command with curl if needed."


def open_page(url: str) -> str:
    """Fetch page content (text only, truncated)."""
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "OllamaSWEAgent/1.0"})
        text = resp.text[:8000]
        return f"Content from {url}:\n{text}"
    except Exception as e:
        return f"Error fetching {url}: {e}"


def _execute_subagent_task(task: str, description: str, model: str, cwd: str) -> str:
    """Internal: run a full sub-agent loop for a task (limited steps for safety)."""
    original_cwd = os.getcwd()
    if cwd and os.path.exists(cwd):
        os.chdir(cwd)
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"[SUB-AGENT] {description}\n\n{task}\n\nWork autonomously using your tools. When done, give a clear summary."}
        ]
        for _ in range(15):  # sub-agents get fewer steps to stay focused
            try:
                resp = chat_with_ollama(messages, model, DEFAULT_OLLAMA_BASE)
            except Exception as e:
                return f"Sub-agent LLM error: {e}"
            msg = resp["choices"][0]["message"]
            messages.append(msg)
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return f"Sub-agent completed: {content or 'No final text.'}"
            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                args = json.loads(fn["arguments"] or "{}")
                if name in TOOL_IMPLEMENTATIONS:
                    try:
                        obs = str(TOOL_IMPLEMENTATIONS[name](**args))[:3000]
                    except Exception as ex:
                        obs = f"Tool {name} error: {ex}"
                else:
                    obs = f"Unknown tool: {name}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": obs
                })
        return "Sub-agent reached step limit. Last content: " + (messages[-1].get("content") or "")
    finally:
        os.chdir(original_cwd)


def spawn_subagent(task: str, description: str, model: Optional[str] = None, cwd: Optional[str] = None) -> str:
    """Spawn sub-agent in parallel thread. Supports 20+ concurrent via executor."""
    sub_id = str(uuid.uuid4())[:8]
    use_model = model or "qwen2.5-coder:7b"
    use_cwd = cwd or "."
    future = SUBAGENT_EXECUTOR.submit(_execute_subagent_task, task, description, use_model, use_cwd)
    SUBAGENT_RESULTS[sub_id] = future
    return f"✅ Spawned sub-agent {sub_id} ('{description}'). It is running in parallel. Call get_subagent_result with subagent_id='{sub_id}' to get result."


def get_subagent_result(subagent_id: str) -> str:
    if subagent_id not in SUBAGENT_RESULTS:
        return f"❌ No sub-agent with ID {subagent_id}. Available: {list(SUBAGENT_RESULTS.keys())}"
    fut = SUBAGENT_RESULTS[subagent_id]
    if fut.done():
        try:
            res = fut.result(timeout=5)
            return f"📋 Sub-agent {subagent_id} result:\n{res}"
        except Exception as e:
            return f"❌ Sub-agent {subagent_id} failed: {e}"
    return f"⏳ Sub-agent {subagent_id} is still running..."


def git_status(cwd: Optional[str] = None) -> str:
    return run_command("git status --branch --porcelain", cwd=cwd, description="git status")


def git_diff(cwd: Optional[str] = None, staged: bool = False, path: Optional[str] = None) -> str:
    cmd = "git diff"
    if staged:
        cmd += " --staged"
    if path:
        cmd += f" -- {path}"
    return run_command(cmd, cwd=cwd, description="git diff")


def git_log(cwd: Optional[str] = None, max_count: int = 10) -> str:
    return run_command(f"git log --oneline -n {max_count}", cwd=cwd, description="git log")


def git_commit(cwd: Optional[str] = None, message: str = "chore: automated commit by coding agent") -> str:
    run_command("git add -A", cwd=cwd, description="stage all changes")
    return run_command(f'git commit -m "{message}"', cwd=cwd, description="commit")


def run_tests(cwd: Optional[str] = None, command: Optional[str] = None) -> str:
    workdir = cwd or os.getcwd()
    if command:
        return run_command(command, cwd=workdir, description="run tests (override)")
    # auto-detect
    if os.path.exists(os.path.join(workdir, "package.json")):
        return run_command("npm test", cwd=workdir, description="npm test")
    if os.path.exists(os.path.join(workdir, "Cargo.toml")):
        return run_command("cargo test", cwd=workdir, description="cargo test")
    if os.path.exists(os.path.join(workdir, "go.mod")):
        return run_command("go test ./...", cwd=workdir, description="go test")
    if (os.path.exists(os.path.join(workdir, "pyproject.toml")) or
        os.path.exists(os.path.join(workdir, "pytest.ini")) or
        os.path.exists(os.path.join(workdir, "setup.py"))):
        return run_command("python -m pytest -q --tb=line", cwd=workdir, description="pytest")
    return run_command("python -m pytest -q || python -m unittest discover -v", cwd=workdir, description="fallback tests")


# Register all implementations
TOOL_IMPLEMENTATIONS = {
    "list_dir": list_dir,
    "glob": glob,
    "get_file_info": get_file_info,
    "view_file": view_file,
    "read_multiple_files": read_multiple_files,
    "write_file": write_file,
    "search_replace": search_replace,
    "delete_file": delete_file,
    "move_file": move_file,
    "grep": grep,
    "run_command": run_command,
    "bash": bash,
    "todo_write": todo_write,
    "apply_patch": apply_patch,
    "web_search": web_search,
    "open_page": open_page,
    "spawn_subagent": spawn_subagent,
    "get_subagent_result": get_subagent_result,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_commit": git_commit,
    "run_tests": run_tests,
}


# ========================== OLLAMA CALL ==========================

def chat_with_ollama(messages: List[Dict], model: str, base_url: str) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
        "stream": False,
    }
    resp = requests.post(url, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()


# ========================== MAIN AGENT LOOP ==========================

def run_agent(task: str, model: str, base_url: str, yolo: bool = False, cwd: Optional[str] = None):
    if cwd:
        os.chdir(cwd)
        print(f"Working directory: {os.getcwd()}")

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    print(f"\n=== Starting SWE Agent ({model}) ===\nTask: {task}\n")

    for step in range(1, MAX_STEPS + 1):
        print(f"\n--- Step {step} ---")

        try:
            response = chat_with_ollama(messages, model, base_url)
        except Exception as e:
            print(f"Error calling Ollama: {e}")
            break

        choice = response["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if content:
            print(f"Assistant: {content.strip()}")

        if not tool_calls:
            print("\n=== Task complete (no more tool calls) ===\n")
            break

        messages.append(message)  # add assistant message with tool calls

        for tool_call in tool_calls:
            fn = tool_call["function"]
            name = fn["name"]
            args = json.loads(fn["arguments"] or "{}")
            tool_call_id = tool_call["id"]

            print(f"\n▶ Tool: {name}({args})")

            # Safety for shell commands
            if name in ("run_command", "bash") and not yolo:
                cmd = args.get("command", "")
                dangerous = ["rm -rf", "sudo ", ":(){", "mkfs", "shutdown", "reboot", "> /dev/", "git push --force"]
                if any(d in cmd for d in dangerous):
                    print("⚠️  Potentially dangerous command blocked. Use --yolo to allow.")
                    observation = "Command was blocked for safety. Ask the user for confirmation."
                else:
                    observation = TOOL_IMPLEMENTATIONS[name](**args)
            else:
                observation = TOOL_IMPLEMENTATIONS[name](**args)

            # Truncate very long observations
            if len(observation) > 8000:
                observation = observation[:8000] + "\n... (output truncated)"

            print(f"◀ Observation:\n{textwrap.indent(observation[:2000], '   ')}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": observation,
            })

    else:
        print(f"\nReached maximum steps ({MAX_STEPS}). Stopping.")

    print("\nFinal response:")
    # Print last assistant content if any
    for m in reversed(messages):
        if m["role"] == "assistant" and m.get("content"):
            print(m["content"])
            break


# ========================== CLI ==========================

def main():
    parser = argparse.ArgumentParser(description="Ollama-powered Software Engineer Coding Agent")
    parser.add_argument("task", nargs="?", default=None, help="Task description. If omitted, enters interactive mode.")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Ollama model to use")
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA_BASE, help="Ollama OpenAI-compatible endpoint")
    parser.add_argument("--yolo", action="store_true", help="Allow potentially dangerous commands without confirmation")
    parser.add_argument("--cwd", help="Set working directory for the agent")
    args = parser.parse_args()

    if args.task:
        run_agent(args.task, args.model, args.base_url, args.yolo, args.cwd)
    else:
        print("Interactive mode. Type your task (or 'exit').")
        while True:
            try:
                task = input("\n> Task: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break
            if task.lower() in {"exit", "quit", "q"}:
                break
            if task:
                run_agent(task, args.model, args.base_url, args.yolo, args.cwd)


if __name__ == "__main__":
    main()
