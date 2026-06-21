"""Ralph Wiggum loop — "Ralph is a bash loop" (Geoffrey Huntley).

Run the agent on the SAME task prompt over and over until it emits a completion
promise (``<promise>TEXT</promise>``) or the iteration limit is hit. The loop's
memory lives in the FILES the agent edits, not the conversation: each pass re-reads
its own prior work (and git history) and improves on it. Failures are data — the
agent sees the broken test from last pass and fixes it next pass.

Ported from the Claude Code ``ralph-wiggum`` plugin. That plugin drives the loop
with a Stop hook that blocks exit and re-feeds the prompt; here we own the outer
loop directly, calling ``Agent.run_turn`` once per iteration. The agent's other
machinery composes: any verify/critic gate re-arms per pass.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import RALPH_HARD_CAP, RALPH_STATE_FILE

_PROMISE_RE = re.compile(r"<promise>(.*?)</promise>", re.S | re.I)


def extract_promise(text: str) -> Optional[str]:
    """Return the normalized text inside the FIRST <promise>...</promise>, or None."""
    if not text:
        return None
    m = _PROMISE_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


@dataclass
class RalphState:
    """Observable, cancellable loop state mirrored to a markdown file on disk.

    Removing the state file (from this or another terminal) cancels the loop at the
    next iteration boundary — the Python analog of the plugin's /cancel-ralph.
    """

    cwd: Path
    max_iterations: int = 0
    completion_promise: Optional[str] = None
    run_id: str = ""
    iteration: int = 0

    @property
    def path(self) -> Path:
        return self.cwd / RALPH_STATE_FILE

    def write(self, task: str) -> None:
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        promise = f'"{self.completion_promise}"' if self.completion_promise else "null"
        p.write_text(
            "---\n"
            "active: true\n"
            f'run_id: "{self.run_id}"\n'
            f"iteration: {self.iteration}\n"
            f"max_iterations: {self.max_iterations}\n"
            f"completion_promise: {promise}\n"
            "---\n\n"
            f"{task}\n",
            encoding="utf-8",
        )

    def cancelled(self) -> bool:
        return not self.path.exists()

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _seed(task: str, completion_promise: Optional[str], iteration: int) -> str:
    """The prompt re-fed each iteration: the same task plus the iteration banner."""
    lines = [task, "", f"[Ralph iteration {iteration}] You are in a self-referential loop. "
             "Your previous work persists in the files and git history — read it, then improve it."]
    if completion_promise:
        lines.append(
            f"To END the loop, output exactly <promise>{completion_promise}</promise> — but ONLY when "
            "that statement is completely and unequivocally TRUE. Do NOT output a false promise to "
            "escape the loop, even if you think you are stuck. Keep iterating until it is genuinely true."
        )
    return "\n".join(lines)


def run_ralph(
    agent,
    task: str,
    *,
    max_iterations: int = 0,
    completion_promise: Optional[str] = None,
    run_id: Optional[str] = None,
    verbose: bool = True,
    on_iteration: Optional[Callable[[int, str], None]] = None,
) -> str:
    """Drive the agent in a Ralph loop. Returns the final output of the last iteration.

    ``agent`` needs: ``.ctx.cwd``, ``.add_user(str)``, ``.run_turn() -> str``, and
    (optionally) a ``.critic_gate``/verify gate that exposes ``rounds_used``.
    ``max_iterations`` of 0 means "unlimited", clamped to RALPH_HARD_CAP as a
    runaway safety net.
    """
    cwd = Path(getattr(agent.ctx, "cwd", "."))
    run_id = run_id or f"ralph-{uuid.uuid4().hex[:10]}"
    effective_max = max_iterations if max_iterations > 0 else RALPH_HARD_CAP
    unlimited = max_iterations <= 0

    state = RalphState(cwd=cwd, max_iterations=max_iterations,
                       completion_promise=completion_promise, run_id=run_id)

    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    if unlimited:
        say(f"\033[33m⚠ Ralph: no --max-iterations set; capping at {RALPH_HARD_CAP} "
            f"(set RALPH_HARD_CAP or --max-iterations to change).\033[0m")
    if not completion_promise:
        say("\033[33m⚠ Ralph: no --completion-promise set; the loop only ends at the iteration cap.\033[0m")

    final = ""
    for i in range(1, effective_max + 1):
        state.iteration = i
        state.write(task)
        if state.cancelled():  # racey-safe: someone removed the file between writes
            say("\033[33m🛑 Ralph: state file removed — cancelled.\033[0m")
            return final

        say(f"\n\033[1m🔄 Ralph {run_id} — iteration {i}/{effective_max}\033[0m"
            + (f"  (promise: <promise>{completion_promise}</promise>)" if completion_promise else ""))

        # Fresh critic/verify rounds each iteration — each pass is a new attempt at the bar.
        cg = getattr(agent, "critic_gate", None)
        if cg is not None:
            cg.rounds_used = 0

        agent.add_user(_seed(task, completion_promise, i))
        try:
            final = agent.run_turn()
        except KeyboardInterrupt:
            say("\n\033[33m🛑 Ralph: interrupted by user.\033[0m")
            state.cleanup()
            return final

        if on_iteration:
            on_iteration(i, final)

        if completion_promise:
            got = extract_promise(final)
            if got is not None and got == completion_promise:
                say(f"\n\033[32m✅ Ralph: promise met after {i} iteration(s) — "
                    f"<promise>{completion_promise}</promise>.\033[0m")
                state.cleanup()
                return final

        if state.cancelled():
            say("\033[33m🛑 Ralph: state file removed — cancelled.\033[0m")
            return final

    say(f"\n\033[33m🛑 Ralph: reached the iteration cap ({effective_max}) without the promise.\033[0m")
    state.cleanup()
    return final
