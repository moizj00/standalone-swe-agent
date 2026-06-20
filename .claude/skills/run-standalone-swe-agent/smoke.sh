#!/usr/bin/env bash
# smoke.sh — end-to-end smoke driver for standalone-swe-agent.
#
# Exercises every PR-relevant surface without Ollama installed:
#   1. pytest                           (Python loop, tools, server — 142 tests)
#   2. swe_agent.py --dry-run           (CLI + scripted mock loop)
#   3. swe_agent.server --no-preflight  (HTTP/SSE bridge; health + tools)
#   4. web/ vite dev server + Playwright screenshot of Coding Mode
#
# Anything chat-related needs a live Ollama on :11434 with the configured
# model pulled. This driver covers everything else.
#
# Run from the repo root:  bash .claude/skills/run-standalone-swe-agent/smoke.sh
# Flags:
#   --no-web      skip step 4 (web/ install + screenshot)
#   --quick       skip step 1 (pytest)
set -euo pipefail

SKIP_WEB=0
SKIP_TESTS=0
for a in "$@"; do
  case "$a" in
    --no-web)  SKIP_WEB=1 ;;
    --quick)   SKIP_TESTS=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
cd "$REPO"
SKILL_DIR="$REPO/.claude/skills/run-standalone-swe-agent"
mkdir -p /tmp/shots

cleanup() {
  set +e
  [[ -n "${AGENT_PID:-}" ]] && kill -9 "$AGENT_PID" 2>/dev/null
  [[ -n "${WEB_PID:-}" ]]   && kill -9 "$WEB_PID"   2>/dev/null
  pkill -9 -f swe_agent.server 2>/dev/null
  pkill -9 -f 'tsx server.ts'  2>/dev/null
  return 0
}
trap cleanup EXIT

# Defensive pre-cleanup: an old run that crashed can leave node holding :3000
# (npm run dev's child outlives kill -9 of the npm shell). EADDRINUSE on relaunch
# is the symptom; this prevents it.
cleanup
sleep 0.3

step() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }

if [[ $SKIP_TESTS -eq 0 ]]; then
  step "1. pytest"
  python -m pytest -q
fi

step "2. CLI dry-run (scripted mock)"
python swe_agent.py --dry-run "smoke" | tail -10

step "3. agent server: health + tools"
python -m swe_agent.server --no-preflight --port 8765 --cwd /tmp \
  > /tmp/agent.log 2>&1 &
AGENT_PID=$!
# Poll until the HTTP listener accepts a connection (any 2xx/4xx counts).
timeout 15 bash -c 'until curl -sf http://127.0.0.1:8765/api/health >/dev/null; do sleep 0.2; done'
echo "health: $(curl -sf http://127.0.0.1:8765/api/health)"
echo "tools:  $(curl -sf http://127.0.0.1:8765/api/tools \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d["tools"]), "tools,", len(d["reserved"]), "reserved aliases")')"

if [[ $SKIP_WEB -eq 1 ]]; then
  echo
  echo "OK  (skipped --no-web)"
  exit 0
fi

step "4. web dashboard + Playwright screenshot"
cd web
if [[ ! -d node_modules ]]; then
  npm install >/tmp/web-install.log 2>&1 || { tail -30 /tmp/web-install.log; exit 1; }
fi
npm run dev > /tmp/web-dev.log 2>&1 &
WEB_PID=$!
# Vite logs "Dashboard on http://127.0.0.1:3000" once it's bound.
timeout 30 bash -c 'until grep -q "Dashboard on" /tmp/web-dev.log 2>/dev/null; do sleep 0.3; done'
timeout 15 bash -c 'until curl -sf http://127.0.0.1:3000/ >/dev/null; do sleep 0.3; done'
cd "$REPO"
node "$SKILL_DIR/screenshot.mjs" coding

echo
echo "OK   screenshot → /tmp/shots/coding.png   (agent log /tmp/agent.log, web log /tmp/web-dev.log)"
