"""Hermetic checks for the hybrid-agent bash launcher.

These tests avoid network access, Ollama, and any real cloud calls. They only
verify the launcher's static shape and that bash can parse it. When bash is not
available (e.g. a bare Windows host without git-bash), the whole module is
skipped so the suite stays portable.
"""

import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO_ROOT, "hybrid-agent")

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


def _read_launcher() -> str:
    with open(LAUNCHER, "r", encoding="utf-8") as handle:
        return handle.read()


def test_launcher_exists():
    assert os.path.isfile(LAUNCHER), "hybrid-agent launcher should exist in repo root"


def test_bash_syntax_ok():
    result = subprocess.run(
        [BASH, "-n", LAUNCHER],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_strict_mode_enabled():
    assert "set -euo pipefail" in _read_launcher()


def test_resolves_repo_dir_like_siblings():
    content = _read_launcher()
    assert "readlink -f" in content
    assert "BASH_SOURCE" in content
    assert "dirname" in content


def test_has_local_flag_handling():
    content = _read_launcher()
    assert "--local" in content
    # --local should defer to the local Ollama launcher.
    assert "ollama-agent" in content


def test_references_ensure_ollama_helper():
    assert "ensure-ollama.sh" in _read_launcher()


def test_backgrounds_warmup_best_effort():
    content = _read_launcher()
    # Warmup must be detached (&) and guarded so it cannot kill the script.
    assert "&" in content
    assert "|| true" in content


def test_writes_warmup_log_under_tmp():
    assert "/tmp" in _read_launcher()


def test_execs_cloud_agent_in_default_mode():
    content = _read_launcher()
    assert "cloud-agent" in content
    assert "exec" in content


def test_forwards_all_arguments():
    # Both exec paths should forward the remaining args verbatim.
    assert '"$@"' in _read_launcher()
