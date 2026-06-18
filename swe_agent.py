#!/usr/bin/env python3
"""Thin launcher shim for the standalone Ollama SWE coding agent.

The real implementation lives in the ``swe_agent/`` package. This file is kept so
that ``python swe_agent.py "task"`` and the ``ollama-agent`` launcher keep working
unchanged. When run by path, this file executes as ``__main__``; importing the
``swe_agent`` name resolves to the package (a regular package with __init__.py takes
precedence over a same-named module), so there is no clash.

Usage:
  python swe_agent.py "Add type hints to all functions in src/"
  python swe_agent.py --model qwen2.5-coder:7b --plan "Refactor the config loader"
  python swe_agent.py            # interactive mode
"""
import sys
from pathlib import Path

# Force UTF-8 output so glyphs/ANSI don't crash on Windows' cp1252 console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Make this directory importable when the file is run directly by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swe_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
