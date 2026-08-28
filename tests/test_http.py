"""The HTTP surface: health check, and the client identification that
only works when there is a real request to inspect.

`whoami` is the tool the README leans on to answer "which client is
actually connected", and it reads HTTP headers — so it can only be
tested honestly over the wire.
"""

import httpx
import server as testy


# ------------------------------------------------------------- health


def test_healthz_reports_server_identity(http_base):
    body = httpx.get(f"{http_base}/healthz").json()
    assert body == {"ok": True, "server": testy.SERVER_NAME, "version": testy.VERSION}


def test_healthz_is_the_shape_flys_check_expects(http_base):
    # fly.toml health-checks this path with a 5s timeout.
    response = httpx.get(f"{http_base}/healthz", timeout=5)
    assert response.status_code == 200


# ------------------------------------------------ client fingerprinting


async def test_whoami_reflects_the_user_agent(http_client):
    async with http_client(headers={"User-Agent": "testy-suite/1.0"}) as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["user_agent"] == "testy-suite/1.0"


async def test_whoami_reports_the_negotiated_protocol_version(http_client):
    async with http_client() as client:
        data = (await client.call_tool("whoami", {})).data
    # Set by the client on every post-initialize request; the value moves
    # with the spec, so assert it was negotiated at all.
    assert data["mcp_protocol_version"]


async def test_whoami_reports_the_request_path(http_client):
    async with http_client() as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["path"] == "/mcp"


async def test_whoami_can_see_the_session_id_header(http_client):
    # Guards the fix: FastMCP's get_http_headers() strips
    # `mcp-session-id` unless it is asked for by name. If someone drops
    # the `include=` argument, this is the test that catches it.
    async with http_client(headers={"mcp-session-id": "abc123"}) as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["mcp_session_id"] == "abc123"


async def test_whoami_reports_no_session_id_while_stateless(http_client):
    # Not a bug: a stateless server never issues a session, so a client
    # that was not given one has nothing to send. Documents why the
    # deployed server reports "" here.
    async with http_client() as client:
        data = (await client.call_tool("whoami", {})).data
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


# --------------------------------------------------------- Beirt legs


async def test_beirt_header_is_reflected(http_client):
    async with http_client(headers={"X-Beirt-Conversation": "leg-A"}) as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["beirt_conversation"] == "leg-A"


async def test_query_tag_is_reflected(http_client):
    async with http_client(tag="B") as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["query"] == {"tag": "B"}


async def test_the_two_legs_are_distinguishable(http_client):
    # The whole point of the Beirt path: each leg proves which leg it is
    # against the same URL.
    async with http_client(headers={"X-Beirt-Conversation": "leg-A"}) as a:
        leg_a = (await a.call_tool("whoami", {})).data
    async with http_client(headers={"X-Beirt-Conversation": "leg-B"}) as b:
        leg_b = (await b.call_tool("whoami", {})).data

    assert leg_a["beirt_conversation"] == "leg-A"
    assert leg_b["beirt_conversation"] == "leg-B"
    assert leg_a["beirt_conversation"] != leg_b["beirt_conversation"]


async def test_an_untagged_client_reports_no_leg(http_client):
    async with http_client() as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["beirt_conversation"] == ""


# ------------------------------------------------- end-to-end over HTTP


async def test_full_wiring_report_sequence_over_http(http_client):
    # The exact sequence the `wiring_report` prompt tells a client to run.
    async with http_client(headers={"User-Agent": "wiring-check/1.0"}) as client:
        assert (await client.call_tool("ping", {})).data["message"] == "pong"
        assert (await client.call_tool("echo", {"text": "test"})).data["reversed"] == "tset"
        assert (await client.call_tool("whoami", {})).data["user_agent"] == "wiring-check/1.0"

        results = (await client.call_tool("search", {"query": "anything"})).data["results"]
        assert results

        fetched = (await client.call_tool("fetch", {"id": "doc-1"})).data
        assert "TESTY-OK-1" in fetched["text"]
