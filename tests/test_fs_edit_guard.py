"""Regression tests for the edit/multi_edit empty-old_string guard.

An empty ``old_string`` is never a valid anchor: ``str.count("")`` returns
len+1 and ``str.replace("", x)`` splices ``x`` between every character, so a
single bad call from a small model corrupts the whole file. The tools must
refuse it and leave the file byte-for-byte untouched.
"""
from __future__ import annotations

from swe_agent.tools.fs import edit, multi_edit

ORIGINAL = "def add(a, b):\n    return a + b\n"


def test_edit_rejects_empty_old_string_and_leaves_file_untouched(ctx):
    f = ctx.cwd / "calc.py"
    f.write_text(ORIGINAL, encoding="utf-8")

    result = edit(ctx, "calc.py", "", '"""doc"""', replace_all=True)

    assert result.lower().startswith("error")
    assert "old_string" in result
    assert f.read_text(encoding="utf-8") == ORIGINAL


def test_multi_edit_rejects_empty_old_string_and_writes_nothing(ctx):
    f = ctx.cwd / "calc.py"
    f.write_text(ORIGINAL, encoding="utf-8")

    result = multi_edit(ctx, "calc.py", [{"old_string": "", "new_string": "X"}])

    assert result.lower().startswith("error")
    assert f.read_text(encoding="utf-8") == ORIGINAL
