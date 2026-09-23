# <span style="color:#c9a84c">CC Brief — Migrate Testy to foxxe-mcp</span>

<span style="color:#888">**Repo:**</span> `todd427/testy`
<span style="color:#888">**Branch:**</span> `main`
<span style="color:#888">**Date:**</span> 23 September 2026
<span style="color:#888">**Precedes:**</span> `docs/BRIEF-oauth-probe.md` — the probe is built on the result of this migration. Do this first, deploy it, verify it, then start the probe.
<span style="color:#888">**Follows:**</span> `todd427/foxxe-mcp:docs/migration.md` (the fleet conversion guide). Where this brief is silent, that guide applies.

---

## <span style="color:#c9a84c">0. Why</span>

1. **Testy is outside the fleet bound.** `requirements.txt` declares `fastmcp>=3.4,<4`. foxxe-mcp's `FASTMCP_SPEC` is `>=2.14,<3` (`src/foxxe_mcp/_sdk.py`) — the "PRD finding B" hazard: a standalone `fastmcp` that can pull in an `mcp` the fleet cannot drive. rialu ran exactly this (`mcp 1.29.1 + fastmcp 3.4.7`) until its conversion.
2. **Testy should see what a fleet server sees.** Its job is to prove how a client behaves against *our* servers. Every fleet server (12 of 12 in `foxxe-mcp:fleet.toml`) runs the SDK's `MCPServer` through foxxe-mcp; Testy runs a different framework with different transport, header handling and metadata. Observations made on Testy transfer to the fleet only if Testy runs the same stack.
3. **The OAuth probe needs it.** The probe must exercise the same SDK authorization-server code Mnemos runs (`auth_server_provider=` / `AuthSettings` passed through `MCPServer`, per foxxe-mcp PRD OQ2).

**Decision: go to the SDK `MCPServer`, not the `foxxe-mcp[fastmcp]` extra.** The extra would mean downgrading to fastmcp 2.14.x and still running a non-fleet framework; the fleet has already dropped the extra everywhere (flyer, rialu). Cost, stated: the two fastmcp-only conveniences Testy uses — `get_http_headers()` and `Middleware.on_initialize` — have to be rebuilt on SDK primitives (§3.3, §3.4).

---

## <span style="color:#c9a84c">1. Verified facts this brief relies on</span>

Checked 2026-09-23. Re-check only if the pin moves.

- **foxxe-mcp on PyPI:** latest `0.5.4` (releases 0.3.0, 0.4.1, 0.5.0–0.5.4). `requires_dist`: `mcp<2,>=1.29.1`, `starlette>=0.36.0`, `uvicorn>=0.27.0`, `httpx>=0.27.0`, `pydantic>=2.0`. Public API identical to the repo HEAD (`MCPServer`, `Context`, `build_app`, `serve`, `setup_logging`, `health_route`, `version_route`, …).
- **mcp v1.29.1, `server/fastmcp/server.py`:** `tool()`, `resource()`, `prompt()`, `custom_route()` decorators all *return the original function*, so one plain function can be registered on two server instances (needed by the probe). `tool()` accepts `annotations: ToolAnnotations`.
- **mcp v1.29.1, streamable HTTP:** the transport attaches the Starlette `Request` as `ServerMessageMetadata(request_context=request)`; the lowlevel server passes it into `RequestContext(request=...)`. A tool taking `ctx: Context` reads it as `ctx.request_context.request`.
- **mcp v1.29.1, stateless sessions:** `ServerSession(stateless=True)` starts in `InitializationState.Initialized`; `client_params` is set only while handling an actual `initialize` request, in that request's own throwaway session. No server-side hook exposes it. → the `INIT` log must be taken at the ASGI layer (§3.4).
- **mcp v1.29.1, `shared/memory.py`:** `create_connected_server_and_client_session` exists (in-memory test transport).
- **foxxe-mcp `serve()` / `build_app()`:** default port `$PORT` or **8080** (Testy's `fly.toml` routes to 8000); `/health` and `/version` added automatically, prepended before the SDK catch-all; DNS-rebinding protection off by default since 0.5.0.

---

## <span style="color:#c9a84c">2. Files</span>

| File | Change |
|---|---|
| `requirements.txt` | Replace both lines with `foxxe-mcp==0.5.4`. No `fastmcp`, no `mcp`, no `uvicorn` (all transitive). |
| `requirements-dev.txt` | Remove any `fastmcp` line. Keep pytest / pytest-asyncio / httpx as they are. |
| `testy_common.py` (new) | Fingerprint, XFF redaction, the ASGI body tap / `INIT` logger, the probe tool functions. Shared with the probe later. |
| `server.py` | `MCPServer` + tools + `build_app()`; `serve()` in `__main__`. |
| `Dockerfile` | `COPY *.py ./` instead of `COPY server.py .`; `EXPOSE 8080`. CMD unchanged (`python server.py` → `serve()`). |
| `fly.toml` | `internal_port = 8080`; health-check path `/health`. |
| `tests/conftest.py`, `tests/test_*.py` | fastmcp `Client` → SDK client (§4). |
| `README.md` | Stack line, health/version paths. |

---

## <span style="color:#c9a84c">3. Code</span>

### <span style="color:#5b8def">3.1 Server</span>

```python
from foxxe_mcp import MCPServer, Context, build_app, serve
from mcp.types import ToolAnnotations

mcp = MCPServer(SERVER_NAME, instructions=...)   # no host=, no stateless_http= here (migration.md §2)
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
```

Tool, resource and prompt bodies and return shapes stay as they are (`ping`, `echo`, `whoami`, `search`, `fetch`, `testy://readme`, `wiring_report`). Syntax changes only: `@mcp.tool(annotations=READ_ONLY)`, `@mcp.resource("testy://readme")`, `@mcp.prompt()` (the SDK wants the parentheses).

Keep `SERVER_NAME = "testy"`; bump `VERSION` to `0.2.0`.

### <span style="color:#5b8def">3.2 Structured output</span>

Tools return `dict`; the SDK emits it as `structuredContent` plus a JSON text block. ChatGPT's deep-research path reads `search`/`fetch` results — verify it still accepts them in §6 rather than assuming the shape survived the framework change.

### <span style="color:#5b8def">3.3 `whoami` without fastmcp</span>

`_client_fingerprint(request)` takes a Starlette `Request` (or `None`) and reads `request.headers`, `request.url.path`, `request.query_params`. Same keys as today, same XFF redaction. The `mcp-session-id` strip-list workaround is fastmcp-specific and goes away; keep the test that guards the field (§4).

```python
@mcp.tool(annotations=READ_ONLY)
def whoami(ctx: Context) -> dict:
    req = ctx.request_context.request
    _log_call("whoami", request=req)
    return _client_fingerprint(req)
```

`_log_call` takes the request the same way. Tools that don't declare `ctx` today (`ping`, `echo`, `search`, `fetch`) gain `ctx: Context` so their `CALL` lines keep the fingerprint; the SDK injects it and it is not part of the tool's input schema.

### <span style="color:#5b8def">3.4 `INIT` logging — pure-ASGI body tap</span>

Replaces `InitializeLogger(Middleware)`. In `testy_common.py`:

- `BodyTap(app, *, on_body)`: for `http` scope with method `POST`, drain `receive` until `more_body` is false, buffering up to **1 MiB** (over the cap: stop buffering, pass the stream through untouched, log nothing). Call `on_body(scope, body_bytes)` inside `try/except Exception` — **a logging failure must never fail the request**. Then call `app` with a replacement `receive` that yields the buffered body as one `http.request` message, then delegates to the original `receive` (so disconnects still arrive).
- Pure ASGI, not `BaseHTTPMiddleware` — same reason as foxxe-mcp's `BearerAuthMiddleware` docstring: the streamable-HTTP response is long-lived and `BaseHTTPMiddleware` buffers it through an anyio stream.
- `log_initialize(scope, body)`: only for path `/mcp`. Parse JSON; accept an object or a batch list; for every element with `method == "initialize"`, log `INIT` with `protocol_version`, `client_name`, `client_version` from `params` / `params.clientInfo`, plus the fingerprint built from the scope's headers. Same record shape as today, so the existing test assertions hold.

The probe reuses `BodyTap` with a different `on_body` (§probe brief 2), so write it generically.

### <span style="color:#5b8def">3.5 Bootstrap</span>

```python
app = build_app(
    mcp,
    stateless_http=True,
    routes=[Route("/healthz", healthz, methods=["GET"])],   # one-release alias, see below
)
app.add_middleware(BodyTap, on_body=log_initialize)

if __name__ == "__main__":
    serve(mcp, app=app)
```

- `app` stays a module-level object because the tests import `server.app` (the ainm case in `serve()`'s docstring: build once, pass `app=`).
- `/health` and `/version` come from `build_app`. `/healthz` stays for one release returning its current body, in case anything external probes it; fly.toml moves to `/health`. Remove `/healthz` in the next Testy change after this one.
- Delete Testy's own `logging.basicConfig(...)`. `serve()` calls `setup_logging("testy")`. **Check** that `setup_logging`'s JSON formatter still leaves `record.getMessage()` as `"CALL {...}"` / `"INIT {...}"` and that pytest's `caplog` still captures the `testy` logger (it installs handlers on the root; confirm it doesn't set `propagate=False` on named loggers). The log-shape tests are the check.

### <span style="color:#5b8def">3.6 Docstring</span>

Rewrite the `server.py` module docstring: stack is foxxe-mcp / SDK `MCPServer`; keep the targets list, the conversation-tag explanation, and "No auth, no data, no state. Do not grow this into a real service — that is foxxe-mcp's job." Add: the `INIT` line comes from an ASGI body tap because a stateless SDK session never exposes `clientInfo` to server code.

---

## <span style="color:#c9a84c">4. Tests</span>

Replace the fastmcp `Client` everywhere.

- **In-memory (`client` fixture):** `create_connected_server_and_client_session(testy.mcp._mcp_server)` from `mcp.shared.memory`. Reaching `_mcp_server` is acceptable in tests; if the attribute name differs on the installed version, find the lowlevel server the SDK's own tests use and note it.
- **HTTP (`http_client` factory):** `streamablehttp_client(url, headers=...)` from `mcp.client.streamable_http` + `ClientSession`, `initialize()` first. Keep the factory's `headers=` / `tag=` interface.
- **Result access:** fastmcp's `.data` becomes the SDK result's `structuredContent`. Add one small helper (e.g. `async def call(session, name, args) -> dict`) so test bodies change as little as possible. Resource and prompt tests use `session.read_resource` / `session.get_prompt`.
- **Keep every existing assertion** except where the framework change makes it untrue, and say which in the commit message. Expected survivors include: user-agent reflection, negotiated protocol version header, `path == "/mcp"`, `mcp-session-id` reflected when sent and `""` when not, no `mcp-session-id` on initialize, conversation tag via header and via `?tag=`, XFF redaction, `INIT`/`CALL` log records and `kind` field.
- **New:** `/health` 200 with `server == "testy"`; `/version` reports `foxxe_mcp`, `sdk`, and `fastmcp` null (the point of the migration); `/healthz` alias still 200; a `BodyTap` unit test — malformed JSON, a batch containing `initialize`, a body over the cap, and an `on_body` that raises — request still succeeds in every case; a non-initialize POST logs no `INIT`.

---

## <span style="color:#c9a84c">5. Verify before deploying</span>

Per `foxxe-mcp:docs/migration.md` §5:

```bash
python -c "import server"
python -m pytest
PORT=8080 python server.py &  curl -s localhost:8080/version | jq   # fastmcp must be null
```

First read what production runs today: `fly ssh console --app testy-foxxelabs -C "pip show mcp fastmcp"` and record it in the commit message.

---

## <span style="color:#c9a84c">6. Deploy and verify in production</span>

```bash
fly deploy --remote-only --depot=false -a testy-foxxelabs
```

(Fleet notes: depot and `--local-only` have both failed from Todd's host; if remote-only fails with an h2c/grpc error, suspect builder rot first — `fly apps destroy <builder> --yes` — per the git-mcp entry in `fleet.toml`.)

The fleet lesson from ainm applies: **a conversion is not verified until a real client call succeeds** — `/health` and `/version` stay green through failures that break `/mcp`. Testy has no auth, so the real calls are:

1. `curl https://testy-foxxelabs.fly.dev/version` — `fastmcp` null, `sdk` 1.x.
2. **ChatGPT** (existing Testy connector, URL unchanged): run the `wiring_report` sequence. `search`/`fetch` must work (deep-research shape, §3.2).
3. **Claude** (connector): same sequence; also `testy://readme` and the prompt if the client surfaces them.
4. Fly logs show an `INIT` line for each with `client_name` populated (ChatGPT's is expected to identify as `openai-mcp`).

---

## <span style="color:#c9a84c">7. Record it</span>

In **`todd427/foxxe-mcp`**, add a `[[server]]` entry to `fleet.toml` — `name = "testy"`, `app = "testy-foxxelabs"`, `migrated = true`, `bound = true`, `shape = "serve"`, and a `note` in the house style: previous stack as read from the running image, the fastmcp 3.x bound breach this closed, the ASGI `INIT` tap and why, the port move 8000→8080, and what the production verification returned. Update the file's twelve-entries paragraph to thirteen. Separate commit, separate repo.

---

## <span style="color:#c9a84c">8. Checklist</span>

- [ ] Production image's `mcp`/`fastmcp` recorded before changing anything
- [ ] `requirements.txt` is exactly `foxxe-mcp==0.5.4`; no `fastmcp` in any requirements file
- [ ] `MCPServer`, `ToolAnnotations`, `@mcp.prompt()`; tool bodies and outputs unchanged
- [ ] `whoami` / `CALL` fingerprint from `ctx.request_context.request`
- [ ] `BodyTap` pure ASGI, 1 MiB cap, never fails a request; `INIT` records same shape as before
- [ ] `build_app()` + `serve(mcp, app=app)`; own `basicConfig` deleted; `/healthz` alias kept for one release
- [ ] Dockerfile `COPY *.py ./`, `EXPOSE 8080`; fly.toml `internal_port = 8080`, check on `/health`
- [ ] Tests on the SDK client, all prior assertions kept or explicitly retired; new `/version` and `BodyTap` tests
- [ ] Deployed; `/version` shows `fastmcp` null; ChatGPT and Claude connector calls succeed; `INIT` lines in Fly logs
- [ ] `foxxe-mcp:fleet.toml` entry added
