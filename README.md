# standalone swe_agent.py

A standalone, autonomous SWE (Software Engineer) coding agent powered by Ollama (recommended: qwen2.5-coder:7b).

This is a self-contained Python-based agent that implements a full ReAct-style tool-calling loop. It provides a comprehensive set of tools modeled after modern coding agents (Grok, Claude, etc.).

It supports parallel sub-agents with **isolated private context** — subagents run independently and only return concise summaries to the main context (no pollution).

## Features

- **Full tool suite** (~23 tools): file system ops (list, read, write, edit, delete, move, glob, info), advanced search (grep with context), rich execution (run_command/bash with background, timeout, description), todo management, git/patch helpers (apply_patch, git_status, git_diff, etc.), dedicated test runner, web research (search, open_page), and powerful sub-agent spawning (20+ simultaneous).
- **Sub-agent isolation**: Each subagent has its own private message history and tool state. Main agent only sees spawn confirmation + clean summary result.
- **One-line launcher**: `ollama-agent "your task here"`
- **Ollama-native**: Uses Ollama's OpenAI-compatible API. Default model qwen2.5-coder:7b.
- **Autonomous**: The LLM decides tool use, spawns subs for parallel work, manages todos, verifies changes.

## Quick Start

1. Ensure Ollama is installed and running:
   ```bash
   ollama serve
   ```

2. Pull the recommended model:
   ```bash
   ollama pull qwen2.5-coder:7b
   ```

3. Run the agent:
   ```bash
   # Using the launcher (recommended)
   ./ollama-agent "Add type hints to all functions in src/"

   # Or directly
   python swe_agent.py "Refactor the config loader"

   # With specific model
   OLLAMA_AGENT_MODEL=qwen2.5:7b ./ollama-agent "your task"

   # Interactive mode
   ./ollama-agent
   ```

4. For dangerous commands (use with caution):
   ```bash
   ./ollama-agent --yolo "delete all __pycache__ and run tests"
   ```

## Core Components

- `swe_agent.py` — The main agent with full tool-calling loop and subagent support.
- `ollama-agent` — Portable bash launcher (resolves paths, ensures server, defaults to coder model).
- `ensure-ollama.sh` — Idempotent helper to start Ollama server if needed.

## Tools Overview

**File System**
- list_dir (recursive support), glob, get_file_info
- view_file, read_multiple_files
- write_file, search_replace, apply_patch
- delete_file, move_file

**Search**
- grep (regex + glob + context_lines)

**Execution**
- run_command, bash (with cwd, description, timeout, background)

**Planning**
- todo_write (structured visible task lists, persisted to .agent_todos.json)

**Git & Patching**
- git_status, git_diff, git_log, git_commit
- apply_patch (unified diffs / git apply)

**Testing**
- run_tests (auto-detects pytest, npm test, cargo, go test, etc.)

**Research**
- web_search, open_page

**Sub-agents (Parallelism)**
- spawn_subagent (task, description; supports 20+ concurrent)
- get_subagent_result (poll for summary)

Sub-agents have **completely private context**. They never leak their full history or intermediate tool calls into the parent. Only a concise summary is returned.

## Subagent Isolation

- Each spawn creates an independent thread with its own `messages` list.
- Sub-agents run with the full toolset.
- When finished (or step limit), they produce a clear summary.
- Parent only receives: "spawned id=xxx" immediately, then the summary via get_subagent_result.
- This prevents context bloat while enabling true parallel work (e.g., explore different modules simultaneously).

Example usage in agent:
```
Spawn several sub-agents for different parts of the codebase, then collect summaries.
```

## Requirements

- Python 3.8+
- requests
- Ollama (with qwen2.5-coder:7b recommended)

Install Python deps:
```bash
pip install -r requirements.txt
```

## Development / Customization

- The agent is fully self-contained.
- To add tools: define in TOOLS list + implement function + register in TOOL_IMPLEMENTATIONS.
- Subagent logic is in `_execute_subagent_task` and `spawn_subagent`.
- The launcher is a thin wrapper; it can be symlinked or installed to PATH.

## License

This is provided as part of personal agent tooling experiments. Use responsibly.

## Acknowledgments

Built iteratively as a local alternative to cloud coding agents, with emphasis on tool richness, subagent parallelism, and strict context isolation.
