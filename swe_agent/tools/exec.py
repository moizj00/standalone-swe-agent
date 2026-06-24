"""Shell execution tools: run_command/bash plus cross-platform background processes.

We never use ``nohup``/``/tmp`` (Unix-only). Background processes use
``subprocess.Popen`` writing to ``tempfile`` files that resolve correctly on
Windows (%TEMP%) and Unix (/tmp). When ``bash`` is available we route commands
through it (so POSIX commands work on Windows git-bash); otherwise we fall back
to the platform default shell.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from typing import Optional, Tuple

from ..config import TOOL_TIMEOUT
from .base import ToolContext, ToolSpec, register

_BASH = shutil.which("bash")


def _invocation(command: str) -> Tuple[object, bool]:
    """Return (args, use_shell) for running ``command``."""
    if _BASH:
        return ([_BASH, "-c", command], False)
    return (command, True)  # cmd.exe / /bin/sh via shell=True


# --------------------------------------------------------------------------- danger detector

_DANGER_PATTERNS = [
    (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", "recursive force delete"),
    (r":\(\)\s*\{\s*:\|:", "fork bomb"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+if=", "raw disk write"),
    (r">\s*/dev/sd", "raw disk write"),
    (r"\bshutdown\b", "system shutdown"),
    (r"\breboot\b", "system reboot"),
    (r"git\s+push\b.*--force", "git force push"),
    (r"\bdel\s+/[a-z]*[sqf]", "windows recursive/force delete"),
    (r"\b(rmdir|rd)\s+/s", "windows recursive rmdir"),
    (r"\bformat\s+[a-zA-Z]:", "windows format drive"),
    (r"Remove-Item\b[^\n]*-Recurse[^\n]*-Force", "powershell recursive force delete"),
    (r"\bFormat-Volume\b", "powershell format volume"),
    (r"\breg\s+delete\b", "registry delete"),
    (r"\bdiskpart\b", "disk partition tool"),
    (r"\bbcdedit\b", "boot configuration edit"),
    (r"(curl|wget)\s+[^\n|]*\|\s*(sudo\s+)?(sh|bash)\b", "pipe download to shell"),
    (r"(iwr|invoke-webrequest)\b[^\n|]*\|\s*iex\b", "powershell pipe to exec"),
    # recursive world-writable chmod, in either flag/mode order (chmod -R 777 / chmod 777 -R)
    (r"\bchmod\b(?=[^\n]*\s-[a-zA-Z]*R)(?=[^\n]*\b[0-7]?777\b)", "recursive world-writable chmod"),
    (r">\s*/etc/", "overwrite of system config under /etc"),
    # sudo with optional flags / env assignments before a destructive verb
    (r"\bsudo\s+(?:(?:-[A-Za-z-]+|[A-Za-z_][A-Za-z0-9_]*=\S+)\s+)*(rm|dd|mkfs|chmod|chown|tee|truncate)\b",
     "privileged destructive command"),
]


def detect_danger(command: str) -> Optional[str]:
    for rx, reason in _DANGER_PATTERNS:
        if re.search(rx, command, re.IGNORECASE):
            return reason
    return None


# --------------------------------------------------------------------------- background registry

class BackgroundRegistry:
    """Tracks Popen-backed background processes and their captured output files."""

    def __init__(self) -> None:
        self._procs = {}
        self._lock = threading.Lock()

    def start(self, command: str, cwd: str, desc: str) -> str:
        bash_id = uuid.uuid4().hex[:8]
        out_path = tempfile.NamedTemporaryFile(prefix=f"swe_bg_{bash_id}_", suffix=".out", delete=False).name
        err_path = tempfile.NamedTemporaryFile(prefix=f"swe_bg_{bash_id}_", suffix=".err", delete=False).name
        ofh = open(out_path, "wb")
        efh = open(err_path, "wb")
        args, use_shell = _invocation(command)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, shell=use_shell, cwd=cwd, stdout=ofh, stderr=efh, **kwargs)
        except Exception as e:
            ofh.close(); efh.close()
            return f"Error starting background command: {e}"
        with self._lock:
            self._procs[bash_id] = {
                "proc": proc, "out": out_path, "err": err_path,
                "ofh": ofh, "efh": efh, "offset_out": 0, "offset_err": 0,
                "command": command,
            }
        return (f"[background] started '{desc}' as bash_id={bash_id} (pid {proc.pid}). "
                f"Poll with bash_output(bash_id='{bash_id}'), stop with kill_bash(bash_id='{bash_id}').")

    def _read_new(self, info: dict, which: str) -> str:
        key = f"offset_{which}"
        try:
            (info["ofh"] if which == "out" else info["efh"]).flush()
        except Exception:
            pass
        try:
            with open(info[which], "rb") as fh:
                fh.seek(info[key])
                data = fh.read()
                info[key] = fh.tell()
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def output(self, bash_id: str) -> str:
        info = self._procs.get(bash_id)
        if not info:
            return f"No background process with id {bash_id}"
        rc = info["proc"].poll()
        status = "running" if rc is None else f"exited(code={rc})"
        new_out = self._read_new(info, "out")
        new_err = self._read_new(info, "err")
        parts = [f"[bash_id={bash_id} status={status}]"]
        if new_out.strip():
            parts.append(new_out.rstrip())
        if new_err.strip():
            parts.append("[stderr]\n" + new_err.rstrip())
        if not new_out.strip() and not new_err.strip():
            parts.append("(no new output)")
        return "\n".join(parts)

    def kill(self, bash_id: str) -> str:
        info = self._procs.get(bash_id)
        if not info:
            return f"No background process with id {bash_id}"
        proc = info["proc"]
        if proc.poll() is not None:
            return f"bash_id={bash_id} already exited (code={proc.returncode})"
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return f"Killed bash_id={bash_id}"

    def cleanup(self) -> None:
        with self._lock:
            for info in self._procs.values():
                for h in ("ofh", "efh"):
                    try:
                        info[h].close()
                    except Exception:
                        pass
                for k in ("out", "err"):
                    try:
                        os.unlink(info[k])
                    except Exception:
                        pass


# --------------------------------------------------------------------------- tools

def run_command(ctx: ToolContext, command: str, cwd: Optional[str] = None,
                description: Optional[str] = None, timeout: Optional[int] = None,
                background: bool = False) -> str:
    workdir = str(ctx.resolve(cwd)) if cwd else str(ctx.cwd)
    desc = description or command

    if background:
        if ctx.bg_registry is None:
            return "Error: background execution is not available in this context."
        return ctx.bg_registry.start(command, workdir, desc)

    args, use_shell = _invocation(command)
    eff_timeout = timeout or TOOL_TIMEOUT
    try:
        res = subprocess.run(args, shell=use_shell, cwd=workdir, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=eff_timeout)
    except subprocess.TimeoutExpired:
        return f"Command timed out after {eff_timeout}s: {command}"
    except Exception as e:
        return f"Error running command: {e}"

    header = f"$ {command}" + (f"   # {desc}" if desc != command else "")
    parts = [header]
    if res.stdout and res.stdout.strip():
        parts.append(res.stdout.rstrip())
    if res.stderr and res.stderr.strip():
        parts.append("[stderr]\n" + res.stderr.rstrip())
    parts.append(f"[exit_code={res.returncode}]")
    return "\n".join(parts).strip()


def bash_output(ctx: ToolContext, bash_id: str) -> str:
    if ctx.bg_registry is None:
        return "Error: background process tracking is not available in this context."
    return ctx.bg_registry.output(bash_id)


def kill_bash(ctx: ToolContext, bash_id: str) -> str:
    if ctx.bg_registry is None:
        return "Error: background process tracking is not available in this context."
    return ctx.bg_registry.kill(bash_id)


register(ToolSpec(
    name="run_command",
    description="Run a real shell command and return its stdout/stderr and exit code; use for builds, tests, git, "
                "package managers, and other CLI work. SAFETY: this executes actual shell commands, so be careful "
                "with destructive ones (deletes, force pushes). Set background=true for long-running processes, then "
                "poll with bash_output and stop with kill_bash.",
    parameters={"type": "object", "properties": {
        "command": {"type": "string", "description": "Shell command to run, e.g. 'npm run build' or 'git status'"},
        "cwd": {"type": "string", "description": "Working directory to run in (default: agent cwd)"},
        "description": {"type": "string", "description": "Short description of what the command does"},
        "timeout": {"type": "integer", "description": "Timeout in seconds before the command is killed"},
        "background": {"type": "boolean", "default": False, "description": "Run detached for long-running processes; returns a bash_id to poll"},
    }, "required": ["command"]},
    impl=run_command, mutating=True, category="exec", aliases=("bash", "shell", "run_terminal_cmd"),
))

register(ToolSpec(
    name="bash_output",
    description="Fetch any new stdout/stderr from a background command started with run_command(background=true). "
                "Poll this to check progress or completion of a long-running process.",
    parameters={"type": "object", "properties": {"bash_id": {"type": "string", "description": "The bash_id returned when the background command was started"}}, "required": ["bash_id"]},
    impl=bash_output, category="read",
))

register(ToolSpec(
    name="kill_bash",
    description="Terminate a still-running background command started with run_command(background=true). "
                "SAFETY: this force-kills the process and any children; use when a process is stuck or no longer needed.",
    parameters={"type": "object", "properties": {"bash_id": {"type": "string", "description": "The bash_id of the background process to kill"}}, "required": ["bash_id"]},
    impl=kill_bash, mutating=True, category="exec",
))
