"""Tools, resource, and prompt over the in-memory transport.

These assert the contract the README advertises per client: the five
tools, the ChatGPT deep-research `search`/`fetch` shape, and the two
capability probes.
"""

from datetime import datetime

import pytest
from fastmcp.exceptions import ToolError

import server as testy

DOCUMENTED_TOOLS = {"ping", "echo", "whoami", "search", "fetch"}

FINGERPRINT_KEYS = {
    "user_agent",
    "mcp_protocol_version",
    "mcp_session_id",
    "origin",
    "x_forwarded_for",
    "beirt_conversation",
}


# ------------------------------------------------------------ listing


async def test_lists_exactly_the_documented_tools(client):
    names = {t.name for t in await client.list_tools()}
    assert names == DOCUMENTED_TOOLS


async def test_every_tool_declares_itself_read_only(client):
    # Without these, a client assumes the worst: ChatGPT labelled `echo`
    # PUBLIC WRITE / OPEN WORLD / DESTRUCTIVE. `search` and `fetch` in
    # particular must read as read-only for the deep-research path.
    for tool in await client.list_tools():
        ann = tool.annotations
        assert ann is not None, f"{tool.name} declares no annotations"
        assert ann.readOnlyHint is True, tool.name
        assert ann.openWorldHint is False, tool.name


async def test_every_tool_describes_itself(client):
    # A wiring tester is useless if the client lists a tool with no
    # explanation of what proving it proves.
    for tool in await client.list_tools():
        assert tool.description and tool.description.strip(), tool.name


# --------------------------------------------------------------- ping


async def test_ping_identifies_the_server(client):
    data = (await client.call_tool("ping", {})).data
    assert data["message"] == "pong"
    assert data["server"] == testy.SERVER_NAME
    assert data["version"] == testy.VERSION


async def test_ping_time_is_tz_aware_utc(client):
    data = (await client.call_tool("ping", {})).data
    parsed = datetime.fromisoformat(data["time_utc"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


# --------------------------------------------------------------- echo


@pytest.mark.parametrize(
    "text",
    ["test", "", "a", "ünïcødé ✓", "  padded  ", "x" * 500, "line\nbreak"],
)
async def test_echo_round_trips(client, text):
    data = (await client.call_tool("echo", {"text": text})).data
    assert data["text"] == text
    assert data["reversed"] == text[::-1]
    assert data["length"] == len(text)


async def test_echo_rejects_a_missing_argument(client):
    # Argument marshalling has to fail loudly, or `echo` proves nothing.
    with pytest.raises(ToolError):
        await client.call_tool("echo", {})


# -------------------------------------------------------------- whoami


async def test_whoami_reports_every_fingerprint_field(client):
    data = (await client.call_tool("whoami", {})).data
    assert FINGERPRINT_KEYS <= set(data)


async def test_whoami_survives_having_no_http_request(client):
    # In-memory there are no headers; the tool must degrade to empty
    # strings rather than raise.
    data = (await client.call_tool("whoami", {})).data
    for key in FINGERPRINT_KEYS:
        assert data[key] == ""


# ------------------------------------------------------ search / fetch


async def test_search_returns_the_whole_corpus(client):
    results = (await client.call_tool("search", {"query": "anything"})).data["results"]
    assert {r["id"] for r in results} == set(testy._CORPUS)


async def test_search_results_carry_the_deep_research_fields(client):
    results = (await client.call_tool("search", {"query": "x"})).data["results"]
    assert results, "search must return at least one result"
    for result in results:
        assert {"id", "title", "url"} <= set(result)
        assert result["title"] and result["url"]


@pytest.mark.parametrize("doc_id,marker", [("doc-1", "TESTY-OK-1"), ("doc-2", "TESTY-OK-2")])
async def test_fetch_returns_the_marker(client, doc_id, marker):
    data = (await client.call_tool("fetch", {"id": doc_id})).data
    assert marker in data["text"]
    assert data["id"] == doc_id
    assert data["metadata"]["server"] == testy.SERVER_NAME


async def test_every_search_result_is_fetchable(client):
    # The actual deep-research contract: whatever search hands back,
    # fetch has to resolve.
    results = (await client.call_tool("search", {"query": "x"})).data["results"]
    for result in results:
        data = (await client.call_tool("fetch", {"id": result["id"]})).data
        assert data["text"], result["id"]
        assert data["title"] == result["title"]


async def test_fetch_of_an_unknown_id_degrades_quietly(client):
    # A client probing ids must get a shaped answer, not an error.
    result = await client.call_tool("fetch", {"id": "no-such-doc"})
    assert result.is_error is False
    assert result.data["title"] == "not found"
    assert result.data["text"] == ""


# --------------------------------------------------- capability probes


async def test_readme_resource_is_listed_and_readable(client):
    uris = {str(r.uri) for r in await client.list_resources()}
    assert "testy://readme" in uris
    contents = await client.read_resource("testy://readme")
    assert "TESTY-RESOURCE-OK" in contents[0].text


async def test_wiring_report_prompt_names_every_tool(client):
    names = {p.name for p in await client.list_prompts()}
    assert "wiring_report" in names
    rendered = (await client.get_prompt("wiring_report", {})).messages[0].content.text
    for tool in DOCUMENTED_TOOLS:
        assert tool in rendered, tool


# ------------------------------------------------ forwarded-for privacy


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("   ", ""),
        ("2a02:8086:cad::1", "<redacted>"),
        ("1.2.3.4, 9.129.58.33", "<redacted>, 9.129.58.33"),
        ("1.2.3.4, 9.129.58.33, 66.241.125.175", "<redacted>, 9.129.58.33, 66.241.125.175"),
        (" 1.2.3.4 ,  9.129.58.33 ", "<redacted>, 9.129.58.33"),
    ],
)
def test_forwarded_for_drops_the_originating_hop(raw, expected):
    assert testy._redact_forwarded_for(raw) == expected


def test_forwarded_for_never_echoes_the_client_address():
    # The point of the redaction: the end-user IP ChatGPT forwards must
    # not survive into anything the server returns or logs.
    assert "2a02:8086:cad::1" not in testy._redact_forwarded_for(
        "2a02:8086:cad::1, 9.129.58.33, 66.241.125.175"
    )
