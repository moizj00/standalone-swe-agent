# standalone-swe-agent

 Standalone Python SWE agent with a React/Vite dashboard. The Python agent owns
 the CLI, tool loop, approvals, providers, HTTP/SSE bridge, and tests; `web/`
 is the dashboard proxy/UI.

## Commands

| Command | Purpose |
|---|---|
| `pip install -r requirements-dev.txt` | Install Python runtime/test deps |
| `python -m pytest` | Run Python tests from `pytest.ini` |
| `python swe_agent.py "task"` | Run agent directly |
| `./ollama-agent "task"` | Run via launcher |
| `python -m swe_agent.server --cwd /path/to/workspace --approval read-only` | Start local HTTP/SSE bridge |
| `cd web && npm install` | Install dashboard deps |
| `cd web && npm run dev` | Start dashboard dev server |
| `cd web && npm run lint` | Type-check dashboard |
| `cd web && npm run build` | Build dashboard and Node server |

## Architecture

- `swe_agent/` - core Python package: agent loop, CLI, config, sessions, approvals.
- `swe_agent/tools/` - tool specs and implementations.
- `swe_agent/providers/` - provider integrations and normalization.
- `swe_agent/server.py` - local HTTP/SSE bridge used by the dashboard.
- `swe_agent.py` - compatibility CLI shim into `swe_agent.cli`.
- `tests/` - hermetic pytest coverage; should not require Ollama or network.
- `web/src/` - React dashboard source.
- `web/server.ts` - Express/Vite proxy to the Python agent server.
- `docs/` - project docs; `mcps/` contains MCP schema fixtures.

## Testing Notes

- Add focused pytest coverage for agent loops, tool dispatch, provider behavior,
  approval gating, server events, and security-sensitive changes.
- For dashboard changes, run `cd web && npm run lint`; also run build for routing,
  type, or server integration changes.
- Keep tests hermetic; do not require Ollama or network access.

## Gotchas

- Default server work should use `--approval read-only`; only loosen approval
  when the task requires writes.
- `web/server.ts` keeps bearer tokens server-side while proxying to the Python bridge.
- Real chat paths require Ollama, but tests use mocks and should run without it.
- Do not commit secrets, populated `.env` files, or local tokens.
