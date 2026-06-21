"""Minimal spec test for ``worker.apply_patch`` (hermetic; a tmp_path git repo).

No network access: the repo is built under pytest's ``tmp_path`` and cleaned up
automatically. Exhaustive behavior coverage lives in ``tests/test_patcher.py``;
this file pins the exact acceptance scenario from the task spec.
"""
from __future__ import annotations

from pathlib import Path

from git import Repo

from worker.apply_patch import apply_patch


def test_apply_patch_creates_branch_and_updates_file(tmp_path: Path):
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test User")
        cw.set_value("user", "email", "test@example.com")
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")

    result = apply_patch(
        str(tmp_path),
        [{"path": "src/feature.py", "content": "print('feature')\n"}],
    )

    assert "branch" in result and "commit" in result
    assert result["branch"] in {h.name for h in repo.heads}
    assert (tmp_path / "src" / "feature.py").read_text(encoding="utf-8") == "print('feature')\n"
