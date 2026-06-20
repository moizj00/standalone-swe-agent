# Project Board Dashboard — SWE Agent web UI

A React + Vite dashboard that fronts the standalone SWE agent. The "Coding
Workspace" tab is a live chat against the agent's real tool-calling loop
(running locally on Ollama), streaming tokens and tool activity over SSE.

> Originally a Google AI Studio applet wired to Gemini. It has been re-pointed
> at the local SWE agent: `web/server.ts` is now a thin proxy to the Python
> agent server, and `src/components/CodingMode.tsx` consumes the agent's
> Server-Sent Events. See [../docs/dashboard-integration.md](../docs/dashboard-integration.md).

## Run it

**1. Start the agent server** (from the repo root, with Ollama running):

```bash
python -m swe_agent.server --cwd /path/to/the/workspace --approval read-only
# optional hardening:
#   --token "$(openssl rand -hex 16)"   # require a bearer token
#   --approval auto-accept              # allow file edits (still refuses dangerous shell)
```

It binds `127.0.0.1:8765` by default. `read-only` blocks all mutations — a safe
default for a network-reachable agent.

**2. Start the dashboard:**

```bash
cd web
npm install
cp .env.example .env.local   # set AGENT_SERVER_URL / token if you used one
npm run dev                  # http://localhost:3000
```

The dashboard proxies `/api/chat`, `/api/chat/stream`, and `/api/tools` to the
agent; the bearer token (if any) stays server-side.

## Notes

- The **Tools** tab now shows the agent's *real* registry (read-only). Tools are
  Python `ToolSpec`s, not editable at runtime — add one in `swe_agent/tools/`.
- The **VS Code** tab still writes `.vscode/*` locally (unchanged).
- Security: the agent runs real shell/file operations. Keep it on `127.0.0.1`,
  set a token, and prefer `read-only`/`auto-accept` over `yolo`.
