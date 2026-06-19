"""Shared outbound-HTTP helpers with an SSRF guard.

Both web_fetch (tools/web.py) and the custom HTTP tool executor (tools/custom.py)
make requests to URLs that may be model- or user-influenced, so every outbound
call goes through here: scheme allowlist, block hosts resolving to internal
ranges, and re-validate on every redirect hop.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; swe-agent/2.0; +local)"}
DEFAULT_TIMEOUT = 20
MAX_REDIRECTS = 5


def ip_is_blocked(ip: str) -> bool:
    """True if ip is loopback/link-local/private/reserved/multicast/unspecified."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def ssrf_check(url: str) -> Optional[str]:
    """Return a reason string if ``url`` must NOT be fetched (SSRF), else None.

    Blocks non-http(s) schemes and any host that resolves to a loopback,
    link-local (incl. the 169.254.169.254 cloud-metadata IP), private, reserved,
    multicast, or unspecified address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"refused: scheme '{parsed.scheme}' not allowed (http/https only)"
    host = parsed.hostname
    if not host:
        return "refused: no host in URL"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return None  # let the request fail naturally with a DNS error
    for info in infos:
        ip = info[4][0]
        if ip_is_blocked(ip):
            return f"refused: {host} resolves to internal address {ip} (SSRF guard)"
    return None


def safe_request(method: str, url: str, *, headers: Optional[dict] = None,
                 params: Optional[dict] = None, json: Optional[dict] = None,
                 timeout: int = DEFAULT_TIMEOUT, max_redirects: int = MAX_REDIRECTS):
    """requests.request with redirects followed MANUALLY so every hop is
    SSRF-checked (an allowed front door must not 30x-bounce into the internal
    range). Returns the final ``requests.Response``; raises ValueError on an SSRF
    refusal or too many redirects."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    current = url
    body = json
    for _ in range(max_redirects + 1):
        reason = ssrf_check(current)
        if reason:
            raise ValueError(reason)
        r = requests.request(method.upper(), current, headers=merged, params=params,
                             json=body, timeout=timeout, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            current = urljoin(current, r.headers["Location"])
            params = None  # query already encoded into the original; don't re-append
            if r.status_code == 303:
                method, body = "GET", None
            continue
        return r
    raise ValueError("too many redirects")


def safe_get(url: str):
    """Convenience GET used by web_fetch."""
    return safe_request("GET", url)
