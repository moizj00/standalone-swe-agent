# Repository Guidelines

## Project Structure & Module Organization

This repository contains a standalone Python SWE agent plus a React dashboard.
Core Python code lives in `swe_agent/`, with the CLI shim in `swe_agent.py`.
Agent tools are organized under `swe_agent/tools/`, provider integrations under
`swe_agent/providers/`, and HTTP/SSE dashboard bridging in `swe_agent/server.py`.
Tests live in `tests/` and mirror agent, provider, gating, server, and tool
behavior. The web UI lives in `web/`, with React source in `web/src/`, Vite
config in `web/vite.config.ts`, and the Node server in `web/server.ts`. Docs
belong in `docs/`; MCP tool schema fixtures are under `mcps/`.

## Build, Test, and Development Commands

- `pip install -r requirements-dev.txt`: install Python runtime and test dependencies.
- `python -m pytest`: run the full Python test suite configured by `pytest.ini`.
- `python swe_agent.py "task"` or `./ollama-agent "task"`: run the agent locally.
- `python -m swe_agent.server --cwd /path/to/workspace --approval read-only`: start the local agent bridge for the dashboard.
- `cd web && npm install`: install dashboard dependencies.
- `cd web && npm run dev`: run the dashboard development server.
- `cd web && npm run build`: build the Vite app and bundled Node server.
- `cd web && npm run lint`: run TypeScript checking with `tsc --noEmit`.

## Coding Style & Naming Conventions

Python uses 4-space indentation, type hints, and module-level
docstrings for important behavior. Prefer small functions, explicit imports, and
clear names such as `ApprovalMode` and `ToolContext`. Tests use `test_*.py` files
and descriptive `test_*` functions. React components use PascalCase; hooks and
utilities use camelCase.

## Testing Guidelines

Add or update focused pytest coverage for Python behavior changes, especially
agent loops, tool dispatch, approval gating, provider normalization, and server
events. Keep tests hermetic; they should not require Ollama or network access.
For dashboard changes, run `npm run lint` and build when touching routing,
TypeScript types, or server integration.

## Commit & Pull Request Guidelines

Recent history mostly uses concise Conventional Commit-style subjects such as
`feat: add backend setup...`; follow that pattern when possible. Keep commits
scoped to one change. Pull requests should include a short description, testing
performed, linked issues when applicable, and screenshots or screen recordings
for visible dashboard changes.

## Security & Configuration Tips

Do not commit secrets, local tokens, or populated `.env` files. Use `web/.env.example`
as the template for dashboard configuration. Keep destructive agent operations
behind the documented approval modes, and default local server work to
`--approval read-only` unless a task requires writes.
