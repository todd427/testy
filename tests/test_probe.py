"""The OAuth probe: the dance, what gets logged, and what must never be.

These drive probe:app over real HTTP, because the thing under test is an
authorization server and half of it is redirects and status codes.
"""

import base64
import hashlib
import json
import logging
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import probe

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    return server, thread


@pytest.fixture(scope="module")
def base():
    port = _free_port()
    server, thread = _serve(probe.app, port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with an empty provider — these share one app."""
    p = probe.provider
    p._clients.clear(); p._codes.clear(); p._tokens.clear(); p._refresh.clear()
    yield


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(base, **overrides):
    body = {
        "client_name": "probe-test-client",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        **overrides,
    }
    return httpx.post(f"{base}/register", json=body, timeout=10)


def dance(base, **overrides):
    """Register → authorize → token. Returns (client, tokens, verifier)."""
    client = register(base, **overrides).json()
    verifier, challenge = pkce()
    r = httpx.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "probe-state", "scope": "mcp",
    }, follow_redirects=False, timeout=10)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "client_id": client["client_id"],
            "code_verifier": verifier}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    tokens = httpx.post(f"{base}/token", data=form, timeout=10).json()
    return client, tokens, verifier



def wait_tagged(caplog, tag, match=lambda _: True, timeout=2.0):
    """tagged(), but wait for a matching record to arrive.

    HttpLogger writes its line AFTER the response has gone back, so httpx
    returns before the server thread has logged. Asserting immediately is a
    race — one that only shows up when test order shifts.
    """
    deadline = time.time() + timeout
    while True:
        hits = [t for t in tagged(caplog, tag) if match(t)]
        if hits or time.time() > deadline:
            return hits
        time.sleep(0.02)


def tagged(caplog, tag):
    """Parsed payloads of our own `TAG {json}` records.

    Filtering on the logger name is load-bearing: httpx logs its own lines
    starting "HTTP Request: ...", which collide with our HTTP tag.
    """
    out = []
    for rec in caplog.records:
        if rec.name != "testy":
            continue
        msg = rec.getMessage()
        if msg.startswith(tag + " "):
            out.append(json.loads(msg[len(tag) + 1:]))
    return out


# ------------------------------------------------------------ metadata


def test_metadata_advertises_registration_and_s256(base):
    meta = httpx.get(f"{base}/.well-known/oauth-authorization-server", timeout=10).json()
    assert meta["registration_endpoint"].endswith("/register")
    assert "S256" in meta["code_challenge_methods_supported"]
    # Toggle is off by default, so the RFC 9207 field must be absent.
    assert "authorization_response_iss_parameter_supported" not in meta


def test_protected_resource_metadata_exists(base):
    r = httpx.get(f"{base}/.well-known/oauth-protected-resource", timeout=10)
    assert r.status_code == 200


# ---------------------------------------------------------- the dance


def test_full_dance_confidential_client(base):
    # No token_endpoint_auth_method sent → the SDK issues a secret and
    # defaults to client_secret_post.
    client, tokens, _ = dance(base)
    assert client["client_secret"]
    assert client.get("token_endpoint_auth_method", "client_secret_post") == "client_secret_post"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["token_type"].lower() == "bearer"


def test_full_dance_public_client(base):
    client, tokens, _ = dance(base, token_endpoint_auth_method="none")
    assert not client.get("client_secret")
    assert tokens["access_token"]


def test_authorize_round_trips_state(base):
    client = register(base).json()
    _, challenge = pkce()
    r = httpx.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "xyzzy", "scope": "mcp",
    }, follow_redirects=False, timeout=10)
    assert r.status_code in (302, 303, 307)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyzzy"]
    assert q["code"]
    assert "iss" not in q      # toggle off


def test_refresh_grant_issues_a_new_access_token(base):
    client, tokens, _ = dance(base)
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"]}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    refreshed = httpx.post(f"{base}/token", data=form, timeout=10).json()
    assert refreshed["access_token"] != tokens["access_token"]


async def test_oauth_whoami_reports_the_registration(base):
    client, tokens, _ = dance(base, client_name="whoami-probe")
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {tokens['access_token']}"}
    ) as h:
        async with streamable_http_client(f"{base}/mcp", http_client=h) as (r, w, _x):
            async with ClientSession(r, w) as session:
                await session.initialize()
                data = (await session.call_tool("oauth_whoami", {})).structuredContent

    assert data["authenticated"] is True
    assert data["client_known"] is True
    assert data["client_name"] == "whoami-probe"
    assert data["redirect_uris"] == [REDIRECT]
    assert data["scopes"] == ["mcp"]
    assert data["expires_in_seconds"] > 0
    assert data["uptime_seconds"] >= 0
    # Never leak the bearer's identity material.
    assert "client_id" not in data
    assert "client_secret" not in data
    assert "token" not in data


async def test_probe_exposes_only_the_four_probe_tools(base):
    _, tokens, _ = dance(base)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {tokens['access_token']}"}
    ) as h:
        async with streamable_http_client(f"{base}/mcp", http_client=h) as (r, w, _x):
            async with ClientSession(r, w) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
    assert names == {"ping", "echo", "whoami", "oauth_whoami"}


# ------------------------------------------------------------- refusals


def test_mcp_without_a_bearer_is_401(base):
    r = httpx.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                   headers={"Accept": "application/json, text/event-stream"}, timeout=10)
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert "oauth-protected-resource" in r.headers["WWW-Authenticate"]


def test_registration_asking_for_another_scope_is_rejected(base, caplog):
    with caplog.at_level(logging.INFO, logger="testy"):
        r = register(base, scope="openid")
    assert r.status_code == 400
    # The raw body is logged even though the SDK rejected it — that is the
    # finding: what the client asked for, and that it was refused.
    regs = tagged(caplog, "REG")
    assert regs and regs[-1]["raw"]["scope"] == "openid"


def test_a_disallowed_redirect_is_refused(base):
    client = register(base, redirect_uris=["http://evil.example/cb"]).json()
    _, challenge = pkce()
    r = httpx.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": "http://evil.example/cb", "code_challenge": challenge,
        "code_challenge_method": "S256", "scope": "mcp",
    }, follow_redirects=False, timeout=10)
    # Plain http on a non-loopback host is not allowed; the SDK surfaces it.
    assert r.status_code >= 400 or "error" in urlparse(r.headers.get("location", "")).query


# ------------------------------------------------------- state loss (Q7)
# A stop wipes the provider's four dicts. Clearing them is that wipe: the
# provider IS the state, so this drives the same code paths a restart does.
# The one thing it cannot reproduce is a new process, which is why
# oauth_whoami reports uptime separately.


def _wipe():
    p = probe.provider
    p._clients.clear(); p._codes.clear(); p._tokens.clear(); p._refresh.clear()


async def test_access_token_after_state_loss_is_401(base, caplog):
    _, tokens, _ = dance(base)
    with caplog.at_level(logging.INFO, logger="testy"):
        _wipe()
        r = httpx.post(f"{base}/mcp",
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Authorization": f"Bearer {tokens['access_token']}",
                                "Accept": "application/json, text/event-stream"},
                       timeout=10)
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert any(m["lookup"] == "load_access_token" for m in tagged(caplog, "UNKNOWN"))


def test_refresh_token_after_state_loss_is_rejected(base, caplog):
    client, tokens, _ = dance(base)
    with caplog.at_level(logging.INFO, logger="testy"):
        _wipe()
        form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"]}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        r = httpx.post(f"{base}/token", data=form, timeout=10)

    assert r.status_code >= 400
    # The brief guessed invalid_grant. The SDK never reaches the grant: the
    # CLIENT is gone too, so client authentication fails first and the answer
    # is unauthorized_client / "Invalid client_id". Recorded rather than
    # assumed, because what a client does next depends on which it sees.
    assert r.json()["error"] == "unauthorized_client", r.json()
    assert "client_id" in r.json()["error_description"].lower()
    assert any(m["lookup"] == "get_client" for m in tagged(caplog, "UNKNOWN"))


def test_authorize_after_state_loss_is_invalid_client(base):
    client, _, _ = dance(base)
    _wipe()
    _, challenge = pkce()
    r = httpx.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "scope": "mcp",
    }, follow_redirects=False, timeout=10)
    assert r.status_code >= 400
    # Also not invalid_client: /authorize answers invalid_request with
    # "Client ID ... not found" — a third distinct shape for the same
    # underlying state loss, which is worth knowing before reading a client's
    # reaction to it.
    assert r.json()["error"] == "invalid_request", r.json()
    assert "not found" in r.json()["error_description"]


def test_a_fresh_registration_works_after_state_loss(base):
    dance(base)
    _wipe()
    _, tokens, _ = dance(base)          # the recovery path a client must take
    assert tokens["access_token"]


# --------------------------------------------------------------- redaction


SECRET_FIELDS = ("code_verifier", "client_secret")


def test_the_dance_leaks_nothing_into_the_log(base, caplog):
    with caplog.at_level(logging.INFO, logger="testy"):
        probe.log_boot()
        client, tokens, verifier = dance(base, client_name="redaction-probe")

    # Our records only — httpx logs the full authorize URL itself, which is
    # its business, not a leak in what this server writes.
    blob = "\n".join(r.getMessage() for r in caplog.records if r.name == "testy")

    # Nothing that could be replayed may appear anywhere in the log.
    for secret in (tokens["access_token"], tokens["refresh_token"], verifier,
                   client["client_id"], client.get("client_secret") or "\0",
                   "probe-state"):
        assert secret not in blob, f"leaked: {secret[:12]}…"

    # And every tag the brief asks for is present and parses.
    wait_tagged(caplog, "HTTP", lambda line: line["path"] == "/token")
    tags = {t: tagged(caplog, t) for t in ("BOOT", "HTTP", "REG", "REG_OK", "AUTHZ", "TOKEN")}
    tags = {t: v for t, v in tags.items() if v}
    assert set(tags) == {"BOOT", "HTTP", "REG", "REG_OK", "AUTHZ", "TOKEN"}, sorted(tags)

    # The fields we came for ARE there.
    assert tags["REG"][-1]["raw"]["client_name"] == "redaction-probe"
    assert tags["REG_OK"][-1]["client_name"] == "redaction-probe"
    assert tags["REG_OK"][-1]["redirect_uris"] == [REDIRECT]
    assert tags["AUTHZ"][-1]["redirect_host"] == "claude.ai"
    assert tags["AUTHZ"][-1]["state_present"] is True
    assert tags["AUTHZ"][-1]["code_challenge_present"] is True
    request_token_lines = [t for t in tags["TOKEN"] if t.get("stage") == "request"]
    assert request_token_lines[-1]["grant_type"] == "authorization_code"
    assert request_token_lines[-1]["client_auth"] in {"client_secret_post", "none"}
    # Form field NAMES are logged; their values, beyond the allow-list, are not.
    assert "code_verifier" in request_token_lines[-1]["fields"]


def test_token_tap_is_an_allow_list(base, caplog):
    # A field no one anticipated must be reported by name only.
    client, _, _ = dance(base)
    with caplog.at_level(logging.INFO, logger="testy"):
        httpx.post(f"{base}/token", data={
            "grant_type": "refresh_token", "refresh_token": "nope",
            "client_id": client["client_id"], "surprise_field": "surprise-value",
        }, timeout=10)
    request_lines = [line for line in tagged(caplog, "TOKEN") if line.get("stage") == "request"]
    assert "surprise_field" in request_lines[-1]["fields"]
    assert "surprise-value" not in json.dumps(request_lines[-1])


# ------------------------------------------------------------ HTTP logging


def test_http_logger_records_a_404_on_an_unknown_well_known(base, caplog):
    with caplog.at_level(logging.INFO, logger="testy"):
        httpx.get(f"{base}/.well-known/openid-configuration", timeout=10)
    hit = wait_tagged(caplog, "HTTP",
                      lambda line: line["path"] == "/.well-known/openid-configuration")
    assert hit, "the 404 was not logged"
    assert hit[-1]["status"] == 404


def test_http_logger_keeps_query_values_only_for_discovery(base, caplog):
    client = register(base).json()
    _, challenge = pkce()
    with caplog.at_level(logging.INFO, logger="testy"):
        httpx.get(f"{base}/authorize", params={
            "response_type": "code", "client_id": client["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "must-not-appear", "scope": "mcp",
        }, follow_redirects=False, timeout=10)
    authz = wait_tagged(caplog, "HTTP", lambda line: line["path"] == "/authorize")[-1]
    assert authz["query"] == sorted(authz["query"])      # keys only, sorted
    assert "must-not-appear" not in json.dumps(authz)


# --------------------------------------------------------- RFC 9207 toggle


@pytest.fixture(scope="module")
def iss_base(monkeypatch_module=None):
    """A second, independent probe module with the iss toggle on."""
    import importlib.util
    import os

    os.environ["TESTY_PROBE_ISS"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("probe_iss", "probe.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.pop("TESTY_PROBE_ISS", None)

    port = _free_port()
    server, thread = _serve(mod.app, port)
    yield f"http://127.0.0.1:{port}", mod
    server.should_exit = True
    thread.join(timeout=5)


def test_iss_toggle_advertises_and_sends_iss(iss_base):
    base_url, mod = iss_base
    meta = httpx.get(f"{base_url}/.well-known/oauth-authorization-server", timeout=10).json()
    assert meta["authorization_response_iss_parameter_supported"] is True
    # The SDK's own fields must all survive — this route replaces the SDK's.
    assert meta["registration_endpoint"].endswith("/register")
    assert meta["authorization_endpoint"].endswith("/authorize")
    assert meta["token_endpoint"].endswith("/token")
    assert "S256" in meta["code_challenge_methods_supported"]

    client = register(base_url).json()
    _, challenge = pkce()
    r = httpx.get(f"{base_url}/authorize", params={
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s", "scope": "mcp",
    }, follow_redirects=False, timeout=10)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["iss"] == [mod.ISSUER]
