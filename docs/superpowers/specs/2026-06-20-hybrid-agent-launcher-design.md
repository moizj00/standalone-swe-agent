# Hybrid Agent Launcher Design

## Goal

Provide a fast way to use the coding agent with a cloud model immediately while local Ollama starts, pulls, or warms the preferred local model in the background. The solution should preserve the existing `ollama-agent` and `cloud-agent` launchers and avoid risky provider switching inside an active agent session.

## Scope

Add a lightweight `hybrid-agent` command-line launcher and supporting documentation. The launcher coordinates startup only; it does not change the core agent loop, provider registry, tool dispatch, or session format.

## Recommended User Flow

- `./hybrid-agent "task"` starts local Ollama warmup in the background, then immediately runs the cloud-backed agent.
- `./hybrid-agent --local "task"` forces the local Ollama launcher.
- `./ollama-agent "task"` remains the direct local-only path.
- `./cloud-agent --provider openai "task"` remains the direct cloud-only path.

## Architecture

The launcher should be a Bash script beside `ollama-agent` and `cloud-agent`.

It will:

1. Resolve the repository directory the same way the existing launchers do.
2. Start Ollama through `ensure-ollama.sh` when available.
3. Start a background warmup process for `OLLAMA_AGENT_MODEL`, defaulting to the same model used by `ollama-agent`.
4. Write warmup output to a small log under `/tmp`.
5. Print a concise status line showing the active cloud provider, local model, and warmup log.
6. Execute `cloud-agent` immediately, forwarding normal task arguments.

The warmup process should be best-effort. Failure to pull or warm the local model must not block the active cloud run.

## Provider Behavior

The active run uses `cloud-agent`, which already supports `minimax`, `kimi`, `nemotron`, and `openai`. Existing environment variables continue to apply:

- `SWE_AGENT_PROVIDER` selects the cloud provider.
- Provider API keys such as `OPENAI_API_KEY`, `MINIMAX_API_KEY`, `MOONSHOT_API_KEY`, or `NVIDIA_API_KEY` enable live cloud calls.
- `OLLAMA_AGENT_MODEL` selects the local model to warm.

The launcher should avoid changing cloud defaults in Python. That keeps provider behavior centralized in the existing CLI and provider registry.

## Error Handling

If `--local` is passed, the launcher should directly exec `ollama-agent` and let its existing preflight report local Ollama or model issues.

For cloud-first mode:

- Missing cloud API keys should be reported by `cloud-agent` as they are today.
- Ollama warmup failures should be logged but should not fail the cloud run.
- Missing `ensure-ollama.sh` should be tolerated; warmup can still attempt Ollama commands if available.

## Testing

Verification should include:

- `bash -n hybrid-agent`
- `./hybrid-agent --local --dry-run "explore project"` to confirm local forwarding works.
- `./hybrid-agent --no-preflight --dry-run "explore project"` to confirm cloud-first forwarding and local warmup startup do not block.
- `python3 -m pytest` for the Python suite.

Network-dependent live model pulls or cloud calls are manual checks because tests must remain hermetic.
