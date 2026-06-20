"""notebook_edit: minimal Jupyter (.ipynb) cell editing."""
from __future__ import annotations

import json
from typing import Optional

from .base import ToolContext, ToolSpec, register


def notebook_edit(ctx: ToolContext, path: str, cell_index: int, new_source: str = "",
                  mode: str = "replace", cell_type: str = "code") -> str:
    p = ctx.resolve(path)
    if not p.exists():
        return f"Error: notebook not found: {path}"
    try:
        nb = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error parsing notebook JSON: {e}"

    cells = nb.setdefault("cells", [])
    # Jupyter stores source as a list of lines (keepends); normalize.
    src = new_source.splitlines(keepends=True)

    if mode == "insert":
        idx = max(0, min(int(cell_index), len(cells)))
        cell = {"cell_type": cell_type, "metadata": {}, "source": src}
        if cell_type == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        cells.insert(idx, cell)
        msg = f"Inserted {cell_type} cell at index {idx}"
    elif mode == "delete":
        if not (0 <= cell_index < len(cells)):
            return f"Error: cell_index {cell_index} out of range (0..{len(cells) - 1})"
        cells.pop(cell_index)
        msg = f"Deleted cell {cell_index}"
    else:  # replace
        if not (0 <= cell_index < len(cells)):
            return f"Error: cell_index {cell_index} out of range (0..{len(cells) - 1})"
        cells[cell_index]["source"] = src
        if cells[cell_index].get("cell_type") == "code":
            cells[cell_index]["outputs"] = []
            cells[cell_index]["execution_count"] = None
        msg = f"Replaced source of cell {cell_index}"

    try:
        p.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return f"Error writing notebook: {e}"
    return msg


register(ToolSpec(
    name="notebook_edit",
    description="Edit a Jupyter notebook (.ipynb): replace, insert, or delete a cell by index.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string"},
        "cell_index": {"type": "integer", "description": "0-based cell index"},
        "new_source": {"type": "string", "description": "New cell source (for replace/insert)"},
        "mode": {"type": "string", "enum": ["replace", "insert", "delete"], "default": "replace"},
        "cell_type": {"type": "string", "enum": ["code", "markdown"], "default": "code"},
    }, "required": ["path", "cell_index"]},
    impl=notebook_edit, mutating=True, category="write",
))
