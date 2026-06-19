"""Web tools: web_fetch (HTML -> text) and web_search (pluggable + scrape fallback).

HTML-to-text uses only the stdlib (html.parser) so there is no extra dependency.
web_search uses a real provider when WEBSEARCH_API_KEY + WEBSEARCH_PROVIDER are set
(tavily|brave), otherwise it scrapes DuckDuckGo's HTML endpoint (best-effort).
"""
from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from ..config import WEB_FETCH_MAX_CHARS
from .base import ToolContext, ToolSpec, register

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; swe-agent/2.0; +local)"}
_SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "header", "footer", "svg", "form"}
_BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            txt = data.strip()
            if txt:
                self.parts.append(txt + " ")

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n[ \t]*", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
    except Exception:
        pass
    return parser.text()


# --------------------------------------------------------------------------- SSRF guard

def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _ssrf_check(url: str) -> Optional[str]:
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
        if _ip_is_blocked(ip):
            return f"refused: {host} resolves to internal address {ip} (SSRF guard)"
    return None


def _safe_get(url: str, max_redirects: int = 5):
    """requests.get with redirects followed MANUALLY so every hop is SSRF-checked
    (an allowed front door must not 30x-bounce into the internal range)."""
    current = url
    for _ in range(max_redirects + 1):
        reason = _ssrf_check(current)
        if reason:
            raise ValueError(reason)
        r = requests.get(current, timeout=20, headers=_HEADERS, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            current = urljoin(current, r.headers["Location"])
            continue
        return r
    raise ValueError("too many redirects")


def web_fetch(ctx: ToolContext, url: str) -> str:
    try:
        r = _safe_get(url)
        r.raise_for_status()
    except Exception as e:
        return f"Error fetching {url}: {e}"
    ctype = r.headers.get("content-type", "")
    body = r.text
    text = html_to_text(body) if ("html" in ctype or body.lstrip()[:1] == "<") else body
    text = html.unescape(text)
    orig = len(text)
    if orig > WEB_FETCH_MAX_CHARS:
        text = text[:WEB_FETCH_MAX_CHARS] + f"\n... (truncated; {orig - WEB_FETCH_MAX_CHARS} more chars)"
    return f"Content from {url} ({r.status_code}):\n{text}"


# --------------------------------------------------------------------------- search

def _tavily(query: str, key: str, n: int) -> str:
    try:
        r = requests.post("https://api.tavily.com/search",
                          json={"api_key": key, "query": query, "max_results": n}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return f"Tavily search error: {e}"
    out = []
    if data.get("answer"):
        out.append(f"Answer: {data['answer']}")
    for item in data.get("results", [])[:n]:
        out.append(f"- {item.get('title', '')} — {item.get('url', '')}\n  {item.get('content', '')[:200]}")
    return "\n".join(out) or f"No results for: {query}"


def _brave(query: str, key: str, n: int) -> str:
    try:
        r = requests.get("https://api.search.brave.com/res/v1/web/search",
                         params={"q": query, "count": n},
                         headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return f"Brave search error: {e}"
    out = []
    for item in (data.get("web", {}) or {}).get("results", [])[:n]:
        out.append(f"- {item.get('title', '')} — {item.get('url', '')}\n  {item.get('description', '')[:200]}")
    return "\n".join(out) or f"No results for: {query}"


def _ddg_scrape(query: str, n: int) -> str:
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                          headers=_HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return (f"Web search error: {e}. (Tip: set WEBSEARCH_PROVIDER=tavily|brave and "
                f"WEBSEARCH_API_KEY for reliable results, or use run_command with curl.)")
    markup = r.text
    results = []
    # DuckDuckGo HTML result anchors: <a ... class="result__a" href="URL">TITLE</a>
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', markup, re.S):
        url = html.unescape(m.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if url and title:
            results.append(f"- {title} — {url}")
        if len(results) >= n:
            break
    if not results:
        return f"No results parsed for: {query}. Try web_fetch on a specific URL instead."
    return "\n".join(results)


def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    n = max(1, min(int(max_results or 5), 10))
    key = os.environ.get("WEBSEARCH_API_KEY")
    provider = os.environ.get("WEBSEARCH_PROVIDER", "").lower()
    if key and provider == "tavily":
        return _tavily(query, key, n)
    if key and provider == "brave":
        return _brave(query, key, n)
    return _ddg_scrape(query, n)


register(ToolSpec(
    name="web_fetch",
    description="Fetch a URL and return its content as readable text (HTML is converted to plain text).",
    parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    impl=web_fetch, category="read",
))

register(ToolSpec(
    name="web_search",
    description="Search the web and return top result titles + URLs. Uses a real provider if configured, "
                "else a best-effort scrape.",
    parameters={"type": "object", "properties": {
        "query": {"type": "string"},
        "max_results": {"type": "integer", "default": 5},
    }, "required": ["query"]},
    impl=web_search, category="read",
))
