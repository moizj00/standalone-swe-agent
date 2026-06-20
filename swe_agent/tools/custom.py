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
from urllib.parse import parse_qsl, quote, urlsplit

from ._net import read_text_capped, safe_request, ssrf_check
from .base import REGISTRY, ToolContext, ToolSpec

# query-string keys that look like credentials worth redacting from echoed responses
_CRED_QS_KEYS = {"api_key", "apikey", "key", "token", "access_token", "secret",
                 "sig", "signature", "password", "pwd"}

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


def _param_props(defn: dict) -> dict:
    """Safely extract parameters.properties (a possibly-malicious payload may set
    parameters/properties to a non-object)."""
    p = defn.get("parameters")
    props = p.get("properties") if isinstance(p, dict) else None
    return props if isinstance(props, dict) else {}


def _param_required(defn: dict) -> List[str]:
    p = defn.get("parameters")
    req = p.get("required") if isinstance(p, dict) else None
    return [str(r) for r in req] if isinstance(req, list) else []


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

    params = defn.get("parameters")
    if params is not None and not isinstance(params, dict):
        errors.append(f"tool {name!r}: parameters must be an object")
    elif isinstance(params, dict) and params.get("properties") is not None \
            and not isinstance(params.get("properties"), dict):
        errors.append(f"tool {name!r}: parameters.properties must be an object")
    # every required name must be a declared property, else the model supplies it,
    # the required-gate passes, but _render drops it -> a broadened request.
    for r in _param_required(defn):
        if r not in _param_props(defn):
            errors.append(f"tool {name!r}: required param {r!r} is not declared in parameters.properties")

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
                # Placeholders are only safe in the path/query. A {host}/{port} in the
                # authority would let a model-supplied arg redirect the tool's auth
                # headers to an arbitrary origin. Use urlsplit (case-insensitive on the
                # scheme) so a mixed-case scheme like HTTPS:// can't slip past.
                split = urlsplit(url)
                if "{" in split.netloc:
                    errors.append(f"tool {name!r}: URL placeholders are only allowed in the path/query, not the host")
                # userinfo (user:pass@host) becomes a basic-auth header requests would
                # send and could echo back unredacted; require the auth field instead.
                try:
                    has_userinfo = bool(split.username or split.password)
                except ValueError:
                    has_userinfo = "@" in split.netloc
                if has_userinfo:
                    errors.append(f"tool {name!r}: URL must not embed credentials (user:pass@host); use the auth field")
                # Every {placeholder} must map to a declared parameter, or _render
                # filters the (undeclared) arg out and the literal {x} URL is called.
                declared = set(_param_props(defn))
                for ph in _PLACEHOLDER_RE.findall(url):
                    if ph not in declared:
                        errors.append(f"tool {name!r}: URL placeholder '{{{ph}}}' has no matching parameter")
            headers = http.get("headers")
            if headers is not None and not isinstance(headers, dict):
                errors.append(f"tool {name!r}: http.headers must be an object")
            # header names a header-located param must not clobber (configured + auth)
            reserved = {str(k).lower() for k in headers} if isinstance(headers, dict) else set()
            auth = http.get("auth")
            if isinstance(auth, dict):
                atype = str(auth.get("type", "")).lower()
                if atype == "bearer":
                    reserved.add("authorization")
                elif atype == "header" and auth.get("key"):
                    reserved.add(str(auth["key"]).lower())
            loc = http.get("param_location")
            if loc is not None and not isinstance(loc, dict):
                errors.append(f"tool {name!r}: http.param_location must be an object")
            if isinstance(loc, dict):
                for pname, where in loc.items():
                    # `where` is client-supplied; an unhashable value (list/dict) would
                    # raise on the set test, so type-check before membership.
                    if not isinstance(where, str) or where not in VALID_LOCATIONS:
                        errors.append(f"tool {name!r}: param '{pname}' has bad location {where!r}")
                        continue
                    if where == "path" and isinstance(url, str) and ("{" + str(pname) + "}") not in url:
                        # a path param with no matching placeholder would be silently
                        # dropped (e.g. delete_user(id) -> DELETE /users), so reject it.
                        errors.append(f"tool {name!r}: path param '{pname}' has no '{{{pname}}}' placeholder in the URL")
                    if where == "header" and str(pname).lower() in reserved:
                        errors.append(f"tool {name!r}: param '{pname}' (header) collides with a configured/auth header")
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
    locations = http.get("param_location")
    locations = locations if isinstance(locations, dict) else {}
    default_loc = "query" if method in ("GET", "DELETE") else "body"

    raw_headers = http.get("headers")
    headers: Dict[str, str] = ({str(k): str(v) for k, v in raw_headers.items()}
                               if isinstance(raw_headers, dict) else {})
    headers.update(_auth_headers(http.get("auth")))
    query: Dict[str, object] = {}
    body: Dict[str, object] = {}

    # Only forward arguments the operator actually declared — a hallucinated field
    # (e.g. admin=true / delete_all=true) must never become a real request param.
    declared = set(_param_props(defn))
    args = {k: v for k, v in args.items() if k in declared}

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
            if isinstance(v, str) and v:  # unambiguous credential: no length floor
                secrets.append(v)
    hdrs = http.get("headers")
    if isinstance(hdrs, dict):
        for v in hdrs.values():
            if isinstance(v, str) and len(v) >= 4:
                secrets.append(v)
    # credentials embedded directly in the URL query string (e.g. ?api_key=...)
    url = http.get("url")
    if isinstance(url, str):
        try:
            for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=False):
                if k.lower() in _CRED_QS_KEYS and isinstance(v, str) and len(v) >= 4:
                    secrets.append(v)
        except Exception:
            pass
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
    required = _param_required(defn)

    # NB: first param is `_ctx`, not `ctx` — _dispatch calls impl(self.ctx, **args)
    # positionally, so a custom tool with a declared `ctx` parameter must not
    # collide with it (it flows through **args instead).
    def impl(_ctx: ToolContext, **args) -> str:
        if not has_endpoint:
            return f"[custom tool '{name}' has no endpoint configured]"
        missing = [r for r in required if r not in args]
        if missing:
            # function calling isn't a strict validator; refuse rather than send a
            # broadened request (e.g. a missing id turning a targeted call wide).
            return f"Error calling '{name}': missing required argument(s): {', '.join(missing)}"
        try:
            url, query, headers, body = _render(defn, args)
            leftover = _PLACEHOLDER_RE.findall(url)
            if leftover:
                # an optional path param the model omitted would leave a literal {x}
                # in the URL — refuse rather than hit the wrong endpoint.
                return (f"Error calling '{name}': missing argument(s) for URL placeholder(s): "
                        + ", ".join("{" + p + "}" for p in leftover))
            r = safe_request(method, url, headers=headers, params=query or None, json=body)
        except ValueError as e:        # SSRF / redirect refusal — safe to show
            return f"Error calling '{name}': {e}"
        except Exception:              # transport error — do NOT echo headers/secrets
            return f"Error calling '{name}': request failed"
        # Redact BEFORE truncating, so the length cap can never sever a secret and
        # leave an un-redacted fragment (an endpoint that echoes our headers/auth
        # must not leak them, in whole or in part). read_text_capped bounds the
        # download so a huge body can't exhaust memory.
        text = _redact(read_text_capped(r), secrets)
        if len(text) > MAX_RESPONSE_CHARS:
            extra = len(text) - MAX_RESPONSE_CHARS
            text = text[:MAX_RESPONSE_CHARS] + f"\n... (truncated; {extra} more chars)"
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
