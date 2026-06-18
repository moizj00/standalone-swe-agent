"""Tool registry. Importing this package registers every tool and exposes the
advertised JSON schema list (canonical names only -- aliases resolve for dispatch
but are not advertised, which keeps the tool count down for small models).
"""
from __future__ import annotations

from .base import REGISTRY, ToolContext, ToolSpec, register, resolve_spec

# Importing each module triggers its register(...) calls.
from . import fs          # noqa: E402,F401
from . import search      # noqa: E402,F401
from . import exec        # noqa: E402,F401  (shadows builtin name within this module only)
from . import git         # noqa: E402,F401
from . import testing     # noqa: E402,F401
from . import planning    # noqa: E402,F401
from . import web         # noqa: E402,F401
from . import notebook    # noqa: E402,F401
from . import subagent    # noqa: E402,F401  (lazily imports agent inside its impl)


def _build_schema():
    seen = set()
    schemas = []
    for spec in list(REGISTRY.values()):
        if spec.name in seen:
            continue
        seen.add(spec.name)
        schemas.append(spec.schema())
    return schemas


TOOLS = _build_schema()
ADVERTISED = [s["function"]["name"] for s in TOOLS]
VALID_NAMES = set(REGISTRY.keys())

__all__ = ["TOOLS", "ADVERTISED", "VALID_NAMES", "REGISTRY",
           "ToolContext", "ToolSpec", "register", "resolve_spec"]
