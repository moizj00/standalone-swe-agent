"""Secret redaction: detect and mask likely credentials in text.

Uses a combination of known-format regex patterns (API keys, tokens, passwords)
and Shannon entropy analysis for unknown-format high-entropy strings.
"""
from __future__ import annotations

import math
import re
from typing import List, Tuple

REDACTED = "[REDACTED]"

# Known secret patterns (compiled once)
_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # OpenAI
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'), "openai_key"),
    # GitHub PAT
    (re.compile(r'\bghp_[A-Za-z0-9]{36,}\b'), "github_pat"),
    (re.compile(r'\bghs_[A-Za-z0-9]{36,}\b'), "github_secret"),
    (re.compile(r'\bgho_[A-Za-z0-9]{36,}\b'), "github_oauth"),
    (re.compile(r'\bghu_[A-Za-z0-9]{36,}\b'), "github_user_token"),
    (re.compile(r'\bghr_[A-Za-z0-9]{36,}\b'), "github_refresh"),
    # AWS
    (re.compile(r'\bAKIA[A-Z0-9]{16}\b'), "aws_access_key"),
    (re.compile(r'(?:aws_secret_access_key|secret_key)\s*[:=]\s*["\']?([A-Za-z0-9/+=]{40})', re.I), "aws_secret"),
    # Generic API key / token / password in assignments
    (re.compile(
        r'(?:api[_-]?key|api[_-]?secret|token|secret[_-]?key|password|passwd|auth[_-]?token)'
        r'\s*[:=]\s*["\']?([A-Za-z0-9_\-/+=.]{16,})["\']?',
        re.I,
    ), "generic_secret"),
    # Bearer tokens in headers
    (re.compile(r'Bearer\s+([A-Za-z0-9_\-./+=]{20,})', re.I), "bearer_token"),
    # Private keys
    (re.compile(r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----'), "private_key"),
    # Slack tokens
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9\-]+\b'), "slack_token"),
    # Stripe
    (re.compile(r'\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}\b'), "stripe_key"),
    # Heroku
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'), "uuid_maybe_secret"),
]

# Minimum entropy threshold for flagging unknown-format tokens
_ENTROPY_THRESHOLD = 4.2
_MIN_TOKEN_LEN = 20


def redact(text: str, *, check_entropy: bool = True) -> str:
    """Replace likely secrets in text with [REDACTED].

    Applies known-format patterns first, then optionally scans for
    high-entropy strings that look like secrets.
    """
    if not text:
        return text

    result = text
    for pattern, _label in _PATTERNS:
        # For patterns with capture groups, redact just the captured part
        if pattern.groups:
            result = pattern.sub(
                lambda m: m.group(0).replace(m.group(1), REDACTED) if m.group(1) else REDACTED,
                result,
            )
        else:
            result = pattern.sub(REDACTED, result)

    if check_entropy:
        result = _redact_high_entropy(result)

    return result


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq: dict = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _redact_high_entropy(text: str) -> str:
    """Find and redact high-entropy tokens that weren't caught by patterns."""
    # Look for long alphanumeric tokens that might be secrets
    token_re = re.compile(r'\b[A-Za-z0-9_\-/+=.]{20,}\b')
    for match in token_re.finditer(text):
        token = match.group(0)
        if len(token) < _MIN_TOKEN_LEN:
            continue
        # Skip common non-secret patterns (paths, URLs, known prefixes)
        if any(token.startswith(p) for p in ("http", "/", "./", "../", "node_modules")):
            continue
        if _shannon_entropy(token) >= _ENTROPY_THRESHOLD:
            text = text.replace(token, REDACTED, 1)
    return text


def contains_secret(text: str) -> bool:
    """Quick check: does the text appear to contain any secret?"""
    if not text:
        return False
    for pattern, _ in _PATTERNS:
        if pattern.search(text):
            return True
    return False
