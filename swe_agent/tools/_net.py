"""Shared outbound-HTTP helpers with an SSRF guard.

Both web_fetch (tools/web.py) and the custom HTTP tool executor (tools/custom.py)
make requests to URLs that may be model- or user-influenced, so every outbound
call goes through here: scheme allowlist, block hosts resolving to internal
ranges, and re-validate on every redirect hop.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; swe-agent/2.0; +local)"}
DEFAULT_TIMEOUT = 20
MAX_REDIRECTS = 5
MAX_DOWNLOAD_BYTES = 2_000_000  # stop reading a response body past this (memory DoS guard)

# trust_env=False ignores HTTP(S)_PROXY/NO_PROXY/.netrc: a proxy would do its OWN
# DNS resolution and connect, bypassing our address pin (and re-opening the SSRF
# rebinding hole). All guarded fetches go direct.
_SESSION = requests.Session()
_SESSION.trust_env = False


def read_text_capped(r, max_bytes: int = MAX_DOWNLOAD_BYTES) -> str:
    """Read at most ``max_bytes`` of a (streamed) response body so a huge/malicious
    body can't exhaust memory before truncation. Falls back to ``.text`` for
    non-streaming test doubles."""
    if not hasattr(r, "iter_content"):
        return getattr(r, "text", "") or ""
    chunks, total = [], 0
    try:
        for chunk in r.iter_content(8192):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
    except Exception:
        pass
    finally:
        try:
            r.close()
        except Exception:
            pass
    enc = getattr(r, "encoding", None) or "utf-8"
    return b"".join(chunks).decode(enc, errors="replace")

# --- DNS pinning (anti-rebinding) -------------------------------------------
# The SSRF check resolves the host, but `requests`/urllib3 would resolve it AGAIN
# at connect time — a TOCTOU window a DNS-rebinding host can exploit (public IP to
# the check, internal IP to the connect). We close it by pinning: resolve+validate
# once, then make the connect use the SAME validated IPs. A single global
# getaddrinfo wrapper consults a THREAD-LOCAL pin map (the server is multi-threaded,
# so a global patch must be thread-safe) and only overrides resolution for the host
# we pinned on this thread; everything else delegates to the real resolver. SNI and
# certificate validation still use the original hostname.
_real_getaddrinfo = socket.getaddrinfo
_pin = threading.local()


def _pinning_getaddrinfo(host, port, *args, **kwargs):
    pins = getattr(_pin, "map", None)
    if pins and host in pins:
        out = []
        for ip in pins[host]:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
            out.append((fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        if out:
            return out
    return _real_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _pinning_getaddrinfo  # installed once; inert unless _pin.map is set


def ip_is_blocked(ip: str) -> bool:
    """True if ip is not safe to fetch (any non-globally-routable address)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # Anything not globally routable is an SSRF target. is_global also catches the
    # 100.64.0.0/10 CGNAT/shared range, which is neither is_private nor is_reserved.
    if not getattr(addr, "is_global", True):
        return True
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


def _resolve_validated(url: str) -> Tuple[str, List[str]]:
    """Resolve ``url``'s host and return (host, [validated_ips]) for connection
    pinning, or raise ValueError(reason) if it is unsafe to fetch. Unlike
    ssrf_check (which returns a reason for config-time validation), this is the
    request-time gate and never silently passes a bad host."""
    try:
        parsed = urlparse(url)
        scheme, host, port = parsed.scheme, parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("refused: malformed URL")
    if scheme not in ("http", "https"):
        raise ValueError(f"refused: scheme '{scheme}' not allowed (http/https only)")
    if not host:
        raise ValueError("refused: no host in URL")
    try:
        infos = _real_getaddrinfo(host, port or (443 if scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"refused: cannot resolve host {host}")
    ips: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip_is_blocked(ip):
            raise ValueError(f"refused: {host} resolves to internal address {ip} (SSRF guard)")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise ValueError(f"refused: no addresses for host {host}")
    return host, ips


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
    try:
        for _ in range(max_redirects + 1):
            # Resolve+validate, then pin the connect to the SAME IPs (no second,
            # independent DNS lookup the rebinding window could exploit).
            host, ips = _resolve_validated(current)
            _pin.map = {host: ips}
            # Only send caller-supplied headers (which may carry bearer tokens /
            # API keys), query params, and body on a same-origin hop. A redirect to
            # another origin gets the default UA and none of them — no exfiltration.
            same_origin = base_origin is not None and _origin(current) == base_origin
            req_headers = {**DEFAULT_HEADERS, **(caller_headers if same_origin else {})}
            # stream=True so the body isn't buffered until the caller reads it via
            # read_text_capped; _SESSION (trust_env=False) keeps proxies out of the loop.
            r = _SESSION.request(method.upper(), current, headers=req_headers,
                                 params=(params if same_origin else None),
                                 json=(body if same_origin else None),
                                 timeout=timeout, allow_redirects=False, stream=True)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                r.close()  # drop the unread redirect body before the next hop
                current = urljoin(current, r.headers["Location"])
                # 301/302/303 convert a non-GET to GET (POST-redirect-GET), matching
                # normal client semantics; 307/308 preserve method + body.
                if r.status_code in (301, 302, 303):
                    method, body = "GET", None
                continue
            return r
        raise ValueError("too many redirects")
    finally:
        _pin.map = {}


def safe_get(url: str):
    """Convenience GET used by web_fetch."""
    return safe_request("GET", url)
