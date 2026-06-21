"""Tests for swe_agent.audit."""
import json
import tempfile
from pathlib import Path

from swe_agent.audit import AuditLog, Timer


def test_audit_log_records_entry():
    """Writes a valid JSONL entry to the audit log."""
    with tempfile.TemporaryDirectory() as td:
        log = AuditLog(Path(td), enabled=True)
        log.record(step=1, tool_name="read_file", args={"path": "foo.py"},
                   result="file contents here", duration_ms=42, approved=True)
        audit_path = Path(td) / ".agent" / "audit.log"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["step"] == 1
        assert entry["tool"] == "read_file"
        assert entry["duration_ms"] == 42
        assert entry["approved"] is True
        assert "ts" in entry


def test_audit_log_disabled():
    """No file created when disabled."""
    with tempfile.TemporaryDirectory() as td:
        log = AuditLog(Path(td), enabled=False)
        log.record(step=1, tool_name="x", args={}, result="", duration_ms=0)
        audit_path = Path(td) / ".agent" / "audit.log"
        assert not audit_path.exists()


def test_audit_log_truncates_long_args():
    """Long string values in args are truncated."""
    with tempfile.TemporaryDirectory() as td:
        log = AuditLog(Path(td), enabled=True)
        long_val = "x" * 1000
        log.record(step=1, tool_name="write", args={"content": long_val},
                   result="ok", duration_ms=5)
        audit_path = Path(td) / ".agent" / "audit.log"
        entry = json.loads(audit_path.read_text().strip())
        assert len(entry["args"]["content"]) < 600


def test_audit_blocked_reason():
    """Blocked tool calls include reason."""
    with tempfile.TemporaryDirectory() as td:
        log = AuditLog(Path(td), enabled=True)
        log.record(step=2, tool_name="run_command", args={"command": "rm -rf /"},
                   result="", duration_ms=0, approved=False,
                   blocked_reason="dangerous command")
        entry = json.loads((Path(td) / ".agent" / "audit.log").read_text().strip())
        assert entry["approved"] is False
        assert entry["blocked"] == "dangerous command"


def test_timer_context_manager():
    """Timer measures elapsed time in ms."""
    import time
    with Timer() as t:
        time.sleep(0.01)
    assert t.elapsed_ms >= 10
