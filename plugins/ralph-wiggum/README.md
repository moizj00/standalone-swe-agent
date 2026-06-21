# Ralph Wiggum Plugin

Implementation of the **Ralph Wiggum** technique for iterative, self-referential AI development loops in Claude Code.

> Adapted from Anthropic's official [`ralph-loop`](https://github.com/anthropics/claude-code) plugin (Apache-2.0). See [Attribution](#attribution).

## What is Ralph Wiggum?

Ralph Wiggum is a development methodology based on continuous AI agent loops. As Geoffrey Huntley describes it: **"Ralph is a Bash loop"** - a simple `while true` that repeatedly feeds an AI agent the *same* prompt file, allowing it to iteratively improve its work until completion.

The technique is named after the character from The Simpsons, embodying the philosophy of persistent iteration despite setbacks.

### Core Concept

This plugin implements Ralph using a **Stop hook** that intercepts Claude's exit attempts:

```bash
# You run ONCE:
/ralph-loop "Your task description" --completion-promise "DONE"

# Then Claude Code automatically:
# 1. Works on the task
# 2. Tries to exit
# 3. Stop hook blocks exit
# 4. Stop hook feeds the SAME prompt back
# 5. Repeat until completion (promise detected or max iterations)
```

The loop happens **inside your current session** - you don't need external bash loops. The Stop hook in `hooks/stop-hook.sh` creates the self-referential feedback loop by blocking normal session exit and re-injecting the prompt as `{"decision":"block","reason":<prompt>}`.

This creates a **self-referential feedback loop** where:
- The prompt never changes between iterations
- Claude's previous work persists in files
- Each iteration sees modified files and git history
- Claude autonomously improves by reading its own past work in files

## How It Works (the loop driver)

`hooks/stop-hook.sh` runs on every `Stop` event and:

1. Looks for the state file `.claude/ralph-loop.local.md`; exits cleanly (allowing exit) if it is absent.
2. Parses the YAML frontmatter for `iteration`, `max_iterations`, `completion_promise`, and `session_id`.
3. **Session isolation** - if the state file's `session_id` does not match the current hook's session, it exits without blocking or touching the file (so a loop in one session never traps another session).
4. **Validates** that `iteration` and `max_iterations` are numeric before any arithmetic; a corrupted state file is reported and removed.
5. **Max-iteration stop** - if `max_iterations > 0` and `iteration >= max_iterations`, it stops the loop and removes the state file.
6. Reads the transcript JSONL (`transcript_path` from the hook input) and extracts the **last assistant text block** with `jq`.
7. **Completion-promise detection** - extracts the contents of `<promise>...</promise>` with `perl`, normalizes whitespace, and compares it to `completion_promise` using a **literal** string match (`=` in `[[ ]]`, not glob pattern matching). On a match, it stops and removes the state file.
8. Otherwise it **increments `iteration`** (atomic temp-file + `mv`) and emits:
   ```json
   { "decision": "block", "reason": "<the SAME prompt>", "systemMessage": "..." }
   ```
   which re-injects the unchanged prompt for the next iteration.

Graceful exits (`exit 0` with the state file removed) are used for every error case - missing transcript, no assistant messages, jq parse failure, empty prompt - so a broken loop never wedges the session.

## Quick Start

```bash
/ralph-loop "Build a REST API for todos. Requirements: CRUD operations, input validation, tests. Output <promise>COMPLETE</promise> when done." --completion-promise "COMPLETE" --max-iterations 50
```

Claude will:
- Implement the API iteratively
- Run tests and see failures
- Fix bugs based on test output
- Iterate until all requirements met
- Output the completion promise when done

## Commands

### `/ralph-loop`

Start a Ralph Wiggum loop in your current session.

**Usage:**
```bash
/ralph-loop "<prompt>" --max-iterations <n> --completion-promise "<text>"
```

**Options:**
- `--max-iterations <n>` - Stop after N iterations (default: `0`, unlimited)
- `--completion-promise <text>` - Phrase that signals completion (quote multi-word phrases)

### `/cancel-ralph`

Cancel the active Ralph Wiggum loop (removes `.claude/ralph-loop.local.md`).

```bash
/cancel-ralph
```

### `/help`

Explain the plugin, its commands, and the technique.

## Completion-Promise Discipline

To signal completion, Claude must output the configured phrase wrapped in a `<promise>` tag:

```
<promise>COMPLETE</promise>
```

The hook matches the tag contents **literally** against `--completion-promise`. Because it is an exact string match (not a pattern), you **cannot** encode multiple outcomes ("SUCCESS" vs "BLOCKED") in a single promise - always use `--max-iterations` as your primary safety net.

**The cardinal rule:** only emit the promise when the statement is **completely and unequivocally true**. Do not output a false promise to escape the loop - even if you feel stuck, the task seems impossible, or you have been running a long time. The loop is designed to continue until genuine completion; if it should stop, the promise statement will become true naturally. To abort deliberately, the operator runs `/cancel-ralph`.

## Prompt Writing Best Practices

1. **Clear completion criteria** - spell out exactly what "done" means and the promise to emit.
2. **Incremental goals** - break large tasks into phases so each iteration makes verifiable progress.
3. **Self-correction** - prefer TDD-style prompts (write failing tests -> implement -> run -> fix -> repeat).
4. **Escape hatches** - always set `--max-iterations` as a safety net for impossible tasks.

```markdown
Build a REST API for todos.

When complete:
- All CRUD endpoints working
- Input validation in place
- Tests passing (coverage > 80%)
- README with API docs
- Output: <promise>COMPLETE</promise>
```

## Philosophy

- **Iteration > perfection** - let the loop refine the work.
- **Failures are data** - "deterministically bad" failures are predictable and informative.
- **Operator skill matters** - success depends on writing good prompts.
- **Persistence wins** - the loop handles retry logic automatically.

## When to Use Ralph

**Good for:** well-defined tasks with clear success criteria, work that benefits from iteration and self-correction (getting tests to pass), greenfield projects, and tasks with automatic verification (tests, linters).

**Not good for:** tasks requiring human judgment or design decisions, one-shot operations, unclear success criteria, and production debugging.

## Windows / WSL Compatibility (git-bash note)

The stop hook is a bash script and requires **Git for Windows** to run properly.

**Issue:** On Windows, the `bash` command may resolve to WSL bash (often misconfigured) instead of Git Bash, causing the hook to fail with errors like:
- `wsl: Unknown key 'automount.crossDistro'`
- `execvpe(/bin/bash) failed: No such file or directory`

**Workaround:** Edit the installed plugin's `hooks/hooks.json` to invoke Git Bash explicitly:

```json
"command": "\"C:/Program Files/Git/bin/bash.exe\" ${CLAUDE_PLUGIN_ROOT}/hooks/stop-hook.sh"
```

**Note:** Use `Git/bin/bash.exe` (the wrapper with a proper PATH), **not** `Git/usr/bin/bash.exe` (raw MinGW bash without utilities like `jq`/`perl`/`awk` on PATH). The hook depends on `jq`, `perl`, `sed`, `awk`, and `grep` being available.

## Requirements

- `bash`
- `jq` (transcript parsing and JSON emission)
- `perl` (multiline `<promise>` extraction)
- `sed`, `awk`, `grep` (frontmatter and prompt parsing)

## Attribution

This plugin is a faithful adaptation of Anthropic's official **`ralph-loop`** plugin, distributed under the **Apache License, Version 2.0**. The original implementation - the Stop-hook loop driver, the literal promise match, numeric validation, session isolation, and the state-file format - is the work of Anthropic, PBC. This `ralph-wiggum` variant re-namespaces and re-brands it while keeping the hook logic faithful to the reference.

See the bundled [`LICENSE`](./LICENSE) for the full Apache-2.0 text and the attribution notice.

## Learn More

- Original technique: https://ghuntley.com/ralph/
- Ralph Orchestrator: https://github.com/mikeyobrien/ralph-orchestrator

## For Help

Run `/help` in Claude Code for the detailed command reference and examples.
