"""
testy-auth — the OAuth probe.

A second, OAuth-protected Testy whose only job is to log what each MCP
client does during the OAuth dance, against the same SDK authorization-server
code Mnemos runs. It grants access to nothing but three read-only probe
tools, which is why auto-approving every client is harmless here and must
never happen on Mnemos.

Deployed as its own Fly app (testy-auth-foxxelabs), not a second path on
testy-foxxelabs: an MCPServer carries one auth configuration and OAuth
discovery lives at the root, so putting an authorization server on the
no-auth host would change what every no-auth client sees — contaminating
the thing Testy exists to observe.

State is in memory and is never persisted, on exactly one machine. One
machine because the dance is three separate requests and two machines
behind the Fly proxy would let /register land on one and /authorize on
another, producing an invalid-client error that looks like client
behaviour but is our artefact. In memory because where the provider keeps
state is invisible to the client — and what a client does when the state
is gone is question 7, to be logged rather than prevented.

Read the log, not the response bodies: see docs/BRIEF-oauth-probe.md §2 for
the tag table, and §0 for the questions each tag answers.
"""

import logging
import os
import time
from typing import Any

from foxxe_mcp import Context, MCPServer, build_app, serve
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route

import testy_common
from testy_common import BodyTap, HttpLogger, READ_ONLY, log_probe_body
from probe_oauth import ProbeOAuthProvider

SERVER_NAME = "testy-auth"
VERSION = "0.1.0"

ISSUER = os.environ.get("TESTY_PROBE_ISSUER", "https://testy-auth-foxxelabs.fly.dev")
SEND_ISS = os.environ.get("TESTY_PROBE_ISS", "0") == "1"

# Logged on the testy logger, like every other tag, so one grep covers both
# apps and a BOOT line always sits next to the client behaviour after it.
log = logging.getLogger("testy")

STARTED_AT = time.time()

_REGISTRATION = ClientRegistrationOptions(
    enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"],
)

provider = ProbeOAuthProvider(issuer=ISSUER, send_iss=SEND_ISS)

mcp = MCPServer(
    SERVER_NAME,
    instructions=(
        "OAuth probe. Complete the OAuth flow, then call oauth_whoami to see "
        "what the server recorded about your client during registration, and "
        "whoami to see what it sees on the request itself. Read-only; grants "
        "access to no data."
    ),
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(ISSUER),
        client_registration_options=_REGISTRATION,
        required_scopes=["mcp"],
    ),
)

# ---------------------------------------------------------------- tools

# The same three functions server.py registers — the SDK's decorator
# returns the original function, so registering them on a second instance
# is plain. No search/fetch/resource/prompt here: the probe is about the
# handshake, not the corpus.
ping = mcp.tool(annotations=READ_ONLY)(testy_common.ping)
echo = mcp.tool(annotations=READ_ONLY)(testy_common.echo)
whoami = mcp.tool(annotations=READ_ONLY)(testy_common.whoami)


@mcp.tool(annotations=READ_ONLY)
async def oauth_whoami(ctx: Context) -> dict[str, Any]:
    """What this server recorded about your client when it registered:
    client_name, redirect_uris, token_endpoint_auth_method, grant_types,
    when the registration was issued, your token's scopes and remaining
    lifetime, and how long this server process has been running.

    The uptime is the useful one: if it is small and your token stopped
    working, the server restarted and lost its OAuth state — that is
    expected here and is exactly what the probe is measuring.

    Never returns your token, your client_id, or any secret.
    """
    token = get_access_token()
    if token is None:
        return {"authenticated": False, "uptime_seconds": int(time.time() - STARTED_AT)}

    client = await provider.get_client(token.client_id)
    testy_common.log_call("oauth_whoami", request=testy_common.request_of(ctx))

    info: dict[str, Any] = {
        "authenticated": True,
        "scopes": list(token.scopes or []),
        "expires_in_seconds": (int(token.expires_at - time.time())
                               if token.expires_at else None),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "iss_toggle": SEND_ISS,
    }
    if client is None:
        # The token verified but its client is gone: mid-session state loss.
        info["client_known"] = False
        return info

    info.update({
        "client_known": True,
        "client_name": getattr(client, "client_name", None),
        "redirect_uris": [str(u) for u in (client.redirect_uris or [])],
        "token_endpoint_auth_method": getattr(client, "token_endpoint_auth_method", None),
        "grant_types": list(getattr(client, "grant_types", None) or []),
        "client_id_issued_at": getattr(client, "client_id_issued_at", None),
    })
    return info


# ------------------------------------------------------------ RFC 9207

async def _metadata_with_iss(request):
    """Authorization-server metadata plus authorization_response_iss_parameter_supported.

    The SDK builds this document with no hook for extra fields, so when the
    toggle is on this route is PREPENDED (build_app puts routes= ahead of the
    SDK's) and serves the SDK's own metadata with the one field added — never
    a hand-written document, so nothing else can drift.
    """
    metadata = build_metadata(
        issuer_url=AnyHttpUrl(ISSUER),
        service_documentation_url=None,
        client_registration_options=_REGISTRATION,
        # Not optional despite the AuthSettings default: build_metadata reads
        # .enabled off it unconditionally.
        revocation_options=RevocationOptions(),
    )
    body = metadata.model_dump(exclude_none=True, mode="json")
    body["authorization_response_iss_parameter_supported"] = True
    return JSONResponse(body)


iss_routes = (
    [Route("/.well-known/oauth-authorization-server", _metadata_with_iss, methods=["GET"])]
    if SEND_ISS else []
)

# ---------------------------------------------------------------- app

app = build_app(mcp, stateless_http=True, routes=iss_routes)
app.add_middleware(BodyTap, on_body=log_probe_body)
app.add_middleware(HttpLogger)   # outermost: added last, so it wraps the rest


def log_boot() -> None:
    import json as _json

    from foxxe_mcp import SDK_VERSION, version as _fx
    log.info("BOOT %s", _json.dumps({
        "server": SERVER_NAME,
        "version": VERSION,
        "started_at": STARTED_AT,
        "mcp": SDK_VERSION,
        "foxxe_mcp": getattr(_fx, "__version__", None) or _fx,
        "iss_toggle": SEND_ISS,
        "issuer": ISSUER,
    }, default=str))


if __name__ == "__main__":
    log_boot()
    serve(mcp, app=app)
