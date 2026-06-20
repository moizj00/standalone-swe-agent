#!/usr/bin/env bash
# ensure-ollama.sh
# Idempotent helper: make sure Ollama server is reachable.
# Usage: ./ensure-ollama.sh   or source it and call ensure_ollama

set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
LOG_FILE="${OLLAMA_LOG_FILE:-/tmp/swe-agent-ollama-server.log}"

ensure_ollama() {
  if curl -s --max-time 2 "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
    echo "✓ Ollama server already running at ${OLLAMA_URL}"
    return 0
  fi

  echo "→ Starting Ollama server in background..."
  mkdir -p "$(dirname "$LOG_FILE")"
  nohup ollama serve > "$LOG_FILE" 2>&1 &
  OLLAMA_PID=$!
  disown || true

  # Wait up to ~8 seconds for it to come up
  for i in {1..16}; do
    if curl -s --max-time 1 "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
      echo "✓ Ollama server started (pid ${OLLAMA_PID})"
      return 0
    fi
    sleep 0.5
  done

  echo "✗ Ollama did not respond after starting. Check $LOG_FILE"
  return 1
}

list_models() {
  echo "Local Ollama models:"
  curl -s "${OLLAMA_URL}/api/tags" | python3 -c '
import sys, json
data = json.load(sys.stdin)
for m in data.get("models", []):
    print(" -", m.get("name"))
' 2>/dev/null || ollama list || echo "(could not list)"
}

# If script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  ensure_ollama
  echo
  list_models
fi
