"""A minimal HTTP/SSE bridge that lets a web frontend drive the SWE agent.

This is the server the agent never had. It is intentionally stdlib-only (no
FastAPI/uvicorn) so it runs anywhere the agent does -- including very new Python
where compiled web frameworks may lack wheels.

Endpoints
---------
  GET  /api/health             -> liveness + config summary
  GET  /api/tools              -> the REAL tool registry, serialized in the
                                  Gemini/AI-Studio UPPERCASE schema shape so the
                                  dashboard's Tool Schema tab can display it
  POST /api/chat               -> non-streaming; returns {text, session_id}
                                  (drop-in for the dashboard's existing contract)
  POST /api/chat/stream        -> Server-Sent Events: token / assistant / step /
                                  tool_call / tool_result / final / error events

Design notes
------------
* One live ``Agent`` is kept per ``session_id`` in an in-process registry, each
  guarded by its own lock -- a second concurrent request for the same session
  gets 409 rather than corrupting ``self.messages``.
* The dashboard speaks the Gemini ``Content[]`` shape ({role, parts:[{text}]});
  ``translate_messages`` flattens that (and the plain {role, content} shape) and
  maps role ``model`` -> ``assistant``.
* Approval defaults to READ_ONLY: mutations are blocked by ``Agent._gate`` before
  any (impossible, over HTTP) interactive prompt. AUTO_ACCEPT/YOLO are opt-in.
* SSE events are written directly from the handler thread via ``Agent.event_cb``
  (each connection already runs on its own thread), so no worker/queue is needed.

SECURITY: these tools run real shell commands and write real files with no path
containment. Bind to 127.0.0.1 (the default) and set SWE_AGENT_SERVER_TOKEN to
require a bearer token. Do NOT expose this on 0.0.0.0 without an isolation layer.
"""
from __future__ import annotations

import argparse
import copy
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import llm, prompts
from .agent import Agent
from .config import (ApprovalMode, DEFAULT_MODEL, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_BASE,
                     DEFAULT_TEMPERATURE, MAX_STEPS, SESSION_DIR)
from .session import Session, build_env_context, load_project_instructions
from .tools import ADVERTISED, TOOLS, VALID_NAMES
from .tools.base import ToolContext
from .tools.custom import build_toolspecs
from .tools.exec import BackgroundRegistry

MAX_BODY = 16 * 1024 * 1024          # reject request bodies larger than this (413)
SSE_SEND_TIMEOUT = 120               # seconds; a stalled SSE client surfaces as a socket error
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # client-supplied session ids must match this
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class _HttpError(Exception):
    """Carry an HTTP status + message out of a helper to the request handler."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- config

@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_OLLAMA_BASE
    num_ctx: int = DEFAULT_NUM_CTX
    temperature: float = DEFAULT_TEMPERATURE
    max_steps: int = MAX_STEPS
    cwd: Path = field(default_factory=Path.cwd)
    approval: ApprovalMode = ApprovalMode.READ_ONLY
    token: Optional[str] = None
    persist: bool = True
    # Path to a built React/SPA bundle (web/dist) the server should serve for
    # non-/api/* GETs. None disables static serving (the local-dev default; the
    # Node proxy in web/server.ts handles it instead).
    static_dir: Optional[Path] = None
    # Injectable for tests: build an Agent for a session id. Defaults to a real,
    # Ollama-backed agent built from this config.
    agent_factory: Optional[Callable[["ServerConfig", str], Agent]] = None


# --------------------------------------------------------------------------- message translation

def _msg_text(msg: dict) -> str:
    """Extract plain text from either a Gemini Content ({parts:[{text}]}) or a
    plain {content} message."""
    if isinstance(msg.get("content"), str):
        return msg["content"]
    parts = msg.get("parts") or []
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "".join(out)


def translate_messages(raw: List[dict]) -> List[Tuple[str, str]]:
    """Return [(role, text)] with Gemini's 'model' role mapped to 'assistant'.

    Empty-text messages and unknown roles are dropped. System messages are kept
    as-is (the agent already carries its own system prompt, so callers normally
    omit them, but we tolerate them).
    """
    out: List[Tuple[str, str]] = []
    for m in raw or []:
        role = (m.get("role") or "user").lower()
        if role == "model":
            role = "assistant"
        if role not in ("user", "assistant", "system"):
            role = "user"
        text = _msg_text(m).strip()
        if text:
            out.append((role, text))
    return out


# --------------------------------------------------------------------------- tool schema export

_TYPE_MAP = {"object": "OBJECT", "array": "ARRAY", "string": "STRING",
             "integer": "INTEGER", "number": "NUMBER", "boolean": "BOOLEAN", "null": "NULL"}


def _uppercase_types(node):
    """Recursively map JSON-schema lowercase types to Gemini UPPERCASE in a copy."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = _TYPE_MAP.get(v, v.upper())
            else:
                out[k] = _uppercase_types(v)
        return out
    if isinstance(node, list):
        return [_uppercase_types(x) for x in node]
    return node


def gemini_tool_declarations() -> List[dict]:
    """The advertised tool schemas as Gemini functionDeclarations (UPPERCASE types)."""
    decls = []
    for t in TOOLS:
        fn = t.get("function", {})
        decls.append({
            "name": fn.get("name"),
            "description": fn.get("description"),
            "parameters": _uppercase_types(copy.deepcopy(fn.get("parameters", {}))),
        })
    return decls


# --------------------------------------------------------------------------- agent factory + registry

def _server_approval_cb(name: str, args: dict, reason: str) -> bool:
    """Non-interactive approval for HTTP: allow unless flagged dangerous.

    Mutations are already blocked upstream by Agent._gate in READ_ONLY mode; in
    AUTO_ACCEPT/YOLO this lets non-dangerous exec/edits through but still refuses
    a command the danger detector flagged (rm -rf, force-push, fork bomb, ...).
    """
    return not bool(reason)


def default_agent_factory(config: ServerConfig, session_id: str) -> Agent:
    cwd = Path(config.cwd).resolve()
    env = build_env_context(cwd)
    proj, _ = load_project_instructions(cwd)
    system = prompts.build_system_prompt(
        env_context=env, project_instructions=proj,
        plan_mode=(config.approval == ApprovalMode.READ_ONLY),
    )
    ctx = ToolContext(
        cwd=cwd, approval=config.approval, approve_cb=_server_approval_cb,
        bg_registry=BackgroundRegistry(), model=config.model, base_url=config.base_url,
        num_ctx=config.num_ctx, temperature=config.temperature,
        confine=True,  # a network-driven agent must not read/write outside the workspace
    )
    return Agent(
        model=config.model, ctx=ctx, system_prompt=system, stream=True,
        verbose=False, max_steps=config.max_steps, base_url=config.base_url,
        num_ctx=config.num_ctx, temperature=config.temperature,
    )


class AgentRegistry:
    """Keyed store of live Agents, one lock per session."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self._factory = config.agent_factory or default_agent_factory
        self._entries: Dict[str, dict] = {}
        self._guard = threading.Lock()

    def get_or_create(self, session_id: Optional[str]) -> Tuple[str, dict, bool]:
        """Return (session_id, entry, created). entry = {agent, lock, session}.

        The heavy work (Session FS setup + agent construction, which shells out to
        git) is done OUTSIDE the registry lock so a slow build can't serialize
        every other session's lookups; the lock only guards the dict check/insert.
        """
        # Fast path: a live entry already exists.
        with self._guard:
            if session_id and session_id in self._entries:
                return session_id, self._entries[session_id], False

        # Build a candidate without holding the lock. When a session_id is given,
        # key the on-disk Session to THAT id so saves land in <id>.jsonl and
        # Session.load(id) can resume it later (id is pre-validated by the handler).
        if self.config.persist:
            session = (Session(session_id, SESSION_DIR / f"{session_id}.jsonl")
                       if session_id else Session.create())
            sid = session.sid
        else:
            session = None
            sid = session_id or _rand_id()
        agent = self._factory(self.config, sid)
        entry = {"agent": agent, "lock": threading.Lock(), "session": session}

        # Re-check under the lock: another thread may have created the same id.
        with self._guard:
            if sid in self._entries:
                try:
                    agent.ctx.bg_registry.cleanup()
                except Exception:
                    pass
                return sid, self._entries[sid], False
            self._entries[sid] = entry
            return sid, entry, True

    def cleanup(self) -> None:
        with self._guard:
            for entry in self._entries.values():
                try:
                    entry["agent"].ctx.bg_registry.cleanup()
                except Exception:
                    pass
            self._entries.clear()


def _rand_id() -> str:
    import uuid
    return "sess_" + uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- turn driving

class _ClientGone(Exception):
    """Raised inside an event callback when the SSE client has disconnected."""


def _prime_agent(entry: dict, messages: List[Tuple[str, str]], created: bool) -> str:
    """Append the incoming turn to the agent's history; return the new user text.

    For a brand-new session we replay any prior turns the client sent (everything
    but the final user message). For an existing session we trust the server-side
    history and append only the final user message. Raises ValueError if the last
    message isn't a user turn.
    """
    if not messages:
        raise ValueError("no messages provided")
    role, text = messages[-1]
    if role != "user":
        raise ValueError("the last message must be a user message")
    agent: Agent = entry["agent"]
    if created and len(messages) > 1:
        for r, t in messages[:-1]:
            if r == "system":
                continue
            agent.messages.append({"role": r, "content": t})
    agent.add_user(text)
    return text


def _persist(entry: dict) -> None:
    session = entry.get("session")
    if session is None:
        return
    try:
        session.save(entry["agent"].messages, meta={"model": entry["agent"].model})
    except Exception:
        pass


# --------------------------------------------------------------------------- HTTP server

class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, config: ServerConfig):
        super().__init__(addr, handler)
        self.config = config
        self.registry = AgentRegistry(config)


class Handler(BaseHTTPRequestHandler):
    server_version = "SWEAgent/1.0"

    # ---- low-level helpers ------------------------------------------------

    @property
    def config(self) -> ServerConfig:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def registry(self) -> AgentRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    def log_message(self, *_args):
        pass  # quiet by default

    def _authorized(self) -> bool:
        token = self.config.token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        # constant-time compares so a wrong token can't be discovered by timing
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True
        return hmac.compare_digest(self.headers.get("X-Agent-Token", "").strip(), token)

    def _read_json(self) -> dict:
        """Parse the JSON body. Raises _HttpError(400/413) on a malformed or
        oversized request so the caller can return the right status."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise _HttpError(400, "invalid Content-Length")
        if length < 0:
            raise _HttpError(400, "invalid Content-Length")
        if length > MAX_BODY:
            raise _HttpError(413, f"request body too large (> {MAX_BODY} bytes)")
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise _HttpError(400, "invalid JSON body")

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        # One SSE stream per connection, then close: with no Content-Length the
        # client detects end-of-stream by EOF, so we must NOT keep the socket
        # alive or iter_lines() would hang waiting for a (never-sent) next byte.
        self.close_connection = True
        # A client that stops reading must not pin the handler thread (and the
        # per-session lock) forever: a write that blocks past this timeout raises
        # a socket error, which _write_sse turns into _ClientGone.
        try:
            self.connection.settimeout(SSE_SEND_TIMEOUT)
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
        self.end_headers()

    def _write_sse(self, event: dict) -> None:
        try:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ClientGone()

    # ---- routing ----------------------------------------------------------

    def do_GET(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/health":
            return self._send_json({
                "status": "ok", "model": self.config.model,
                "approval": self.config.approval.value, "tools": len(ADVERTISED),
                "cwd": str(Path(self.config.cwd).resolve()),
            })
        if path == "/api/tools":
            # `reserved` is the full set of names (incl. aliases like bash/cat/list_dir)
            # the builder must refuse so a custom tool can't shadow one and 400 the chat.
            return self._send_json({"tools": gemini_tool_declarations(),
                                    "reserved": sorted(VALID_NAMES)})
        # Non-/api/* GETs fall back to the SPA bundle when one is configured (Cloud
        # Run / single-container layout). The check is local — no static_dir set,
        # no static serving, original 404 behavior is preserved for local dev.
        if self.config.static_dir and not path.startswith("/api/"):
            served = self._try_serve_static(path)
            if served:
                return
        return self._send_json({"error": f"not found: {path}"}, 404)

    # ---- static (SPA) -----------------------------------------------------

    def _try_serve_static(self, path: str) -> bool:
        """Serve a file from ``static_dir``; SPA-fallback to index.html for unknown paths."""
        root = self.config.static_dir
        if not root or not root.is_dir():
            return False
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        # Reject path traversal: target must remain inside the static root.
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return False
        if not target.is_file():
            # SPA fallback: any unknown route resolves to index.html so client-side
            # routing works (deep links, dashboard tabs).
            target = (root / "index.html").resolve()
            if not target.is_file():
                return False
        try:
            body = target.read_bytes()
        except OSError:
            return False
        mime, _ = mimetypes.guess_type(target.name)
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return True

    def do_POST(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/chat":
            return self._handle_chat(stream=False)
        if path == "/api/chat/stream":
            return self._handle_chat(stream=True)
        return self._send_json({"error": f"not found: {path}"}, 404)

    # ---- chat -------------------------------------------------------------

    def _handle_chat(self, stream: bool):
        try:
            data = self._read_json()
        except _HttpError as e:
            return self._send_json({"error": e.message}, e.status)

        session_id = data.get("session_id")
        if session_id is not None and not (isinstance(session_id, str) and _SID_RE.match(session_id)):
            return self._send_json({"error": "invalid session_id"}, 400)
        try:
            messages = translate_messages(data.get("messages") or [])
        except Exception as e:
            return self._send_json({"error": f"bad messages: {e}"}, 400)

        # Validate custom tools before touching session state. Omitted key = no
        # change; an explicit (possibly empty) list = replace this session's set.
        raw_custom = data.get("custom_tools")
        custom_specs = {}
        if raw_custom is not None:
            try:
                custom_specs, cerrors = build_toolspecs(raw_custom)
            except Exception as e:
                # backstop: a validator bug on hostile input must degrade to a clean
                # 400, never an uncaught exception that drops the connection.
                print(f"[server] custom_tools validation error: {e}", file=sys.stderr)
                return self._send_json({"error": "invalid custom_tools"}, 400)
            if cerrors:
                return self._send_json(
                    {"error": "invalid custom_tools: " + "; ".join(cerrors[:5])}, 400)

        sid, entry, created = self.registry.get_or_create(session_id)

        lock: threading.Lock = entry["lock"]
        if not lock.acquire(blocking=False):
            # Do NOT mutate the shared entry on the busy path.
            return self._send_json({"error": "session busy", "session_id": sid}, 409)
        try:
            # Model override is applied only inside the lock (never on the 409 path),
            # so it can't switch models out from under another in-flight turn. It is
            # sticky for the session (and recorded in persisted meta) by design.
            if data.get("model"):
                entry["agent"].model = data["model"]
            if raw_custom is not None:
                entry["agent"].extra_tools = custom_specs
            try:
                _prime_agent(entry, messages, created)
            except ValueError as e:
                return self._send_json({"error": str(e), "session_id": sid}, 400)
            if stream:
                return self._run_stream(entry, sid)
            return self._run_blocking(entry, sid)
        finally:
            lock.release()

    def _run_blocking(self, entry: dict, sid: str):
        agent: Agent = entry["agent"]
        agent.event_cb = None
        try:
            text = agent.run_turn()
        except Exception as e:
            print(f"[server] turn error (session {sid}): {e}", file=sys.stderr)
            return self._send_json({"error": "internal agent error", "session_id": sid}, 500)
        finally:
            _persist(entry)
        return self._send_json({"text": text, "session_id": sid})

    def _run_stream(self, entry: dict, sid: str):
        agent: Agent = entry["agent"]
        self._send_sse_headers()
        self._write_sse({"type": "session", "session_id": sid})
        agent.event_cb = self._write_sse
        try:
            agent.run_turn()
        except _ClientGone:
            return  # client hung up mid-turn; stop quietly
        except Exception as e:
            try:
                self._write_sse({"type": "error", "message": str(e)})
            except _ClientGone:
                pass
        finally:
            agent.event_cb = None
            _persist(entry)


# --------------------------------------------------------------------------- entrypoint

def build_server(config: ServerConfig) -> AgentServer:
    return AgentServer((config.host, config.port), Handler, config=config)


def _safety_refusal(config: ServerConfig) -> Optional[str]:
    """Return a reason to refuse start for an unsafe secure-by-default posture.

    ``SWE_AGENT_TRUST_NETWORK=1`` is a positive assertion that an outer layer
    (Cloud Run IAM, IAP, a sidecar reverse proxy) has authenticated the caller,
    so the non-loopback-needs-token rule is allowed to relax. The YOLO /
    auto-accept rule is NOT relaxed: mutating modes still require a token because
    a misconfigured outer layer would let the agent run arbitrary shell.
    """
    loopback = config.host in _LOOPBACK
    trust_network = os.environ.get("SWE_AGENT_TRUST_NETWORK") == "1"
    if not config.token:
        if config.approval != ApprovalMode.READ_ONLY:
            return (f"refusing to start: approval={config.approval.value} (allows mutations/"
                    f"shell) with no --token. Set a token or use --approval read-only.")
        if not loopback and not trust_network:
            return (f"refusing to start: bound to non-loopback host {config.host} with no "
                    f"--token. Set a token, bind 127.0.0.1, or set "
                    f"SWE_AGENT_TRUST_NETWORK=1 if outer-layer auth is in place.")
    return None


def serve(config: ServerConfig, *, preflight: bool = True, insecure: bool = False) -> None:
    refusal = None if insecure else _safety_refusal(config)
    if refusal:
        print(f"\033[31m{refusal}\033[0m\n  (override with --insecure if you understand the risk.)")
        return
    if preflight:
        ok, msg = llm.check_server(config.base_url, config.model)
        if not ok:
            print(f"\033[31m{msg}\033[0m")
            return
    httpd = build_server(config)
    host, port = httpd.server_address[0], httpd.server_address[1]
    tokeninfo = "token REQUIRED" if config.token else "\033[33mNO TOKEN (open on this host)\033[0m"
    print(f"\033[1mSWE agent server\033[0m on http://{host}:{port}  "
          f"model={config.model} approval={config.approval.value} cwd={Path(config.cwd).resolve()}")
    print(f"  auth: {tokeninfo}   endpoints: /api/health /api/tools /api/chat /api/chat/stream")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  \033[31m⚠ bound to a non-loopback address; tools run real shell/file ops — "
              "set a token and add an isolation layer.\033[0m")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
    finally:
        httpd.registry.cleanup()
        httpd.server_close()


def _approval_from(name: str) -> ApprovalMode:
    return {
        "read-only": ApprovalMode.READ_ONLY,
        "auto": ApprovalMode.AUTO_ACCEPT,
        "auto-accept": ApprovalMode.AUTO_ACCEPT,
        "yolo": ApprovalMode.YOLO,
    }.get(name.lower(), ApprovalMode.READ_ONLY)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="swe_agent.server",
                                description="HTTP/SSE bridge for the SWE agent.")
    p.add_argument("--host", default=os.environ.get("SWE_AGENT_SERVER_HOST", "127.0.0.1"))
    # Cloud Run injects $PORT (typically 8080); keep SWE_AGENT_SERVER_PORT as the
    # explicit per-app override, and fall back to the legacy 8765 for local dev.
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT")
                               or os.environ.get("SWE_AGENT_SERVER_PORT")
                               or "8765"))
    p.add_argument("--model", "-m", default=DEFAULT_MODEL)
    p.add_argument("--base-url", default=DEFAULT_OLLAMA_BASE)
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--max-steps", type=int, default=MAX_STEPS)
    p.add_argument("--cwd", default=None, help="Workspace root the agent operates in")
    p.add_argument("--approval", default="read-only",
                   choices=["read-only", "auto", "auto-accept", "yolo"],
                   help="read-only (default) blocks all mutations; auto-accept allows "
                        "edits; yolo allows everything (dangerous over HTTP)")
    p.add_argument("--token", default=os.environ.get("SWE_AGENT_SERVER_TOKEN"),
                   help="Require this bearer token (env: SWE_AGENT_SERVER_TOKEN)")
    p.add_argument("--static-dir", default=os.environ.get("SWE_AGENT_STATIC_DIR"),
                   help="Serve a built SPA bundle (e.g. web/dist) for non-/api/* GETs. "
                        "Used in single-container deployments like Cloud Run.")
    p.add_argument("--no-persist", action="store_true", help="Don't save sessions to disk")
    p.add_argument("--no-preflight", action="store_true", help="Skip the Ollama server/model check")
    p.add_argument("--insecure", action="store_true",
                   help="Allow starting without a token in a mutating/non-loopback posture "
                        "(you accept the RCE/file-write exposure)")
    args = p.parse_args(argv)

    config = ServerConfig(
        host=args.host, port=args.port, model=args.model, base_url=args.base_url,
        num_ctx=args.num_ctx, temperature=args.temperature, max_steps=args.max_steps,
        cwd=Path(args.cwd).resolve() if args.cwd else Path.cwd(),
        approval=_approval_from(args.approval), token=args.token, persist=not args.no_persist,
        static_dir=Path(args.static_dir).resolve() if args.static_dir else None,
    )
    serve(config, preflight=not args.no_preflight, insecure=args.insecure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
