"""User-defined custom HTTP/REST tools.

A custom tool is *data* (sent from the dashboard per chat request), not a
code-registered ToolSpec. ``build_toolspec`` turns one definition into a ToolSpec
whose ``impl`` performs an SSRF-guarded HTTP request when the model calls it, so
the agent can call arbitrary external APIs the operator defined in the UI.

Payload shape (from web/src/store/toolSchemas.ts -> toCustomToolPayload):
  {
    "name": "get_weather",
    "description": "...",
    "parameters": {"type": "object", "properties": {...}, "required": [...]},
    "http": {
      "method": "GET",
      "url": "https://api.example.com/weather/{city}",
      "headers": {"X-Api-Key": "..."},
      "param_location": {"city": "path", "units": "query"},
      "auth": {"type": "bearer", "token": "..."}        # optional
    }
  }

SECURITY: every request goes through ._net.safe_request (scheme allowlist, blocks
internal/loopback/link-local, re-checks each redirect). Custom tools are
category="exec"/mutating, so Agent._gate blocks them in READ_ONLY and they run
only under an approving mode (which the server requires a token for). Secrets in
headers/auth are never echoed back in error text.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from ._net import safe_request, ssrf_check
from .base import REGISTRY, ToolContext, ToolSpec

NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
VALID_LOCATIONS = {"query", "path", "body", "header"}
MAX_TOOLS = 50
MAX_RESPONSE_CHARS = 4000
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _norm_type(t) -> str:
    """Accept Gemini UPPERCASE or JSON-schema lowercase; advertise lowercase."""
    return str(t).lower() if isinstance(t, str) else "string"


def _normalize_parameters(params) -> dict:
    """Coerce a parameters object into a lowercase JSON-schema the model expects."""
    if not isinstance(params, dict):
        return {"type": "object", "properties": {}}
    props = params.get("properties") or {}
    out_props = {}
    if isinstance(props, dict):
        for k, v in props.items():
            v = v if isinstance(v, dict) else {}
            out_props[k] = {"type": _norm_type(v.get("type", "string")),
                            "description": str(v.get("description", ""))}
    out = {"type": "object", "properties": out_props}
    req = params.get("required")
    if isinstance(req, list) and req:
        out["required"] = [str(x) for x in req]
    return out


def validate_def(defn: dict) -> List[str]:
    """Return human-readable validation errors for one custom-tool definition."""
    errors: List[str] = []
    if not isinstance(defn, dict):
        return ["custom tool must be an object"]

    name = defn.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"invalid tool name: {name!r}")
    elif name in REGISTRY:
        # A custom tool that reuses a built-in name would be shadowed: dispatch
        # resolves the global registry first, so the HTTP executor never runs.
        errors.append(f"tool name {name!r} shadows a built-in tool; choose another")
    if not isinstance(defn.get("description"), str) or not defn["description"].strip():
        errors.append(f"tool {name!r} needs a description")

    http = defn.get("http")
    if http is not None:
        if not isinstance(http, dict):
            errors.append(f"tool {name!r}: http must be an object")
        else:
            method = str(http.get("method", "GET")).upper()
            if method not in VALID_METHODS:
                errors.append(f"tool {name!r}: unsupported method {method}")
            url = http.get("url")
            if not isinstance(url, str) or not url.strip():
                errors.append(f"tool {name!r}: http.url is required")
            else:
                try:
                    reason = ssrf_check(_strip_placeholders(url))
                except ValueError:
                    reason = "malformed URL"
                if reason:
                    errors.append(f"tool {name!r}: {reason}")
            loc = http.get("param_location") or {}
            if isinstance(loc, dict):
                for pname, where in loc.items():
                    if where not in VALID_LOCATIONS:
                        errors.append(f"tool {name!r}: param '{pname}' has bad location '{where}'")
    return errors


def _strip_placeholders(url: str) -> str:
    """Replace {param} path placeholders with a dummy so the base URL can be
    SSRF-checked at validation time (host is what matters)."""
    return _PLACEHOLDER_RE.sub("x", url)


def _auth_headers(auth) -> Dict[str, str]:
    if not isinstance(auth, dict):
        return {}
    kind = str(auth.get("type", "")).lower()
    if kind == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    if kind == "header" and auth.get("key"):
        return {str(auth["key"]): str(auth.get("value", ""))}
    return {}


def _render(defn: dict, args: dict) -> Tuple[str, dict, dict, Optional[dict]]:
    """Map call args into (url, query, headers, json_body) per http.param_location."""
    http = defn.get("http") or {}
    method = str(http.get("method", "GET")).upper()
    url = str(http.get("url", ""))
    locations = http.get("param_location") or {}
    default_loc = "query" if method in ("GET", "DELETE") else "body"

    headers: Dict[str, str] = {str(k): str(v) for k, v in (http.get("headers") or {}).items()}
    headers.update(_auth_headers(http.get("auth")))
    query: Dict[str, object] = {}
    body: Dict[str, object] = {}

    # path params first (substitute {name}); the rest go to query/body/header
    path_params = {n for n, w in locations.items() if w == "path"}
    for pname in _PLACEHOLDER_RE.findall(url):
        path_params.add(pname)

    for pname, value in args.items():
        if pname in path_params:
            url = url.replace("{" + pname + "}", quote(str(value), safe=""))
            continue
        where = locations.get(pname, default_loc)
        if where == "header":
            headers[pname] = str(value)
        elif where == "body":
            body[pname] = value
        else:  # query
            query[pname] = value

    return url, query, headers, (body or None)


def _collect_secrets(http: dict) -> List[str]:
    """Configured credential strings (auth token/value + header values) to redact
    if the endpoint echoes them back in its response body."""
    secrets: List[str] = []
    auth = http.get("auth") or {}
    if isinstance(auth, dict):
        for k in ("token", "value"):
            v = auth.get(k)
            if isinstance(v, str) and len(v) >= 4:
                secrets.append(v)
    for v in (http.get("headers") or {}).values():
        if isinstance(v, str) and len(v) >= 4:
            secrets.append(v)
    return secrets


def _redact(text: str, secrets: List[str]) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, "***REDACTED***")
    return text


def build_toolspec(defn: dict) -> ToolSpec:
    """Turn a validated custom-tool definition into a runnable ToolSpec."""
    name = defn["name"]
    description = defn.get("description", "")
    parameters = _normalize_parameters(defn.get("parameters"))
    http = defn.get("http") or {}
    method = str(http.get("method", "GET")).upper()
    has_endpoint = bool(http.get("url"))
    secrets = _collect_secrets(http)

    def impl(ctx: ToolContext, **args) -> str:
        if not has_endpoint:
            return f"[custom tool '{name}' has no endpoint configured]"
        try:
            url, query, headers, body = _render(defn, args)
            r = safe_request(method, url, headers=headers, params=query or None, json=body)
        except ValueError as e:        # SSRF / redirect refusal — safe to show
            return f"Error calling '{name}': {e}"
        except Exception:              # transport error — do NOT echo headers/secrets
            return f"Error calling '{name}': request failed"
        text = r.text or ""
        if len(text) > MAX_RESPONSE_CHARS:
            text = text[:MAX_RESPONSE_CHARS] + f"\n... (truncated; {len(r.text) - MAX_RESPONSE_CHARS} more chars)"
        text = _redact(text, secrets)  # an endpoint that echoes our headers/auth must not leak them
        return f"[{name}] HTTP {r.status_code}\n{text}".strip()

    return ToolSpec(
        name=name, description=description, parameters=parameters, impl=impl,
        mutating=True, category="exec",
    )


def build_toolspecs(defs: List[dict]) -> Tuple[Dict[str, ToolSpec], List[str]]:
    """Validate + build a name->ToolSpec map. Returns (specs, errors)."""
    errors: List[str] = []
    if not isinstance(defs, list):
        return {}, ["custom_tools must be a list"]
    if len(defs) > MAX_TOOLS:
        return {}, [f"too many custom tools (max {MAX_TOOLS})"]
    specs: Dict[str, ToolSpec] = {}
    for defn in defs:
        errs = validate_def(defn)
        if errs:
            errors.extend(errs)
            continue
        if defn["name"] in specs:
            errors.append(f"duplicate custom tool name: {defn['name']}")
            continue
        specs[defn["name"]] = build_toolspec(defn)
    return specs, errors
