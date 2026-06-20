# Web dashboard ↔ SWE agent integration

The `web/` dashboard (a vendored Google AI Studio applet, originally Gemini-backed)
is wired to the standalone SWE agent. This is "Option (a)" from the integration
assessment: a small HTTP/SSE server on the Python agent, with the dashboard
proxying its chat to it.

## Architecture

```
 Browser (React, CodingMode.tsx)
    │  POST /api/chat/stream   (Server-Sent Events)
    ▼
 web/server.ts  (Express, same-origin proxy; holds the bearer token)
    │  POST /api/chat/stream
    ▼
 swe_agent/server.py  (stdlib HTTP/SSE, session→Agent registry)
    │  drives Agent.run_turn() with event_cb → SSE events
    ▼
 swe_agent/agent.py  ──▶ Ollama /api/chat  (real tools, approval gating, compaction)
```

The agent stays the single source of truth: real tool execution, approval
gating (`Agent._gate`), inline-tool-call recovery + dedupe, context compaction,
native Ollama streaming, and JSONL session persistence are all reused unchanged.

## The contract

`POST /api/chat` → `{ text, session_id }` (non-streaming, drop-in).

`POST /api/chat/stream` → `text/event-stream`. Request body:
`{ messages: GeminiContent[], session_id? }`. The server translates Gemini
`Content[]` (`{role, parts:[{text}]}`, role `model`→`assistant`) into the
agent's `{role, content}` shape. It keeps one live `Agent` per `session_id`
(self-healing: an unknown id replays the sent history). Events emitted:

| event         | payload                          | meaning                          |
|---------------|----------------------------------|----------------------------------|
| `session`     | `{session_id}`                   | sent first; client stores the id |
| `step`        | `{n}`                            | loop iteration started           |
| `token`       | `{text}`                         | streamed assistant token         |
| `assistant`   | `{content}`                      | a step's full assistant text     |
| `tool_call`   | `{name, arguments}`              | a tool is about to run           |
| `tool_result` | `{name, content}`                | that tool's (truncated) output   |
| `final`       | `{text}`                         | authoritative final answer       |
| `error`       | `{message}`                      | model/loop error                 |

`GET /api/tools` → the agent's real registry serialized as Gemini
`functionDeclarations` (UPPERCASE types) for the dashboard's Tools tab (read-only).

`GET /api/health` → `{status, model, approval, tools, cwd}`.

## Running

See [../web/README.md](../web/README.md). In short: start the agent
(`python -m swe_agent.server --cwd <workspace>`), then the dashboard
(`cd web && npm install && npm run dev`).

## Security model

The agent executes real shell commands and file writes, so the server is
hardened for its network-facing role:

- **Loopback by default.** The agent binds **127.0.0.1**; the Node proxy now also
  binds `127.0.0.1` (opt into LAN with `BIND_HOST=0.0.0.0`).
- **Secure-by-default refusal.** The server refuses to start with no `--token`
  when approval is non-READ_ONLY *or* the bind is non-loopback. Override only
  with `--insecure`.
- **READ_ONLY default.** All mutations **and code-executing tools** (incl.
  `run_linter`/`run_type_checker`, which run project-controlled binaries) are
  blocked by `Agent._gate` before they run. `auto-accept` allows edits but still
  refuses danger-flagged commands; `yolo` allows everything (requires a token).
- **Workspace confinement.** The server builds the agent's `ToolContext` with
  `confine=True`, so file/exec tools cannot read or write outside the `--cwd`
  workspace (absolute paths and `../` traversal are rejected).
- **SSRF guard.** `web_fetch` blocks non-http(s) schemes and hosts resolving to
  loopback / link-local (incl. `169.254.169.254`) / private / reserved ranges,
  and re-validates every redirect hop.
- **Bearer token** (`--token` / `SWE_AGENT_SERVER_TOKEN`), compared in
  constant time; the Node proxy attaches it so it never reaches the browser.
- Request bodies are capped (16 MB → 413); client `session_id`s are validated
  (`^[A-Za-z0-9_-]{1,64}$`); a stalled SSE reader times out instead of pinning
  the session lock; internal exception text is not echoed to HTTP clients.

## What was intentionally left out (next steps)

- Per-tool approval round-trip over HTTP (today it's a fixed mode per server).
- An explicit cancel endpoint (a client disconnect / SSE-write timeout aborts the
  turn, but there's no dedicated cancel/interrupt call).
- Per-principal session ownership — sessions are validated and random, but a
  shared token is one trust domain (single-operator model; see the docstring).
- Multi-tenant isolation of the process-global subagent executor.
