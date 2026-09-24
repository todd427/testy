"""The HTTP surface: health checks, and the client identification that
only works when there is a real request to inspect.

`whoami` is the tool the README leans on to answer "which client is
actually connected", and it reads HTTP headers — so it can only be
tested honestly over the wire.
"""

import json
import logging

import httpx
import pytest
import server as testy
import testy_common
from conftest import call


# ------------------------------------------------------------- health


def test_healthz_reports_server_identity(http_base):
    body = httpx.get(f"{http_base}/healthz").json()
    assert body == {"ok": True, "server": testy.SERVER_NAME, "version": testy.VERSION}


def test_healthz_alias_survives_the_migration(http_base):
    # Kept for one release in case anything external still probes it.
    # fly.toml now checks /health.
    assert httpx.get(f"{http_base}/healthz", timeout=5).status_code == 200


def test_health_is_the_shape_flys_check_expects(http_base):
    # fly.toml health-checks this path with a 5s timeout.
    response = httpx.get(f"{http_base}/health", timeout=5)
    assert response.status_code == 200
    assert response.json()["server"] == testy.SERVER_NAME


def test_version_shows_the_fleet_stack(http_base):
    # The point of the migration: foxxe-mcp and the SDK are present and
    # standalone fastmcp is not. If fastmcp comes back, the fleet bound
    # has been breached again.
    body = httpx.get(f"{http_base}/version", timeout=5).json()
    assert body["foxxe_mcp"]
    assert body["sdk"]["mcp"].startswith("1.")
    # fastmcp lives inside the sdk block, not at the top level — asserting
    # on body["fastmcp"] would pass whatever is installed.
    assert body["sdk"]["fastmcp"] is None
    assert body["sdk"]["supported"] is True


# ------------------------------------------------ client fingerprinting


async def test_whoami_reflects_the_user_agent(http_client):
    async with http_client(headers={"User-Agent": "testy-suite/1.0"}) as client:
        data = await call(client, "whoami")
    assert data["user_agent"] == "testy-suite/1.0"


async def test_whoami_reports_the_negotiated_protocol_version(http_client):
    async with http_client() as client:
        data = await call(client, "whoami")
    # Set by the client on every post-initialize request; the value moves
    # with the spec, so assert it was negotiated at all.
    assert data["mcp_protocol_version"]


async def test_whoami_reports_the_request_path(http_client):
    async with http_client() as client:
        data = await call(client, "whoami")
    assert data["path"] == "/mcp"


async def test_whoami_can_see_the_session_id_header(http_client):
    # FastMCP stripped `mcp-session-id` from get_http_headers() unless it
    # was asked for by name; reading the Starlette request directly has no
    # strip-list, so the workaround is gone. The field still has to be
    # reported, which is what this guards.
    async with http_client(headers={"mcp-session-id": "abc123"}) as client:
        data = await call(client, "whoami")
    assert data["mcp_session_id"] == "abc123"


async def test_whoami_reports_no_session_id_while_stateless(http_client):
    # Not a bug: a stateless server never issues a session, so a client
    # that was not given one has nothing to send. Documents why the
    # deployed server reports "" here.
    async with http_client() as client:
        data = await call(client, "whoami")
    assert data["mcp_session_id"] == ""


def test_initialize_issues_no_session_id_when_stateless(http_base):
    # The real statelessness check. A stateful server hands back an
    # `mcp-session-id` header on initialize; a stateless one must not,
    # or Fly is free to route the next call to another machine and the
    # session breaks. fly.toml scales to zero, so this matters.
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "testy-suite", "version": "1.0"},
        },
    }
    response = httpx.post(
        f"{http_base}/mcp",
        json=request,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers


# ------------------------------------------------- conversation legs


async def test_conversation_tag_header_is_reflected(http_client):
    async with http_client(headers={"X-Conversation-Tag": "leg-A"}) as client:
        data = await call(client, "whoami")
    assert data["conversation_tag"] == "leg-A"


async def test_query_tag_is_reflected(http_client):
    async with http_client(tag="B") as client:
        data = await call(client, "whoami")
    assert data["query"] == {"tag": "B"}


async def test_the_two_legs_are_distinguishable(http_client):
    # The whole point of the tag: each leg proves which leg it is
    # against the same URL.
    async with http_client(headers={"X-Conversation-Tag": "leg-A"}) as a:
        leg_a = await call(a, "whoami")
    async with http_client(headers={"X-Conversation-Tag": "leg-B"}) as b:
        leg_b = await call(b, "whoami")

    assert leg_a["conversation_tag"] == "leg-A"
    assert leg_b["conversation_tag"] == "leg-B"
    assert leg_a["conversation_tag"] != leg_b["conversation_tag"]


async def test_an_untagged_client_reports_no_leg(http_client):
    async with http_client() as client:
        data = await call(client, "whoami")
    assert data["conversation_tag"] == ""


# ------------------------------------------------- end-to-end over HTTP


async def test_full_wiring_report_sequence_over_http(http_client):
    # The exact sequence the `wiring_report` prompt tells a client to run.
    async with http_client(headers={"User-Agent": "wiring-check/1.0"}) as client:
        assert (await call(client, "ping"))["message"] == "pong"
        assert (await call(client, "echo", {"text": "test"}))["reversed"] == "tset"
        assert (await call(client, "whoami"))["user_agent"] == "wiring-check/1.0"

        results = (await call(client, "search", {"query": "anything"}))["results"]
        assert results

        fetched = await call(client, "fetch", {"id": "doc-1"})
        assert "TESTY-OK-1" in fetched["text"]


# ------------------------------------------- initialize-time identity


async def test_initialize_is_logged_with_the_client_identity(http_client, caplog):
    # The ChatGPT gap: openai-mcp/1.0.0 never sends the
    # MCP-Protocol-Version header, so whoami reports "" for it. The
    # initialize request carries the version and the client's own name
    # regardless, and on a stateless server this log line is the only
    # place either is ever visible.
    with caplog.at_level(logging.INFO, logger="testy"):
        async with http_client() as client:
            await call(client, "ping")

    records = [r.getMessage() for r in caplog.records if r.getMessage().startswith("INIT ")]
    assert records, "initialize was not logged"

    rec = json.loads(records[-1].removeprefix("INIT "))
    assert rec["protocol_version"], "protocol version missing from the INIT record"
    assert rec["client_name"], "client name missing from the INIT record"
    assert "http" in rec


async def test_whoami_redacts_the_forwarded_client_ip(http_client):
    async with http_client(headers={"X-Forwarded-For": "203.0.113.7, 9.129.58.33"}) as client:
        data = await call(client, "whoami")
    assert "203.0.113.7" not in data["x_forwarded_for"]
    assert data["x_forwarded_for"].startswith("<redacted>")


# ------------------------------------------- resource and prompt probes


async def test_reading_the_resource_is_logged(http_client, caplog):
    # These two probes exist to reveal which clients surface resources
    # and prompts — and until now they were the only things in the
    # server that left no trace, so the question was unanswerable from
    # the server side.
    with caplog.at_level(logging.INFO, logger="testy"):
        async with http_client() as client:
            await client.read_resource("testy://readme")

    records = [json.loads(r.getMessage().removeprefix("CALL "))
               for r in caplog.records if r.getMessage().startswith("CALL ")]
    resources = [r for r in records if r["kind"] == "resource"]
    assert resources, "reading the resource logged nothing"
    assert resources[-1]["name"] == "testy://readme"


async def test_getting_the_prompt_is_logged(http_client, caplog):
    with caplog.at_level(logging.INFO, logger="testy"):
        async with http_client() as client:
            await client.get_prompt("wiring_report", {})

    records = [json.loads(r.getMessage().removeprefix("CALL "))
               for r in caplog.records if r.getMessage().startswith("CALL ")]
    prompts = [r for r in records if r["kind"] == "prompt"]
    assert prompts, "getting the prompt logged nothing"
    assert prompts[-1]["name"] == "wiring_report"


async def test_tool_calls_are_logged_as_tools(http_client, caplog):
    # The kind field is what makes the three distinguishable in a log.
    with caplog.at_level(logging.INFO, logger="testy"):
        async with http_client() as client:
            await call(client, "ping")

    records = [json.loads(r.getMessage().removeprefix("CALL "))
               for r in caplog.records if r.getMessage().startswith("CALL ")]
    tools = [r for r in records if r["kind"] == "tool"]
    assert tools, "the tool call logged nothing"
    assert tools[-1]["name"] == "ping"


# ------------------------------------------------------------ body tap
# The INIT line is recovered off the wire now, so the tap sits in front of
# every POST. It must be impossible for it to break a request.


def _post(http_base, content, path="/mcp"):
    return httpx.post(
        f"{http_base}{path}",
        content=content,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=10,
    )


def test_malformed_json_still_reaches_the_app(http_base, caplog):
    with caplog.at_level(logging.INFO, logger="testy"):
        response = _post(http_base, b"{not json at all")
    # The app rejects it on its own terms; the tap must not be what fails.
    assert response.status_code < 500
    assert not [r for r in caplog.records if r.getMessage().startswith("INIT ")]


def test_a_batch_containing_initialize_is_logged(http_base, caplog):
    batch = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "batched-client", "version": "9.9"},
        }},
    ]).encode()
    with caplog.at_level(logging.INFO, logger="testy"):
        _post(http_base, batch)

    records = [json.loads(r.getMessage().removeprefix("INIT "))
               for r in caplog.records if r.getMessage().startswith("INIT ")]
    assert records, "initialize inside a batch was not logged"
    assert records[-1]["client_name"] == "batched-client"


def test_a_body_over_the_cap_is_passed_through_untapped(http_base, caplog):
    oversized = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"pad":"'
    oversized += b"x" * (testy_common.BODY_TAP_LIMIT + 1024) + b'"}}'
    with caplog.at_level(logging.INFO, logger="testy"):
        response = _post(http_base, oversized)
    assert response.status_code < 500
    # Over the cap nothing is buffered, so nothing is logged — by design.
    assert not [r for r in caplog.records if r.getMessage().startswith("INIT ")]


def test_a_non_initialize_post_logs_no_init(http_base, caplog):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    with caplog.at_level(logging.INFO, logger="testy"):
        _post(http_base, body)
    assert not [r for r in caplog.records if r.getMessage().startswith("INIT ")]


def test_a_post_outside_mcp_is_not_tapped(http_base, caplog):
    with caplog.at_level(logging.INFO, logger="testy"):
        _post(http_base, b'{"method":"initialize"}', path="/healthz")
    assert not [r for r in caplog.records if r.getMessage().startswith("INIT ")]


@pytest.mark.parametrize("body", [b"", b"[]", b"null", b'{"method":"initialize"}'])
def test_log_initialize_never_raises(body):
    # Called inside the tap's try/except, but it should not need it.
    testy_common.log_initialize({"path": "/mcp", "headers": [], "query_string": b""}, body)


async def test_a_raising_tap_does_not_fail_the_request(http_base, monkeypatch):
    # The guarantee: a logging failure must never fail a request.
    def boom(scope, body):
        raise RuntimeError("tap exploded")

    # The middleware holds on_body by reference, so patch where it looks.
    for middleware in testy.app.user_middleware:
        if middleware.cls is testy_common.BodyTap:
            monkeypatch.setitem(middleware.kwargs, "on_body", boom)
            break
    else:
        pytest.fail("BodyTap is not installed on the app")

    # user_middleware is read when the stack is built, so rebuild it.
    testy.app.middleware_stack = testy.app.build_middleware_stack()
    try:
        response = _post(http_base, json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "c", "version": "1"}},
        }).encode())
        assert response.status_code == 200
    finally:
        testy.app.middleware_stack = testy.app.build_middleware_stack()
