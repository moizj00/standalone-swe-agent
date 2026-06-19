"""Security-hardening regression tests for the bridge.

Covers: workspace path confinement, web_fetch SSRF guard, server session-id
validation, and the secure-by-default start refusal.
"""
from __future__ import annotations

import pytest

from swe_agent.config import ApprovalMode
from swe_agent.server import ServerConfig, _safety_refusal, _SID_RE
from swe_agent.tools.base import ToolContext
from swe_agent.tools.web import _ip_is_blocked, _ssrf_check


# ---- path confinement ------------------------------------------------------

def test_confine_allows_in_workspace(tmp_path):
    ctx = ToolContext(cwd=tmp_path, confine=True)
    p = ctx.resolve("sub/file.txt")
    assert str(p).startswith(str(tmp_path.resolve()))


def test_confine_rejects_parent_traversal(tmp_path):
    ctx = ToolContext(cwd=tmp_path, confine=True)
    with pytest.raises(ValueError):
        ctx.resolve("../../etc/passwd")


def test_confine_rejects_absolute_escape(tmp_path):
    ctx = ToolContext(cwd=tmp_path, confine=True)
    bad = "C:\\Windows\\System32\\drivers\\etc\\hosts" if __import__("os").name == "nt" else "/etc/passwd"
    with pytest.raises(ValueError):
        ctx.resolve(bad)


def test_unconfined_allows_absolute(tmp_path):
    # CLI default: no confinement, absolute paths pass through.
    ctx = ToolContext(cwd=tmp_path, confine=False)
    assert ctx.resolve("/etc/passwd")  # no raise


# ---- SSRF guard ------------------------------------------------------------

@pytest.mark.parametrize("ip, blocked", [
    ("127.0.0.1", True),
    ("169.254.169.254", True),   # cloud metadata
    ("10.1.2.3", True),
    ("192.168.0.5", True),
    ("172.16.9.9", True),
    ("::1", True),
    ("8.8.8.8", False),
    ("93.184.216.34", False),
])
def test_ip_is_blocked(ip, blocked):
    assert _ip_is_blocked(ip) is blocked


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/secret",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "https://[::1]/",
])
def test_ssrf_blocks_internal(url):
    assert _ssrf_check(url) is not None


def test_ssrf_blocks_non_http_scheme():
    assert _ssrf_check("ftp://example.com/x") is not None
    assert _ssrf_check("file:///etc/passwd") is not None


# ---- session-id validation -------------------------------------------------

@pytest.mark.parametrize("sid, ok", [
    ("sess_abc123", True),
    ("20260619-120000", True),
    ("../etc/passwd", False),
    ("a/b", False),
    ("", False),
    ("x" * 65, False),
])
def test_session_id_regex(sid, ok):
    assert bool(_SID_RE.match(sid)) is ok


# ---- secure-by-default refusal --------------------------------------------

def test_refuses_mutating_without_token(tmp_path):
    cfg = ServerConfig(host="127.0.0.1", approval=ApprovalMode.AUTO_ACCEPT, token=None, cwd=tmp_path)
    assert _safety_refusal(cfg) is not None


def test_refuses_non_loopback_without_token(tmp_path):
    cfg = ServerConfig(host="0.0.0.0", approval=ApprovalMode.READ_ONLY, token=None, cwd=tmp_path)
    assert _safety_refusal(cfg) is not None


def test_allows_readonly_loopback_without_token(tmp_path):
    cfg = ServerConfig(host="127.0.0.1", approval=ApprovalMode.READ_ONLY, token=None, cwd=tmp_path)
    assert _safety_refusal(cfg) is None


def test_allows_mutating_with_token(tmp_path):
    cfg = ServerConfig(host="0.0.0.0", approval=ApprovalMode.YOLO, token="secret", cwd=tmp_path)
    assert _safety_refusal(cfg) is None
