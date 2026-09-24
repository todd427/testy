"""
Shared pieces of the Testy wiring tester.

Split out of server.py so the OAuth probe (docs/BRIEF-oauth-probe.md) can
reuse them: the client fingerprint, the X-Forwarded-For redaction, and the
pure-ASGI body tap that recovers what a client declares at initialize.

Nothing here reaches outside the process or retains anything between
requests.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from foxxe_mcp import Context
from mcp.types import ToolAnnotations

SERVER_NAME = "testy"

log = logging.getLogger(SERVER_NAME)

# A body over this is passed straight through untapped. An initialize
# request is a few hundred bytes; anything near the cap is not one.
BODY_TAP_LIMIT = 1024 * 1024  # 1 MiB


def redact_forwarded_for(value: str) -> str:
    """Drop the originating address from an X-Forwarded-For chain.

    ChatGPT forwards the end user's real IP as the leftmost hop, so an
    unredacted log accumulates a record of where this server's users
    sit. Testy exists to identify which *client* connected, not to
    collect addresses, so the leftmost hop is replaced. The proxy hops
    are kept — they still show the request path.
    """
    hops = [hop.strip() for hop in value.split(",") if hop.strip()]
    if not hops:
        return ""
    return ", ".join(["<redacted>"] + hops[1:])


def _fingerprint_from_headers(headers: dict, path: str = "", query: dict | None = None) -> dict:
    fp = {
        "user_agent": headers.get("user-agent", ""),
        "mcp_protocol_version": headers.get("mcp-protocol-version", ""),
        "mcp_session_id": headers.get("mcp-session-id", ""),
        "origin": headers.get("origin", ""),
        "x_forwarded_for": redact_forwarded_for(headers.get("x-forwarded-for", "")),
        "conversation_tag": headers.get("x-conversation-tag", ""),
    }
    if path:
        fp["path"] = path
        fp["query"] = dict(query or {})
    return fp


def client_fingerprint(request=None) -> dict:
    """What the server can see about the calling client.

    Takes the Starlette Request the SDK attaches to the tool's request
    context. None (an in-memory client, or a call outside HTTP) yields the
    same keys with empty values, so the shape never varies.
    """
    if request is None:
        return _fingerprint_from_headers({})
    return _fingerprint_from_headers(
        dict(request.headers), str(request.url.path), dict(request.query_params)
    )


def fingerprint_from_scope(scope: dict) -> dict:
    """The same fingerprint, built from a raw ASGI scope.

    The body tap runs below Starlette, where there is no Request object.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers", [])}
    query = {}
    raw_query = scope.get("query_string", b"").decode("latin-1")
    if raw_query:
        from urllib.parse import parse_qs

        query = {k: v[0] for k, v in parse_qs(raw_query).items()}
    return _fingerprint_from_headers(headers, scope.get("path", ""), query)


def log_call(name: str, extra: dict | None = None, kind: str = "tool", request=None) -> None:
    rec = {"kind": kind, "name": name, "client": client_fingerprint(request)}
    if extra:
        rec["args"] = extra
    log.info("CALL %s", json.dumps(rec, default=str))


class BodyTap:
    """Pure-ASGI middleware that shows a callback each POST body.

    Pure ASGI rather than BaseHTTPMiddleware for the reason foxxe-mcp's
    BearerAuthMiddleware gives: the streamable-HTTP response is long-lived
    and BaseHTTPMiddleware buffers it through an anyio stream.

    The body is buffered up to BODY_TAP_LIMIT and replayed to the app, so
    the tap is invisible downstream. Over the cap, nothing is buffered and
    the stream is passed through untouched. on_body is called inside a
    try/except: a logging failure must never fail the request.
    """

    def __init__(self, app, *, on_body):
        self.app = app
        self.on_body = on_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        chunks, size, overflowed, more = [], 0, False, True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # http.disconnect — hand it back and let the app deal with it
                chunks.append(None)
                break
            body = message.get("body", b"")
            size += len(body)
            if size > BODY_TAP_LIMIT:
                overflowed = True
                chunks.append(body)
                more = message.get("more_body", False)
                break
            chunks.append(body)
            more = message.get("more_body", False)

        buffered = b"".join(c for c in chunks if c)

        if not overflowed:
            try:
                self.on_body(scope, buffered)
            except Exception:  # noqa: BLE001 — never fail a request over a log line
                log.exception("body tap failed")

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": buffered, "more_body": more}
            return await receive()

        await self.app(scope, replay, send)


def log_initialize(scope: dict, body: bytes) -> None:
    """Log what a client declares when it initializes.

    ChatGPT (`openai-mcp/1.0.0`) never sends the MCP-Protocol-Version
    header on later requests, so whoami reports "" for it. The initialize
    request carries the version and the client's own name either way.

    On a stateless SDK server this has to be read off the wire: the session
    that handles an initialize is a throwaway, and no server-side hook
    exposes its client_params afterwards.
    """
    if scope.get("path") != "/mcp":
        return
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return
    for message in payload if isinstance(payload, list) else [payload]:
        if not isinstance(message, dict) or message.get("method") != "initialize":
            continue
        params = message.get("params") or {}
        info = params.get("clientInfo") or {}
        log.info("INIT %s", json.dumps({
            "protocol_version": params.get("protocolVersion", "") or "",
            "client_name": info.get("name", "") or "",
            "client_version": info.get("version", "") or "",
            "http": fingerprint_from_scope(scope),
        }, default=str))


# ---------------------------------------------------------------- tools
# Defined here, not in server.py, because the OAuth probe registers the
# same three functions on its own MCPServer. The SDK's tool() decorator
# returns the original function, so one function can be registered twice.

VERSION = "0.2.0"

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def request_of(ctx: Context) -> Any:
    """The Starlette request behind this call, or None off the HTTP path."""
    return getattr(ctx.request_context, "request", None)


def ping(ctx: Context) -> dict[str, Any]:
    """Liveness check. Returns server identity and UTC time."""
    log_call("ping", request=request_of(ctx))
    return {
        "server": SERVER_NAME,
        "version": VERSION,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "message": "pong",
    }


def echo(text: str, ctx: Context) -> dict[str, Any]:
    """Round-trip test: returns the text, its reverse, and its length.

    Proves argument marshalling works in both directions.
    """
    log_call("echo", {"text": text}, request=request_of(ctx))
    return {"text": text, "reversed": text[::-1], "length": len(text)}


def whoami(ctx: Context) -> dict[str, Any]:
    """Reflects back what the server sees about the calling client:
    User-Agent, negotiated MCP protocol version, session id, origin,
    forwarded IP, and any conversation tag. Use this to confirm WHICH
    client (ChatGPT / Gemini / Claude, or leg A vs leg B of a
    dual-conversation client) is actually connected.
    """
    req = request_of(ctx)
    log_call("whoami", request=req)
    return client_fingerprint(req)


# ------------------------------------------------------- probe logging
# Used by probe.py only. Kept here because BodyTap lives here and the two
# are a pair: the tap buffers the body, these decide what may be said
# about it.

# Allow-list, not a deny-list. A form field a client adds next year is
# reported by NAME only until someone decides it is safe to log the value.
# client_id is deliberately absent: it is logged as an 8-char prefix only,
# enough to correlate two lines and not enough to replay.
_TOKEN_VALUE_KEYS = {"grant_type", "resource", "redirect_uri", "scope"}

# Query-string values safe to record outside .well-known. code_challenge_method
# is here because it answers question 3 and is visible NOWHERE else: the SDK
# validates it in its /authorize handler and AuthorizationParams does not carry
# it, so the provider can never log it. state, code, client_id and
# code_challenge are deliberately absent.
_QUERY_VALUE_KEYS = {"response_type", "code_challenge_method", "scope", "resource"}


class HttpLogger:
    """Outermost pure-ASGI middleware: one HTTP line per request.

    Outermost so it sees the 404s too — which .well-known paths a client
    probes, and which it tolerates missing, is one of the questions.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        fp = fingerprint_from_scope(scope)
        path = scope.get("path", "")
        query = dict(fp.get("query") or {})
        status = {"code": None}

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            log.info("HTTP %s", json.dumps({
                "method": scope.get("method"),
                "path": path,
                # Values only for discovery paths, where they are the finding.
                # Elsewhere the keys alone say what was asked without risking
                # a code or a state value in the log.
                "query": (query if path.startswith("/.well-known")
                          else sorted(query)),
                "query_values": ({k: query[k] for k in _QUERY_VALUE_KEYS if k in query}
                                 if not path.startswith("/.well-known") else None),
                "status": status["code"],
                "user_agent": fp["user_agent"],
                "x_forwarded_for": fp["x_forwarded_for"],
            }, default=str))


def _form_to_dict(body: bytes) -> dict:
    from urllib.parse import parse_qs
    try:
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
    except (ValueError, UnicodeDecodeError):
        return {}


def log_probe_body(scope: dict, body: bytes) -> None:
    """BodyTap callback for the probe: REG, TOKEN, and INIT.

    /register is logged RAW and in full. That is the point of question 1 —
    what a client actually sends, before the SDK parses it or rejects it —
    and a registration body carries no secret: the SDK issues the
    client_id and client_secret afterwards, in its response.
    """
    path = scope.get("path", "")

    if path == "/register":
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            parsed = None
        log.info("REG %s", json.dumps({
            "raw": parsed if parsed is not None else body[:4000].decode("utf-8", "replace"),
            "parsed": parsed is not None,
        }, default=str))
        return

    if path == "/token":
        form = _form_to_dict(body)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        # How the client actually authenticated, without reading the value.
        if auth.lower().startswith("basic "):
            client_auth = "client_secret_basic"
        elif "client_secret" in form:
            client_auth = "client_secret_post"
        else:
            client_auth = "none"
        log.info("TOKEN %s", json.dumps({
            "stage": "request",
            "fields": sorted(form),          # names of everything sent
            **{k: form[k] for k in _TOKEN_VALUE_KEYS if k in form},
            "client_id_prefix": form.get("client_id", "")[:8],
            "client_auth": client_auth,
        }, default=str))
        return

    log_initialize(scope, body)
