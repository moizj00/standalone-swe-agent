# standalone swe_agent.py

A standalone, autonomous SWE (Software Engineer) coding agent powered by Ollama (recommended: hhao/qwen2.5-coder-tools:7b, a tool-calling-tuned coder model).

This is a self-contained Python-based agent that implements a full ReAct-style tool-calling loop. It provides a comprehensive set of tools modeled after modern coding agents (Grok, Claude, etc.).

It supports parallel sub-agents with **isolated private context** — subagents run independently and only return concise summaries to the main context (no pollution).

## Features

- **Full tool suite** (35 advertised tools): file system ops (read, write, edit, multi_edit, ls, glob, info, move, delete, mkdir), advanced search (grep with context), rich execution (run_command/bash with background, timeout, description), todo management, git/patch helpers (apply_patch, git_status, git_diff, git_log, git_show, git_commit), dedicated test runner, linter/type-checker, project-overview helpers, web research (web_search, web_fetch), and powerful sub-agent spawning (20+ simultaneous).
- **Sub-agent isolation**: Each subagent has its own private message history and tool state. Main agent only sees spawn confirmation + clean summary result.
- **One-line launcher**: `ollama-agent "your task here"`
- **Ollama-native**: Uses Ollama's native `/api/chat` API (sets `num_ctx`, streams tool calls). Default model hhao/qwen2.5-coder-tools:7b.
- **Autonomous**: The LLM decides tool use, spawns subs for parallel work, manages todos, verifies changes.

## Quick Start

1. Ensure Ollama is installed and running:
   ```bash
   ollama serve
   ```

2. Pull a tool-capable model (the agent defaults to `qwen2.5:7b`):
   ```bash
   ollama pull hhao/qwen2.5-coder-tools:7b
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

5. Hybrid (cloud-first, local warmup in background):
   ```bash
   # Run the cloud-backed agent immediately while the local model warms up in
   # the background, ready for a later --local run.
   ./hybrid-agent "refactor the config loader"

   # Forward any cloud-agent options (provider, dry-run, etc.) untouched.
   ./hybrid-agent --provider openai "add type hints"

   # Force the local-only Ollama path (strips --local, defers to ollama-agent).
   ./hybrid-agent --local "explore project"
   ```
   Set `SWE_AGENT_PROVIDER` to pick the cloud provider and `OLLAMA_AGENT_MODEL`
   to choose the local model that gets warmed. Local warmup is best-effort and
   never blocks or fails the cloud run; its output is written to
   `/tmp/swe-agent-hybrid-warmup.log`.

## Core Components

- `swe_agent/` — The agent package: tool-calling loop, tools, Ollama transport, sessions.
- `swe_agent.py` — Thin launcher shim into the package CLI.
- `swe_agent/server.py` — HTTP/SSE bridge so a web UI can drive the agent (see Web UI).
- `web/` — A React/Vite dashboard whose coding chat streams from the agent.
- `ollama-agent` — Portable bash launcher (resolves paths, ensures server, defaults to coder model).
- `cloud-agent` — Bash launcher for cloud-backed providers (minimax, kimi, nemotron, openai).
- `hybrid-agent` — Cloud-first launcher: warms the local model in the background, then runs `cloud-agent`; `--local` defers to `ollama-agent`.
- `ensure-ollama.sh` — Idempotent helper to start Ollama server if needed.

## Web UI (dashboard)

A browser front-end lives in `web/` (a vendored project-board dashboard). Its
coding chat is wired to the agent over a small HTTP/SSE server — token streaming
and live tool activity included.

```bash
# 1) start the agent server (Ollama running):
python -m swe_agent.server --cwd /path/to/workspace --approval read-only
# 2) start the dashboard:
cd web && npm install && npm run dev   # http://localhost:3000
```

The server binds `127.0.0.1` and defaults to READ_ONLY; pass `--token` to require
a bearer token. Details: [docs/dashboard-integration.md](docs/dashboard-integration.md).

## Tools Overview

**File System**
- ls (recursive support), glob, get_file_info, create_directory
- read_file, read_multiple_files
- write_file, edit, multi_edit, apply_patch
- delete_file, move_file

**Search**
- grep (regex + glob + context_lines)

**Execution**
- run_command (aliases: bash, shell; with cwd, description, timeout, background)
- bash_output, kill_bash (poll / stop background processes)

**Planning**
- todo_write / todo_read (structured visible task lists)

**Git & Patching**
- git_status, git_diff, git_log, git_show, git_commit
- apply_patch (unified diffs / git apply)

**Testing & Quality**
- run_tests (auto-detects pytest, npm test, cargo, go test, etc.)
- run_linter, run_type_checker

**Codebase Analysis**
- get_project_overview, get_directory_tree

**Research**
- web_search, web_fetch

**Sub-agents (Parallelism)**
- spawn_subagent (task, description, mode; supports 20+ concurrent). `mode` is one of
  `audit`/`review` (read-only workers, run in the parent cwd) or `implement`/`test`
  (mutating workers, run in an isolated git worktree under `.agent/worktrees/<id>`).
  Default mode is `audit`. Mutating modes are refused when the parent is read-only or
  when the cwd is not a git repo.
- get_subagent_result (poll for summary; reports mode/status and whether a diff exists)
- get_subagent_diff (inspect a mutating sub-agent's worktree changes)
- discard_subagent_workspace (drop a mutating sub-agent's worktree), list_active_subagents

**Completion**
- task_complete (explicit, structured end-of-run signal)

Sub-agents have **completely private context**. They never leak their full history or intermediate tool calls into the parent. Only a concise summary is returned.

## Subagent Isolation

- Each spawn creates an independent thread with its own `messages` list.
- Sub-agents run with the full toolset.
- When finished (or step limit), they produce a clear summary.
- Parent only receives: "spawned id=xxx" immediately, then the summary via get_subagent_result.
- This prevents context bloat while enabling true parallel work (e.g., explore different modules simultaneously).

**Known limitations (this pass):**
- Mutating (`implement`/`test`) sub-agents run in a git worktree forked from `HEAD`, so they do **not** see the parent's uncommitted/staged/untracked changes — they work from a clean committed base. Seeding in-progress parent changes is a later step.
- `spawn_subagent` is treated as a mutating/exec tool, so it is blocked under read-only (`--plan`) approval. Read-only `audit`/`review` sub-agents are therefore only spawnable from a non-read-only parent; relaxing this is a follow-up.
- Adopting a mutating sub-agent's diff into the parent tree is a deliberate, separate step (not automatic).

Example usage in agent:
```
Spawn several sub-agents for different parts of the codebase, then collect summaries.
```

## Testing

The agent loop, tool-call parsing, and approval gating are covered by a fast,
hermetic test suite that needs **no running Ollama server** — the model is
driven through `Agent`'s built-in `mock` hook.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests live under `tests/`. They cover inline tool-call recovery (including the
fenced-JSON double-dispatch regression), native-call normalization, the
step/termination loop, observation truncation, and the full approval-gating
matrix (read-only / default / auto-accept / yolo + danger detection).

## Requirements

- Python 3.8+
- requests
- Ollama (with hhao/qwen2.5-coder-tools:7b recommended)

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
