"""Fixtures for the Testy suite.

Two ways in, matching the two things worth testing:

  `client`      — in-memory transport. Exercises the tool/resource/prompt
                  layer with no HTTP underneath.
  `http_client` — the real ASGI app under uvicorn on an ephemeral port.
                  Needed because `whoami` reports on HTTP headers, which
                  only exist on a genuine request.

Both are MCP SDK clients since the foxxe-mcp migration. `call()` papers
over the one shape change: fastmcp's `.data` is the SDK's
`structuredContent`.
"""

import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

import server as testy


async def call(session: ClientSession, name: str, args: dict | None = None) -> dict:
    """Call a tool and return its structured result."""
    result = await session.call_tool(name, args or {})
    return result.structuredContent


@pytest_asyncio.fixture
async def client():
    """MCP client wired straight to the server object, no network.

    The session is opened and closed inside one dedicated task. anyio
    cancel scopes must be exited in the task that entered them, and
    pytest-asyncio runs fixture setup and teardown in different tasks —
    entering it directly here raises "cancel scope in a different task".
    """
    ready, stop, holder = asyncio.Event(), asyncio.Event(), {}

    async def hold():
        try:
            async with create_connected_server_and_client_session(testy.mcp._mcp_server) as session:
                holder["session"] = session
                ready.set()
                await stop.wait()
        except Exception as exc:  # noqa: BLE001 — re-raised below, in the test's task
            holder["error"] = exc
            ready.set()

    task = asyncio.create_task(hold())
    await ready.wait()
    if "error" in holder:
        raise holder["error"]
    try:
        yield holder["session"]
    finally:
        stop.set()
        await task


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def http_base():
    """Serve `server.app` for the session; yields the base URL."""
    port = _free_port()
    config = uvicorn.Config(testy.app, host="127.0.0.1", port=port, log_level="error")
    server_ = uvicorn.Server(config)
    thread = threading.Thread(target=server_.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server_.started and time.time() < deadline:
        time.sleep(0.05)
    if not server_.started:
        raise RuntimeError("uvicorn did not start within 10s")

    yield f"http://127.0.0.1:{port}"

    server_.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def http_client(http_base):
    """Factory for an MCP session over real HTTP.

    `headers` and `tag` mirror how one leg of a dual-conversation
    client identifies itself: a header, or a `?tag=` on the URL.
    """

    @asynccontextmanager
    async def make(headers: dict | None = None, tag: str | None = None):
        url = f"{http_base}/mcp"
        if tag is not None:
            url += f"?tag={tag}"
        # The current client takes headers via the httpx client rather than a
        # headers= argument; the older streamablehttp_client is deprecated.
        async with httpx.AsyncClient(headers=headers or {}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    return make
