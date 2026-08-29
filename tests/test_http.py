"""The HTTP surface: health check, and the client identification that
only works when there is a real request to inspect.

`whoami` is the tool the README leans on to answer "which client is
actually connected", and it reads HTTP headers — so it can only be
tested honestly over the wire.
"""

import json
import logging

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


# ------------------------------------------------- conversation legs


async def test_conversation_tag_header_is_reflected(http_client):
    async with http_client(headers={"X-Conversation-Tag": "leg-A"}) as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["conversation_tag"] == "leg-A"


async def test_query_tag_is_reflected(http_client):
    async with http_client(tag="B") as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["query"] == {"tag": "B"}


async def test_the_two_legs_are_distinguishable(http_client):
    # The whole point of the tag: each leg proves which leg it is
    # against the same URL.
    async with http_client(headers={"X-Conversation-Tag": "leg-A"}) as a:
        leg_a = (await a.call_tool("whoami", {})).data
    async with http_client(headers={"X-Conversation-Tag": "leg-B"}) as b:
        leg_b = (await b.call_tool("whoami", {})).data

    assert leg_a["conversation_tag"] == "leg-A"
    assert leg_b["conversation_tag"] == "leg-B"
    assert leg_a["conversation_tag"] != leg_b["conversation_tag"]


async def test_an_untagged_client_reports_no_leg(http_client):
    async with http_client() as client:
        data = (await client.call_tool("whoami", {})).data
    assert data["conversation_tag"] == ""


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


# ------------------------------------------- initialize-time identity


async def test_initialize_is_logged_with_the_client_identity(http_client, caplog):
    # The ChatGPT gap: openai-mcp/1.0.0 never sends the
    # MCP-Protocol-Version header, so whoami reports "" for it. The
    # initialize request carries the version and the client's own name
    # regardless, and on a stateless server this log line is the only
    # place either is ever visible.
    with caplog.at_level(logging.INFO, logger="testy"):
        async with http_client() as client:
            await client.call_tool("ping", {})

    records = [r.getMessage() for r in caplog.records if r.getMessage().startswith("INIT ")]
    assert records, "initialize was not logged"

    rec = json.loads(records[-1].removeprefix("INIT "))
    assert rec["protocol_version"], "protocol version missing from the INIT record"
    assert rec["client_name"], "client name missing from the INIT record"
    assert "http" in rec


async def test_whoami_redacts_the_forwarded_client_ip(http_client):
    async with http_client(headers={"X-Forwarded-For": "203.0.113.7, 9.129.58.33"}) as client:
        data = (await client.call_tool("whoami", {})).data
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
            await client.call_tool("ping", {})

    records = [json.loads(r.getMessage().removeprefix("CALL "))
               for r in caplog.records if r.getMessage().startswith("CALL ")]
    tools = [r for r in records if r["kind"] == "tool"]
    assert tools, "the tool call logged nothing"
    assert tools[-1]["name"] == "ping"
