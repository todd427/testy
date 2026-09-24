"""
Testy — MCP wiring tester.

One remote Streamable HTTP endpoint whose only job is to prove an MCP
client is wired up correctly, and to tell you *which* client connected.

Stack: foxxe-mcp, which runs the MCP SDK's `MCPServer` — the same stack as
every other server in the fleet. That is the point: an observation made
here transfers to the fleet only if Testy runs what the fleet runs.

Targets (all verified Aug 2026):
  - ChatGPT developer mode: remote HTTPS, Streamable HTTP, no-auth OK.
    Deep-research/data-only path additionally requires read-only tools
    named `search` and `fetch` — both provided.
  - Gemini web app custom apps / Gemini CLI / Gemini Enterprise.
  - Claude (connectors).
  - Any dual-conversation client: pass ?tag=A / ?tag=B or an
    X-Conversation-Tag header; `whoami` reflects it back so each
    conversation can prove which leg it is.

The `INIT` log line comes from an ASGI body tap rather than a server hook,
because a stateless SDK session never exposes the client's `clientInfo` to
server code — the initialize request is handled in its own throwaway
session and nothing retains what it declared.

No auth, no data, no state. Do not grow this into a real service —
that is foxxe-mcp's job. The OAuth probe is a SEPARATE app (probe.py,
testy-auth-foxxelabs) for that reason: it observes the OAuth handshake and
logs it, and putting an authorization server on this host would change what
every no-auth client sees here.
"""

from datetime import datetime, timezone
from typing import Any

from foxxe_mcp import Context, MCPServer, build_app, serve
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.routing import Route

import testy_common
from testy_common import (
    BodyTap, SERVER_NAME, VERSION, log_call, log_initialize, request_of,
)

mcp = MCPServer(
    SERVER_NAME,
    instructions=(
        "Wiring tester. Call `ping` to prove connectivity, `echo` to prove "
        "argument marshalling, `whoami` to see what the server sees about "
        "your client. `search`/`fetch` exist to satisfy ChatGPT's "
        "deep-research tool-shape requirement."
    ),
)

# ---------------------------------------------------------------- tools

# Every tool here is a probe: it reads, it never writes, and it never
# reaches outside this process. Without these hints a client has to
# assume the worst — ChatGPT labelled `echo` PUBLIC WRITE / OPEN WORLD
# / DESTRUCTIVE — and the deep-research path expects `search` and
# `fetch` to declare themselves read-only.
READ_ONLY = testy_common.READ_ONLY

# ping, echo and whoami live in testy_common so the OAuth probe can register
# the same functions. The SDK's decorator returns the original function, so
# calling it here is the same as decorating there.
ping = mcp.tool(annotations=READ_ONLY)(testy_common.ping)
echo = mcp.tool(annotations=READ_ONLY)(testy_common.echo)
whoami = mcp.tool(annotations=READ_ONLY)(testy_common.whoami)


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
def search(query: str, ctx: Context) -> dict[str, Any]:
    """Search the tester corpus. Returns all documents regardless of
    query (this is a wiring test, not a search engine). Shape matches
    ChatGPT's deep-research `search` requirement.
    """
    log_call("search", {"query": query}, request=request_of(ctx))
    return {
        "results": [
            {"id": doc_id, "title": d["title"], "url": d["url"]}
            for doc_id, d in _CORPUS.items()
        ]
    }


@mcp.tool(annotations=READ_ONLY)
def fetch(id: str, ctx: Context) -> dict[str, Any]:
    """Fetch one tester document by id. Shape matches ChatGPT's
    deep-research `fetch` requirement.
    """
    log_call("fetch", {"id": id}, request=request_of(ctx))
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
    log_call("testy://readme", kind="resource")
    return (
        "Testy wiring tester. If you are reading this as a resource, "
        "your client supports MCP resources. Marker: TESTY-RESOURCE-OK."
    )


@mcp.prompt()
def wiring_report() -> str:
    """Prompt template. If your client surfaces this, it supports MCP
    prompts."""
    log_call("wiring_report", kind="prompt")
    return (
        "Call ping, echo('test'), whoami, search('anything') and "
        "fetch('doc-1') on the testy server, then report which calls "
        "succeeded and what whoami revealed about this client."
    )


# ---------------------------------------------------------------- app


async def healthz(request):
    """Alias kept for one release in case anything external still probes
    it. fly.toml now checks /health, which build_app provides. Remove this
    in the next change after the foxxe-mcp migration."""
    return JSONResponse({"ok": True, "server": SERVER_NAME, "version": VERSION})


# Built at module level because the tests import server.app.
app = build_app(
    mcp,
    stateless_http=True,
    routes=[Route("/healthz", healthz, methods=["GET"])],
)
app.add_middleware(BodyTap, on_body=log_initialize)


if __name__ == "__main__":
    serve(mcp, app=app)
