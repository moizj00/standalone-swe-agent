"""Hermetic tests for the hybrid-agent launcher's argument forwarding.

No network, no real Ollama, no real Python agent are invoked. We copy the
``hybrid-agent`` script into a tmp dir alongside executable STUB scripts named
``cloud-agent``, ``ollama-agent``, and ``ensure-ollama.sh``. Each stub records
its own name plus the args it received into a capture file, so we can assert
exactly which downstream launcher ran and with which arguments.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HYBRID_SRC = REPO_ROOT / "hybrid-agent"

# A stub that appends "<name> <args...>" to capture.txt and exits 0.
CAPTURE_STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $@" >> "$(dirname "$0")/capture.txt"
exit 0
"""

# ensure-ollama.sh just exits 0 (no capture needed).
ENSURE_STUB = """#!/usr/bin/env bash
exit 0
"""


def _make_env(tmp_path: Path) -> Path:
    """Build a tmp dir with hybrid-agent + executable stubs; return its path."""
    shutil.copy(HYBRID_SRC, tmp_path / "hybrid-agent")
    os.chmod(tmp_path / "hybrid-agent", 0o755)

    for name in ("cloud-agent", "ollama-agent"):
        p = tmp_path / name
        p.write_text(CAPTURE_STUB)
        os.chmod(p, 0o755)

    ensure = tmp_path / "ensure-ollama.sh"
    ensure.write_text(ENSURE_STUB)
    os.chmod(ensure, 0o755)

    return tmp_path / "hybrid-agent"


def _capture_text(tmp_path: Path) -> str:
    cap = tmp_path / "capture.txt"
    return cap.read_text() if cap.exists() else ""


def test_local_flag_routes_to_ollama_without_local(tmp_path):
    """--local hands off to ollama-agent, consuming --local but forwarding the rest."""
    hybrid = _make_env(tmp_path)

    result = subprocess.run(
        [str(hybrid), "--local", "--dry-run", "explore project"],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr

    captured = _capture_text(tmp_path)
    assert "ollama-agent --dry-run explore project" in captured
    assert "cloud-agent" not in captured
    # --local must be consumed, not forwarded.
    assert "--local" not in captured


def test_cloud_path_forwards_and_skips_ollama_agent(tmp_path):
    """Cloud-first mode forwards unknown flags to cloud-agent and never runs ollama-agent."""
    hybrid = _make_env(tmp_path)

    result = subprocess.run(
        [str(hybrid), "--no-preflight", "--dry-run", "explore project"],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr

    captured = _capture_text(tmp_path)
    assert "cloud-agent --no-preflight --dry-run explore project" in captured
    # The local ollama-agent launcher must NOT be invoked on the cloud path.
    assert "ollama-agent" not in captured
