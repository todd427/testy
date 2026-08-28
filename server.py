"""
Testy — MCP wiring tester.

One remote Streamable HTTP endpoint whose only job is to prove an MCP
client is wired up correctly, and to tell you *which* client connected.

Targets (all verified Aug 2026):
  - ChatGPT developer mode: remote HTTPS, Streamable HTTP, no-auth OK.
    Deep-research/data-only path additionally requires read-only tools
    named `search` and `fetch` — both provided.
  - Gemini web app custom apps / Gemini CLI / Gemini Enterprise.
  - Claude (connectors).
  - Beirt (dual-conversation client): pass ?tag=A / ?tag=B or an
    X-Beirt-Conversation header; `whoami` reflects it back so each
    conversation can prove which leg it is.

No auth, no data, no state. Do not grow this into a real service —
that is foxxe-mcp's job.
"""

import json
import logging
import sys
from datetime import datetime, timezone

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers, get_http_request
from fastmcp.server.middleware import Middleware

SERVER_NAME = "testy"
VERSION = "0.1.0"

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(SERVER_NAME)

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "Wiring tester. Call `ping` to prove connectivity, `echo` to prove "
        "argument marshalling, `whoami` to see what the server sees about "
        "your client. `search`/`fetch` exist to satisfy ChatGPT's "
        "deep-research tool-shape requirement."
    ),
)


def _redact_forwarded_for(value: str) -> str:
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


def _client_fingerprint() -> dict:
    """What the server can see about the calling client."""
    # `mcp-session-id` is in FastMCP's default strip-list, so it has to
    # be asked for by name or the field below is always blank.
    headers = get_http_headers(include={"mcp-session-id"}) or {}
    fp = {
        "user_agent": headers.get("user-agent", ""),
        "mcp_protocol_version": headers.get("mcp-protocol-version", ""),
        "mcp_session_id": headers.get("mcp-session-id", ""),
        "origin": headers.get("origin", ""),
        "x_forwarded_for": _redact_forwarded_for(headers.get("x-forwarded-for", "")),
        "beirt_conversation": headers.get("x-beirt-conversation", ""),
    }
    try:
        req = get_http_request()
        if req is not None:
            fp["path"] = str(req.url.path)
            fp["query"] = dict(req.query_params)
    except Exception:
        pass
    return fp


def _log_call(tool: str, extra: dict | None = None) -> None:
    rec = {"tool": tool, "client": _client_fingerprint()}
    if extra:
        rec["args"] = extra
    log.info("CALL %s", json.dumps(rec, default=str))


class InitializeLogger(Middleware):
    """Log what a client declares when it initializes.

    ChatGPT (`openai-mcp/1.0.0`) never sends the `MCP-Protocol-Version`
    header on later requests, so `whoami` reports "" for it. The
    initialize request carries the version and the client's own name
    either way, and on a stateless server this hook is the only place
    that information is ever visible — nothing retains it afterwards.
    """

    async def on_initialize(self, context, call_next):
        params = getattr(context.message, "params", None)
        info = getattr(params, "clientInfo", None)
        rec = {
            "protocol_version": getattr(params, "protocolVersion", "") or "",
            "client_name": getattr(info, "name", "") or "",
            "client_version": getattr(info, "version", "") or "",
            "http": _client_fingerprint(),
        }
        log.info("INIT %s", json.dumps(rec, default=str))
        return await call_next(context)


mcp.add_middleware(InitializeLogger())


# ---------------------------------------------------------------- tools

# Every tool here is a probe: it reads, it never writes, and it never
# reaches outside this process. Without these hints a client has to
# assume the worst — ChatGPT labelled `echo` PUBLIC WRITE / OPEN WORLD
# / DESTRUCTIVE — and the deep-research path expects `search` and
# `fetch` to declare themselves read-only.
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


@mcp.tool(annotations=READ_ONLY)
def ping() -> dict:
    """Liveness check. Returns server identity and UTC time."""
    _log_call("ping")
    return {
        "server": SERVER_NAME,
        "version": VERSION,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "message": "pong",
    }


@mcp.tool(annotations=READ_ONLY)
def echo(text: str) -> dict:
    """Round-trip test: returns the text, its reverse, and its length.

    Proves argument marshalling works in both directions.
    """
    _log_call("echo", {"text": text})
    return {"text": text, "reversed": text[::-1], "length": len(text)}


@mcp.tool(annotations=READ_ONLY)
def whoami() -> dict:
    """Reflects back what the server sees about the calling client:
    User-Agent, negotiated MCP protocol version, session id, origin,
    forwarded IP, and any Beirt conversation tag. Use this to confirm
    WHICH client (ChatGPT / Gemini / Claude / Beirt leg A or B) is
    actually connected.
    """
    _log_call("whoami")
    return _client_fingerprint()


# Tiny canned corpus so `search`/`fetch` satisfy ChatGPT's
# deep-research tool-shape requirement with something real to return.
_CORPUS = {
    "doc-1": {
        "title": "Testy wiring test document",
        "text": (
            "If you can read this via fetch, the search/fetch path is "
            "wired correctly. Marker: TESTY-OK-1."
        ),
        "url": "https://foxxelabs.ie/testy/doc-1",
    },
    "doc-2": {
        "title": "Second test document",
        "text": (
            "Secondary document to prove multi-result search. "
            "Marker: TESTY-OK-2."
        ),
        "url": "https://foxxelabs.ie/testy/doc-2",
    },
}


@mcp.tool(annotations=READ_ONLY)
def search(query: str) -> dict:
    """Search the tester corpus. Returns all documents regardless of
    query (this is a wiring test, not a search engine). Shape matches
    ChatGPT's deep-research `search` requirement.
    """
    _log_call("search", {"query": query})
    return {
        "results": [
            {"id": doc_id, "title": d["title"], "url": d["url"]}
            for doc_id, d in _CORPUS.items()
        ]
    }


@mcp.tool(annotations=READ_ONLY)
def fetch(id: str) -> dict:
    """Fetch one tester document by id. Shape matches ChatGPT's
    deep-research `fetch` requirement.
    """
    _log_call("fetch", {"id": id})
    d = _CORPUS.get(id)
    if d is None:
        return {"id": id, "title": "not found", "text": "", "url": "", "metadata": {}}
    return {
        "id": id,
        "title": d["title"],
        "text": d["text"],
        "url": d["url"],
        "metadata": {"server": SERVER_NAME},
    }


# ------------------------------------------------- capability probes
# These exist to reveal which clients surface non-tool capabilities.


@mcp.resource("testy://readme")
def readme() -> str:
    """Static resource. If your client can list and read this, it
    supports MCP resources (ChatGPT generally will not show it)."""
    return (
        "Testy wiring tester. If you are reading this as a resource, "
        "your client supports MCP resources. Marker: TESTY-RESOURCE-OK."
    )


@mcp.prompt
def wiring_report() -> str:
    """Prompt template. If your client surfaces this, it supports MCP
    prompts."""
    return (
        "Call ping, echo('test'), whoami, search('anything') and "
        "fetch('doc-1') on the testy server, then report which calls "
        "succeeded and what whoami revealed about this client."
    )


# ---------------------------------------------------------------- app


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": True, "server": SERVER_NAME, "version": VERSION})


app = mcp.http_app(path="/mcp", stateless_http=True, transport="http")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
