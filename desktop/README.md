# SWE Agent — Desktop (Electron)

A native desktop window for the standalone SWE coding agent. **Cloud-only build**
— the agent runs on a cloud LLM provider (nemotron / openai / minimax / kimi).
No local Ollama is involved.

## How it works

The Electron main process is a thin orchestrator. It does not re-implement the
agent — it spawns the two servers this repo already has and loads the existing
React dashboard in a window:

```
Electron main (desktop/main.js)
  ├─ generate a random bearer token (shared secret for this run)
  ├─ spawn  : <venv>/python -m swe_agent.server --provider <P> --api-key <…>
  │            --port 8765 --token <tok> --cwd <workspace>   (the tool-calling loop)
  ├─ spawn  : web/ dashboard  (npm run dev → Express + Vite on :3000)
  │            with AGENT_SERVER_URL + SWE_AGENT_SERVER_TOKEN injected, so the
  │            Express proxy reaches Python and keeps the token server-side
  ├─ wait   : poll /api/health (auth'd) then the dashboard, then load the UI
  └─ quit   : kill both child process trees
```

Everything binds to `127.0.0.1`. The browser never sees the bearer token — the
Express proxy attaches it server-side.

## Prerequisites

1. **Python agent installed** in the repo venv:
   ```bash
   python -m venv .venv
   .venv/Scripts/python -m pip install -r requirements.txt   # (requirements-dev.txt to run tests)
   ```
2. **Dashboard deps**: `cd web && npm install`
3. **Electron deps**: `cd desktop && npm install`
4. **A cloud API key** for your chosen provider (see below).

## Cloud provider & API keys

Pick a provider with `SWE_AGENT_PROVIDER` (default `nemotron`). Set the matching
API-key env var before launching:

| Provider   | `SWE_AGENT_PROVIDER` | API key env var(s)                         |
|------------|----------------------|--------------------------------------------|
| NVIDIA     | `nemotron` (default) | `NVIDIA_API_KEY`                           |
| OpenAI     | `openai`             | `OPENAI_API_KEY`                           |
| MiniMax    | `minimax`            | `MINIMAX_API_KEY`                          |
| Kimi       | `kimi`               | `MOONSHOT_API_KEY` (or `KIMI_API_KEY`)     |

An explicit `SWE_AGENT_API_KEY` overrides the per-provider variable. The app
refuses to start (with a clear dialog) if no key is found.

## Run

From the repo root, with your key exported:

```bash
# Windows (PowerShell):  $env:OPENAI_API_KEY="sk-…"; $env:SWE_AGENT_PROVIDER="openai"
# bash:                  export OPENAI_API_KEY="sk-…"; export SWE_AGENT_PROVIDER="openai"

cd desktop && npm start
```

## Environment variables

| Variable                | Default            | Purpose                                            |
|-------------------------|--------------------|----------------------------------------------------|
| `SWE_AGENT_PROVIDER`    | `nemotron`         | Cloud LLM provider                                 |
| `SWE_AGENT_API_KEY`     | (per-provider env) | Explicit key override                              |
| `SWE_AGENT_WORKSPACE`   | repo root          | Folder the agent reads/edits (confined)            |
| `SWE_AGENT_APPROVAL`    | `read-only`        | `read-only` \| `auto-accept` \| `yolo`             |
| `SWE_AGENT_PORT`        | `8765`             | Python agent server port                           |
| `SWE_DASH_PORT`         | `3000`             | Dashboard port                                     |
| `SWE_AGENT_PYTHON`      | `.venv` python     | Python interpreter to run the agent                |

## Safety

The agent runs real shell/file tools. The server is loopback-only and
token-gated, and defaults to `read-only` (no mutations). Raise the posture with
`SWE_AGENT_APPROVAL=auto-accept` only for a workspace you trust.
