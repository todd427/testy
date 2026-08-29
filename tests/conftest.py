"""Fixtures for the Testy suite.

Two ways in, matching the two things worth testing:

  `client`      — in-memory transport. Exercises the tool/resource/prompt
                  layer with no HTTP underneath.
  `http_client` — the real ASGI app under uvicorn on an ephemeral port.
                  Needed because `whoami` reports on HTTP headers, which
                  only exist on a genuine request.
"""

import socket
import threading
import time

import pytest
import pytest_asyncio
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

import server as testy


@pytest_asyncio.fixture
async def client():
    """MCP client wired straight to the server object, no network."""
    async with Client(testy.mcp) as c:
        yield c


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
    """Factory for an MCP client over real HTTP.

    `headers` and `tag` mirror how one leg of a dual-conversation
    client identifies itself: a header, or a `?tag=` on the URL.
    """

    def make(headers: dict | None = None, tag: str | None = None) -> Client:
        url = f"{http_base}/mcp"
        if tag is not None:
            url += f"?tag={tag}"
        return Client(StreamableHttpTransport(url, headers=headers or {}))

    return make
