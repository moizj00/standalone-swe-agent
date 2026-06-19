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

    Blocks malformed URLs, non-http(s) schemes, and any host that resolves to a
    loopback, link-local (incl. the 169.254.169.254 cloud-metadata IP), private,
    reserved, multicast, or unspecified address.
    """
    try:
        parsed = urlparse(url)
        scheme, host, port = parsed.scheme, parsed.hostname, parsed.port
    except ValueError:
        return "refused: malformed URL"
    if scheme not in ("http", "https"):
        return f"refused: scheme '{scheme}' not allowed (http/https only)"
    if not host:
        return "refused: no host in URL"
    try:
        infos = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80))
    except socket.gaierror:
        return None  # let the request fail naturally with a DNS error
    for info in infos:
        ip = info[4][0]
        if ip_is_blocked(ip):
            return f"refused: {host} resolves to internal address {ip} (SSRF guard)"
    return None


def _origin(url: str):
    """(scheme, host, port) for redirect same-origin checks; None if unparseable.

    The port is normalized to the scheme default so https://h and https://h:443
    (a canonicalizing proxy redirect) count as the SAME origin and don't trip the
    cross-origin header/body stripping.
    """
    try:
        p = urlparse(url)
        scheme = p.scheme
        port = p.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        return (scheme, p.hostname, port)
    except ValueError:
        return None


def safe_request(method: str, url: str, *, headers: Optional[dict] = None,
                 params: Optional[dict] = None, json: Optional[dict] = None,
                 timeout: int = DEFAULT_TIMEOUT, max_redirects: int = MAX_REDIRECTS):
    """requests.request with redirects followed MANUALLY so every hop is
    SSRF-checked (an allowed front door must not 30x-bounce into the internal
    range). Returns the final ``requests.Response``; raises ValueError on an SSRF
    refusal or too many redirects."""
    caller_headers = headers or {}
    base_origin = _origin(url)
    current = url
    body = json
    for _ in range(max_redirects + 1):
        reason = ssrf_check(current)
        if reason:
            raise ValueError(reason)
        # Only send caller-supplied headers (which may carry bearer tokens / API
        # keys) AND the request body (which may carry credentials/PII) on a
        # same-origin hop. A redirect to another host gets the default UA and no
        # body, so a 3xx can't exfiltrate the custom tool's secrets.
        same_origin = base_origin is not None and _origin(current) == base_origin
        req_headers = {**DEFAULT_HEADERS, **(caller_headers if same_origin else {})}
        r = requests.request(method.upper(), current, headers=req_headers, params=params,
                             json=(body if same_origin else None), timeout=timeout,
                             allow_redirects=False)
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
