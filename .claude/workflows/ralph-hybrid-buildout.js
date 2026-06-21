export const meta = {
  name: 'ralph-hybrid-buildout',
  description: 'Build CLI Ralph wiring, hybrid/ralph launchers, and the ralph-wiggum plugin; adversarially review each',
  phases: [
    { title: 'Build', detail: 'cli wiring, launchers, plugin — disjoint files, parallel' },
    { title: 'Review', detail: 'adversarial check of each deliverable vs its spec' },
  ],
}

const REPO = '/home/moizjmj/standalone-swe-agent'

const CLI_BUILD = `You are editing exactly ONE file: ${REPO}/swe_agent/cli.py — to expose the ALREADY-IMPLEMENTED Ralph loop (in swe_agent/ralph.py) via the CLI. Do NOT touch any other file. Do NOT run the full pytest suite (other agents edit this repo concurrently); only do a syntax check at the end.

First READ ${REPO}/swe_agent/cli.py in full and ${REPO}/swe_agent/ralph.py to confirm the signature:
  run_ralph(agent, task, *, max_iterations=0, completion_promise=None, verbose=True, on_iteration=None) -> str

Apply these EXACT edits (the code already matches this repo; do not invent variations, and do NOT add any 'critic'/'critic_gate' references):

1) IMPORTS. In the multi-line \`from .config import (...)\` tuple, add \`RALPH_STATE_FILE\` to the imported names. Then add a new line next to the other sibling imports: \`from .ralph import run_ralph\`.

2) HELP_TEXT. Immediately after the line \`  /loop-stats        show loop-guard detector state\` add these two lines (keep them inside the triple-quoted HELP_TEXT string):
  /ralph <prompt>    Ralph loop: re-run a task each pass until done [--max-iterations N --completion-promise TEXT]
  /cancel-ralph      cancel an active Ralph loop

3) Add this helper function immediately BEFORE \`def handle_slash(\`:

def _parse_ralph_args(arg: str):
    """Parse '<prompt words> [--max-iterations N] [--completion-promise TEXT]' from /ralph."""
    import shlex
    try:
        tokens = shlex.split(arg)
    except ValueError:
        tokens = arg.split()
    max_it, promise, parts, i = 0, None, [], 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--max-iterations" and i + 1 < len(tokens):
            try:
                max_it = int(tokens[i + 1])
            except ValueError:
                max_it = 0
            i += 2
        elif t == "--completion-promise" and i + 1 < len(tokens):
            promise = tokens[i + 1]
            i += 2
        else:
            parts.append(t)
            i += 1
    return " ".join(parts), max_it, promise

4) Inside handle_slash, insert these two elif branches immediately BEFORE the \`elif cmd in ("loop", "loop-stats"):\` branch:

    elif cmd == "ralph":
        if not arg:
            print("Usage: /ralph <prompt> [--max-iterations N] [--completion-promise TEXT]")
        else:
            prompt, max_it, promise = _parse_ralph_args(arg)
            if not prompt:
                print("Ralph needs a task prompt.")
            else:
                run_ralph(agent, prompt, max_iterations=max_it, completion_promise=promise)
    elif cmd == "cancel-ralph":
        sf = ctx.cwd / RALPH_STATE_FILE
        if sf.exists():
            sf.unlink()
            print("Cancelled Ralph loop.")
        else:
            print("No active Ralph loop.")

5) In parse_args, immediately after the \`--no-intent-gate\` add_argument call, add:

    p.add_argument("--ralph", action="store_true",
                   help="Ralph loop: re-feed the SAME task each iteration until a completion promise or limit")
    p.add_argument("--max-iterations", type=int, default=0,
                   help="Ralph: max outer-loop iterations (0 = unlimited, capped by RALPH_HARD_CAP)")
    p.add_argument("--completion-promise", default=None,
                   help="Ralph: phrase that, inside <promise>...</promise>, ends the loop")

6) In main(), find the \`if args.task:\` block. It currently is:

        if args.task:
            agent.add_user(args.task)
            try:
                result = agent.run_turn()
                if result:
                    print(f"\\n\\033[1mresult>\\033[0m {result}")
            except KeyboardInterrupt:
                print("\\n\\033[33m[interrupted]\\033[0m")

Replace it with (note: in ralph mode do NOT pre-add the task — run_ralph feeds it each iteration):

        if args.task:
            try:
                if getattr(args, "ralph", False):
                    result = run_ralph(
                        agent, args.task,
                        max_iterations=args.max_iterations,
                        completion_promise=args.completion_promise,
                    )
                else:
                    agent.add_user(args.task)
                    result = agent.run_turn()
                if result:
                    print(f"\\n\\033[1mresult>\\033[0m {result}")
            except KeyboardInterrupt:
                print("\\n\\033[33m[interrupted]\\033[0m")

Leave the existing session.save(...) block that follows untouched.

Finally, verify SYNTAX ONLY:
  cd ${REPO} && python3 -c "import ast; ast.parse(open('swe_agent/cli.py').read()); print('cli.py syntax OK')"
Report exactly which edits you made and the syntax-check output.`

const LAUNCHERS_BUILD = `You are creating launcher scripts in ${REPO}. Create NEW files only; do not modify existing files except making your new scripts executable. Do NOT run the full pytest suite.

Reference the existing launchers for exact style (READ them): ${REPO}/ollama-agent, ${REPO}/cloud-agent, ${REPO}/ensure-ollama.sh. They use \`set -euo pipefail\`, resolve AGENT_DIR via \`SCRIPT_PATH="$(readlink -f "\${BASH_SOURCE[0]}")"; AGENT_DIR="$(dirname "$SCRIPT_PATH")"\`, and \`exec python3 "$AGENT_DIR/swe_agent.py" ...\`.

Create THREE files:

(A) ${REPO}/hybrid-agent  — per this design: start a local Ollama warmup in the BACKGROUND, then immediately run the cloud-backed agent. Rules:
  - Resolve AGENT_DIR like the other launchers. PYTHON_AGENT, CLOUD_AGENT="$AGENT_DIR/cloud-agent", OLLAMA_AGENT="$AGENT_DIR/ollama-agent", ENSURE_SCRIPT="$AGENT_DIR/ensure-ollama.sh".
  - Parse args: the launcher OWNS only \`--local\` (consume it, do not forward). It PEEKS \`--dry-run\` (set a flag) but STILL forwards it. Everything else forwards unchanged. Scan all args into a FORWARD array; use the safe expansion \`\${FORWARD[@]+"\${FORWARD[@]}"}\` so an empty array does not trip \`set -u\`.
  - If --local: \`exec "$OLLAMA_AGENT" \${FORWARD[@]+"\${FORWARD[@]}"}\` (let ollama-agent's own preflight report local issues).
  - Cloud-first mode: determine LOCAL_MODEL="\${OLLAMA_AGENT_MODEL:-qwen2.5-coder:7b}" and PROVIDER for display ("\${SWE_AGENT_PROVIDER:-nemotron}", overridden if --provider/-p appears in FORWARD). WARMUP_LOG=/tmp/swe-agent-hybrid-warmup.log. Start a BACKGROUND subshell that: if the --dry-run flag is set, just echoes "dry-run: skipping live Ollama warmup for $LOCAL_MODEL" and exits; otherwise runs "$ENSURE_SCRIPT" (if executable, \`|| true\`) then, if \`command -v ollama\` exists, \`ollama pull "$LOCAL_MODEL" || true\` and \`ollama run "$LOCAL_MODEL" "warmup: reply ok" || true\`, else echoes that ollama is absent. Redirect the subshell to "$WARMUP_LOG" 2>&1, background it with \`&\`, capture WARMUP_PID=$!, \`disown || true\`. The warmup must NEVER block the foreground.
  - Print a concise status line to stderr: provider, local model being warmed, pid, and log path.
  - \`exec "$CLOUD_AGENT" \${FORWARD[@]+"\${FORWARD[@]}"}\`.
  - Include a header comment with usage examples (./hybrid-agent "task"; ./hybrid-agent --local "task"; ./hybrid-agent --provider openai "task").

(B) ${REPO}/ralph-agent  — run the agent in Ralph mode. Rules:
  - Resolve AGENT_DIR, PYTHON_AGENT, ENSURE_SCRIPT like ollama-agent. set -euo pipefail.
  - PROVIDER="\${SWE_AGENT_PROVIDER:-ollama}" (Ralph runs MANY passes, so default to cheap local). MODEL="\${OLLAMA_AGENT_MODEL:-qwen2.5-coder:7b}".
  - If PROVIDER == ollama: ensure the server (\`[[ -x "$ENSURE_SCRIPT" ]] && "$ENSURE_SCRIPT" >/dev/null 2>&1 || true\`) then \`exec python3 "$PYTHON_AGENT" --provider ollama --model "$MODEL" --ralph "$@"\`. Else \`exec python3 "$PYTHON_AGENT" --provider "$PROVIDER" --ralph "$@"\`.
  - Header comment with usage: ./ralph-agent --max-iterations 10 --completion-promise "all tests pass" "make tests pass". Note --max-iterations/--completion-promise are forwarded to the Python --ralph loop.

(C) ${REPO}/tests/test_launchers.py — a HERMETIC pytest (no network, no real ollama) for hybrid-agent forwarding. It must:
  - Copy hybrid-agent into a tmp_path dir, and create executable STUB scripts there named cloud-agent, ollama-agent, ensure-ollama.sh. Each stub writes "$0 $@" (its name + args) to a capture file (e.g. tmp_path/capture.txt) and exits 0. ensure-ollama.sh stub just exits 0.
  - Test 1: run \`hybrid-agent --local --dry-run "explore project"\`; assert the ollama-agent stub was invoked and received \`--dry-run explore project\` (and NOT --local).
  - Test 2: run \`hybrid-agent --no-preflight --dry-run "explore project"\`; assert the cloud-agent stub was invoked with \`--no-preflight --dry-run explore project\`, the command exits 0 promptly (use a timeout), and ollama-agent was NOT invoked.
  - Use subprocess.run with text=True and a small timeout; use shutil.copy and os.chmod(0o755). Keep it self-contained and import-light.

Make hybrid-agent and ralph-agent executable: \`chmod +x ${REPO}/hybrid-agent ${REPO}/ralph-agent\`.

Verify SYNTAX ONLY: \`bash -n ${REPO}/hybrid-agent && bash -n ${REPO}/ralph-agent && echo LAUNCHERS_OK\` and \`python3 -c "import ast; ast.parse(open('${REPO}/tests/test_launchers.py').read()); print('test syntax OK')"\`. Report the files created and the check outputs. Do NOT run pytest.`

const PLUGIN_BUILD = `You are creating a Claude Code plugin at ${REPO}/plugins/ralph-wiggum/ that implements the "Ralph Wiggum" loop technique (re-feed the SAME prompt each iteration via a Stop hook until a completion promise or max-iterations). Create NEW files only.

Anthropic's official implementation is installed locally — READ it as your reference and adapt it faithfully into the new plugin (it is Apache-2.0; keep a LICENSE/attribution note):
  /mnt/c/home/moizjmj/.claude/plugins/cache/claude-plugins-official/ralph-loop/1.0.0/
Read its files: .claude-plugin/plugin.json, hooks/hooks.json, hooks/stop-hook.sh, scripts/setup-ralph-loop.sh, commands/ralph-loop.md, commands/cancel-ralph.md, commands/help.md, README.md.

Produce, under ${REPO}/plugins/ralph-wiggum/, a faithful, working plugin:
  - .claude-plugin/plugin.json  (name: "ralph-wiggum", version "1.0.0", a clear description, author note; valid JSON)
  - hooks/hooks.json            (a Stop hook running bash "\${CLAUDE_PLUGIN_ROOT}/hooks/stop-hook.sh"; valid JSON)
  - hooks/stop-hook.sh          (the loop driver: detect the state file .claude/ralph-loop.local.md, session isolation, validate iteration/max_iterations are numeric, max-iteration stop, read the transcript's last assistant message, detect <promise>TEXT</promise> via literal match against completion_promise, else increment iteration and emit {"decision":"block","reason":<prompt>,"systemMessage":<msg>} to re-inject the SAME prompt)
  - scripts/setup-ralph-loop.sh (parse "<prompt>" [--max-iterations N] [--completion-promise "TEXT"], write the YAML-frontmatter state file with active/iteration/session_id/max_iterations/completion_promise/started_at + the prompt body, print guidance)
  - commands/ralph-loop.md, commands/cancel-ralph.md, commands/help.md (slash commands /ralph-loop, /cancel-ralph, /help mirroring the official copy, but namespaced/branded as ralph-wiggum)
  - README.md (purpose, the technique, usage, completion-promise discipline, the Windows/WSL git-bash note, attribution to Anthropic's ralph-loop)
  - LICENSE (Apache-2.0, since it adapts the official plugin)

Keep the stop-hook logic faithful to the reference (literal promise match, numeric validation, session isolation, graceful exits). Use jq and perl/grep as the reference does.

Verify: \`bash -n ${REPO}/plugins/ralph-wiggum/hooks/stop-hook.sh && bash -n ${REPO}/plugins/ralph-wiggum/scripts/setup-ralph-loop.sh && echo HOOKS_OK\`; validate JSON with \`python3 -c "import json; json.load(open('${REPO}/plugins/ralph-wiggum/.claude-plugin/plugin.json')); json.load(open('${REPO}/plugins/ralph-wiggum/hooks/hooks.json')); print('JSON OK')"\`; \`chmod +x ${REPO}/plugins/ralph-wiggum/hooks/stop-hook.sh ${REPO}/plugins/ralph-wiggum/scripts/setup-ralph-loop.sh\`. Report the tree you created and the check outputs.`

const ISSUE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ok: { type: 'boolean', description: 'true if the deliverable fully meets its requirements with no blocking issues' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          file: { type: 'string' },
          detail: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'file', 'detail', 'fix'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['ok', 'issues', 'summary'],
}

const DELIVERABLES = [
  {
    key: 'cli',
    build: CLI_BUILD,
    review: `Adversarially review the Ralph CLI wiring in ${REPO}/swe_agent/cli.py. READ the file. Verify ALL of: (1) \`from .ralph import run_ralph\` is imported and RALPH_STATE_FILE is imported from .config; (2) _parse_ralph_args exists and correctly separates prompt words from --max-iterations/--completion-promise; (3) handle_slash has working /ralph and /cancel-ralph branches with NO reference to any 'critic'/'critic_gate' symbol (which does NOT exist in this repo's Agent); (4) parse_args defines --ralph, --max-iterations (type=int default 0), --completion-promise (default None); (5) main()'s if args.task branch calls run_ralph when args.ralph is set and does NOT also pre-add the task in that path, but the non-ralph path still does agent.add_user(args.task) then run_turn(); (6) the file is syntactically valid (run: cd ${REPO} && python3 -c "import ast; ast.parse(open('swe_agent/cli.py').read())"). Also grep for 'critic' in cli.py and flag any hit as a blocker. Report ok/issues/summary.`,
  },
  {
    key: 'launchers',
    build: LAUNCHERS_BUILD,
    review: `Adversarially review the launchers in ${REPO}: hybrid-agent, ralph-agent, tests/test_launchers.py. READ all three. Verify: (1) both shell scripts pass \`bash -n\`; (2) hybrid-agent consumes --local (not forwarded) and forwards everything else, peeks --dry-run but still forwards it, uses the safe \${FORWARD[@]+"\${FORWARD[@]}"} expansion, starts the warmup in the BACKGROUND with disown so it cannot block, and execs cloud-agent (or ollama-agent for --local); (3) ralph-agent forwards --ralph to swe_agent.py and ensures ollama only when provider is ollama; (4) both scripts are executable (ls -l). (5) For test_launchers.py: it is hermetic (stubs cloud-agent/ollama-agent/ensure-ollama.sh, no network), and actually asserts the forwarding for both --local and --no-preflight cases. ACTUALLY RUN it: \`cd ${REPO} && PYTHONPATH="$PWD/tests" uv run --no-project --with pytest python -m pytest tests/test_launchers.py -q\` and report pass/fail. Report ok/issues/summary.`,
  },
  {
    key: 'plugin',
    build: PLUGIN_BUILD,
    review: `Adversarially review the plugin at ${REPO}/plugins/ralph-wiggum/. READ every file. Compare against the official reference at /mnt/c/home/moizjmj/.claude/plugins/cache/claude-plugins-official/ralph-loop/1.0.0/. Verify: (1) plugin.json and hooks/hooks.json are valid JSON (run python3 -c json.load on both) and hooks.json wires a Stop hook to hooks/stop-hook.sh via \${CLAUDE_PLUGIN_ROOT}; (2) stop-hook.sh and setup-ralph-loop.sh pass \`bash -n\` and are executable; (3) stop-hook.sh faithfully implements: state-file detection, session isolation, numeric validation of iteration/max_iterations, max-iteration termination, transcript last-message extraction, LITERAL <promise>TEXT</promise> match against completion_promise, and the block/re-inject JSON ({"decision":"block","reason":<prompt>,...}); (4) setup writes the YAML-frontmatter state file with the prompt; (5) commands (ralph-loop, cancel-ralph, help) exist and are branded ralph-wiggum; (6) README + LICENSE (Apache-2.0) + attribution to Anthropic present. Flag anything missing or diverging from the reference's stop semantics. Report ok/issues/summary.`,
  },
]

log('Building 3 deliverables in parallel, each adversarially reviewed as it completes.')

const results = await pipeline(
  DELIVERABLES,
  (d) => agent(d.build, { label: `build:${d.key}`, phase: 'Build' }),
  (buildOut, d) => agent(d.review, { label: `review:${d.key}`, phase: 'Review', schema: ISSUE_SCHEMA })
    .then((verdict) => ({ key: d.key, verdict })),
)

return results.filter(Boolean)
