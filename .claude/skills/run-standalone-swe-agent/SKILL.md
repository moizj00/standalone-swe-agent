---
name: run-standalone-swe-agent
description: Build, run, smoke-test, and screenshot standalone-swe-agent. Use when asked to start the agent, run its tests, drive the CLI, exercise the HTTP/SSE server, launch or screenshot the web dashboard, or verify changes end-to-end.
---

`standalone-swe-agent` is three surfaces in one repo:

1. A Python CLI (`swe_agent.py` / `ollama-agent`) that runs a ReAct tool-calling loop against a local Ollama model.
2. A stdlib HTTP/SSE bridge (`python -m swe_agent.server`) that lets HTTP clients drive the same loop.
3. A React + Vite dashboard in `web/` whose "Coding Mode" tab proxies through the server to the agent.

The driver lives at `.claude/skills/run-standalone-swe-agent/smoke.sh` and exercises every layer without needing Ollama. It boots the server, curls health + tool list, runs the CLI in scripted-mock mode, spins up the web dashboard, and writes a Playwright screenshot to `/tmp/shots/coding.png`. All paths below are relative to the repo root.

## Prerequisites

Python 3.11 and Node 22 are already on PATH in this container. Playwright's bundled Chromium is at `/opt/node22/lib/node_modules/playwright` — the screenshot driver imports it directly, so you do **not** need `playwright install`.

```bash
pip install -r requirements-dev.txt   # pytest + requests
```

For the web step on a fresh checkout you also need `web/node_modules` — `smoke.sh` runs `npm install` automatically if it's missing (~10 s, ~380 packages).

Real chat against the agent (CLI without `--dry-run`, or POSTing to `/api/chat`) needs a live Ollama daemon on `:11434` with `hhao/qwen2.5-coder-tools:7b` pulled. Ollama is **not** installed in this container; the smoke driver intentionally bypasses every code path that requires it.

## Run (agent path) — the smoke driver

One command verifies the whole stack:

```bash
bash .claude/skills/run-standalone-swe-agent/smoke.sh
```

What it does, in order (~25 s):

| step | what gets exercised |
|---|---|
| 1 | `python -m pytest` — 142 tests, agent loop / tools / server / security / gating |
| 2 | `python swe_agent.py --dry-run "smoke"` — CLI with the built-in `scripted_mock`, no Ollama |
| 3 | `python -m swe_agent.server --no-preflight --port 8765 --cwd /tmp` + `curl /api/health` + `curl /api/tools` (expect 35 tools / 41 reserved aliases) |
| 4 | `cd web && npm run dev` + `node screenshot.mjs coding` — Playwright drives the dashboard to **Coding Mode**, then writes `/tmp/shots/coding.png`. The screenshot is only "real" when step 3 is also up: that's what makes the tab header read `Tool Schema Registry (35)`. |

Flags:

```bash
bash .claude/skills/run-standalone-swe-agent/smoke.sh --quick    # skip pytest (step 1)
bash .claude/skills/run-standalone-swe-agent/smoke.sh --no-web   # skip dev server + screenshot (step 4)
```

Logs and artifacts:

- agent server stdout/stderr → `/tmp/agent.log`
- web dev server stdout/stderr → `/tmp/web-dev.log`
- screenshots → `/tmp/shots/` (`coding.png`, plus `overview.png` if you call `screenshot.mjs overview`)

The trap kills both servers on exit; you'll see `... Killed` messages from the kernel reaping them — that's success, not failure. Check the script's exit code.

## Run pieces individually

When you're iterating on one layer it's faster to drive it directly than to rerun the whole smoke. These are the exact commands `smoke.sh` runs:

```bash
# Tests only
python -m pytest                                              # 142 passed in ~7 s

# CLI, scripted mock (no Ollama, --dry-run forces yolo to avoid prompts)
python swe_agent.py --dry-run "any task"

# HTTP/SSE server, no Ollama preflight
python -m swe_agent.server --no-preflight --port 8765 --cwd /tmp &
curl -sf http://127.0.0.1:8765/api/health
curl -sf http://127.0.0.1:8765/api/tools | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["tools"]))'
pkill -9 -f swe_agent.server

# Web dashboard (in a separate shell from the server above)
cd web && npm install && npm run dev &   # http://127.0.0.1:3000, logs to stdout
# then drive it:
node .claude/skills/run-standalone-swe-agent/screenshot.mjs coding
# stop with:  pkill -9 -f 'tsx server.ts'
```

`screenshot.mjs` accepts `overview` (default landing) or `coding` (the agent tab). It prints JSON of `{ screenshot, text, errs }` to stdout so you can grep for assertions in scripts.

## Run (human path)

For a real session with Ollama running you'd skip the smoke driver and do:

```bash
ollama serve &                                    # if not already up
ollama pull hhao/qwen2.5-coder-tools:7b
./ollama-agent "Add type hints to swe_agent/cli.py"   # one-shot
./ollama-agent                                       # interactive REPL
```

For the dashboard pointed at a real workspace:

```bash
python -m swe_agent.server --cwd /path/to/workspace --approval read-only
cd web && npm run dev   # → http://localhost:3000 → "Coding Mode"
```

Both are useless headless without Ollama; use the smoke driver to verify everything else.

## Gotchas

- **The dashboard tab header is the cheap integration check.** `Tool Schema Registry (35)` means the React app → Node proxy → Python `/api/tools` round-trip succeeded. `Tool Schema Registry` (no count) or `(0)` means the agent server isn't running or `--no-preflight` was forgotten and Ollama isn't reachable.
- **`npm run dev`'s child node process survives `kill` of the npm shell.** A previous run that exited uncleanly will hold `:3000` and the next `npm run dev` dies with `EADDRINUSE`. `smoke.sh` calls its cleanup hook *before* launching too, but if you're driving manually use `pkill -9 -f 'tsx server.ts'`.
- **`--dry-run` implies `--yolo`.** The scripted mock can't answer approval prompts, so the CLI flips to no-prompt mode automatically. Don't try to combine `--dry-run --plan` and expect read-only — the CLI overrides it.
- **The CLI defaults to a model preflight.** Without `--no-preflight` (server) or with no `--dry-run` (CLI) the process will try to hit `http://localhost:11434/api/tags` and fail fast if Ollama isn't there. Both flags are the only reason the smoke script runs in this container.
- **The Coding-Mode browser console shows `ERR_CERT_AUTHORITY_INVALID`** on every page load. It's an external favicon request, harmless — the screenshot driver records it in `errs` but treat it as background noise, not a regression.
- **`pytest -q` is the test config.** `pytest.ini` sets `pythonpath = .` and `addopts = -q`. Don't `cd tests/` to run a subset — invoke from the repo root: `python -m pytest tests/test_server.py`.
- **`web/server.ts` reads `AGENT_SERVER_URL` and `SWE_AGENT_SERVER_TOKEN` from env.** Defaults are `http://127.0.0.1:8765` and no token — that matches the smoke driver. If you set a token on the agent, export the same value before `npm run dev` or the proxy returns 401.

## Troubleshooting

- **`npm run dev` exits with `EADDRINUSE: ... :3000`** — a previous tsx process is still listening. `pkill -9 -f 'tsx server.ts'` then retry. (smoke.sh handles this; manual flows don't.)
- **`/api/tools` returns 502 from the proxy** — agent server (`:8765`) isn't running or rejected auth. `curl http://127.0.0.1:8765/api/health` directly to confirm; if that's `connection refused`, relaunch with `--no-preflight`.
- **`requests.exceptions.ConnectionError` from pytest** — only the server tests need a free port; they use port 0 (kernel-assigned) so this shouldn't happen. If it does, something else is binding the loopback range — restart the container.
- **Screenshot is mostly blank** — Vite compiles routes on demand and the first load can take a few seconds. `screenshot.mjs` already does `waitUntil: "networkidle"` + a 1.5 s wait after the tab click; if it's still empty, the dev server died — check `/tmp/web-dev.log`.
- **`Killed` at the end of `smoke.sh`** — the script's `trap` killed its background servers on EXIT. Not a failure. Trust the exit code, not the kernel messages.
