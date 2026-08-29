# <span style="color:#2e86c1">Testy</span> — MCP wiring tester

One no-auth, stateless, Streamable HTTP MCP server whose only job is to
prove a client is wired up, and to identify **which** client connected.

**Deploy your own** — it takes one `fly deploy` and the whole point is
that the endpoint is yours. See [Deploy](#deploy) below. Your endpoint is
then `https://<your-app>.fly.dev/mcp`, health at `/healthz`.

There is a hosted instance at `https://testy-foxxelabs.fly.dev/mcp` you
can point a client at to try it. Treat it as a best-effort demo: it is a
single scale-to-zero machine, it is unauthenticated, it logs every call
it receives, and it may change or disappear without notice. Do not build
anything on it.

## <span style="color:#2e86c1">Tools</span>

| Tool | Proves |
|---|---|
| `ping` | connect → list → call round trip |
| `echo(text)` | argument marshalling both directions |
| `whoami` | client identity: User-Agent, `MCP-Protocol-Version`, session id, origin, conversation tag — each reported only if the client actually sends it. Session id is blank by design (stateless, so none is ever issued). ChatGPT sends no protocol-version header, so that field is blank for it too — see the `INIT` log line below |
| `search(query)` / `fetch(id)` | ChatGPT deep-research tool-shape requirement (read-only, canned corpus with `TESTY-OK-*` markers) |
| resource `testy://readme` | whether the client surfaces MCP resources — reads are logged, so this is answerable server-side |
| prompt `wiring_report` | whether the client surfaces MCP prompts — likewise logged |

Every call is logged server-side with the client fingerprint —
`flyer:app_logs` on `testy-foxxelabs` shows the server's view of each
wiring attempt. Each record carries a `kind` of `tool`, `resource` or
`prompt`, so a log answers not just which tools a client called but
whether it ever surfaced the resource and prompt at all.

ChatGPT forwards the end user's real IP as the leftmost
`X-Forwarded-For` hop. Testy replaces it with `<redacted>` before
logging or returning it — it identifies clients, it does not collect
addresses. The proxy hops are kept, so the request path is still
visible.

Each connection also logs one `INIT` line carrying what the client
declared at `initialize`: the protocol version and its own name and
version. That is the only place a stateless server ever sees either,
and it is how you identify a client like ChatGPT that sends no
`MCP-Protocol-Version` header on later requests.

## <span style="color:#2e86c1">Wiring checklist per client</span>

ChatGPT has been walked end to end; Claude has been walked as far as the
handshake. The rest are written from docs — which is how the
ChatGPT entry was written too, and every menu name in it turned out to
be wrong. Both clients that have been checked had moved their menus.
Treat the unverified ones accordingly.

Illustrated version, with what ChatGPT was observed to do once
connected: <https://claude.ai/code/artifact/0e8d8e49-d9b7-48f0-a2c4-609851454176>

### ChatGPT — verified 2026-08-28
Custom MCP servers live under **Plugins**, not "Connectors". The rename
is why older instructions dead-end.

1. Developer mode: Settings → Security and login → Advanced security →
   Developer mode. Flagged *elevated risk*; the same switch also sits at
   the bottom of Settings → Plugins.
2. `chatgpt.com/plugins` → the **+** beside the search box.
3. Name `Testy`, Connection **Server URL**, your `/mcp` URL,
   Authentication **No Auth** — it defaults to OAuth.
4. Tick "I understand and want to continue", then Create.
5. Per conversation: composer **+** → Testy. The connector is invisible
   to the model until this is done, and it is the step people miss.

It identifies as `openai-mcp/1.0.0`, sends no `MCP-Protocol-Version`
header, initializes once per connection rather than per call, forwards
the end user's IP as the leftmost `X-Forwarded-For` hop, adds
`display_url`/`display_title` to search results, and surfaces neither
resources nor prompts. It caches the tool manifest — hit **Refresh** in
the plugin panel after changing anything the server advertises. For the
deep-research / data-only path, `search` + `fetch` are the two tools it
will use.

### Gemini — unverified
Web app → connected/custom apps → add MCP server URL. A connected custom
app then works in Spark on web and mobile. Gemini CLI: register it as an
HTTP server (`httpUrl`, which is distinct from the SSE and stdio forms —
check current CLI docs). Gemini Enterprise wants OAuth or a GCP
service-account token, so it is out of scope for a no-auth probe.

### Claude — verified 2026-08-29
Settings → Connectors now only says *"Connectors have moved to
Customize"*. The real path:

1. Customize → Connectors → **Add connector** → **Add custom connector**.
2. Step 1 of 2: Name, and Remote MCP server URL.
3. Step 2 of 2: Authentication is a radio group — Always required /
   Required when the server asks / **None**. Pick None. The trust notice
   ("anyone with access to the server URL will be able to use this
   connector") is informational, with no box to tick.
4. Enable it per conversation from the composer's **+**, as with ChatGPT.

Claude arrives as **two different callers**, which is worth knowing
before you read a log. Registering the connector handshakes from a
backend — User-Agent `python-httpx/…`, `clientInfo` name `Anthropic`.
Actual tool calls from a conversation come from User-Agent
`Claude-User`. Do not identify Claude from the registration request.

It **does** send the `MCP-Protocol-Version` header (`2025-11-25`), which
ChatGPT never does, so `whoami` reports a protocol version for Claude
and blank for ChatGPT.

### Dual-conversation clients — unverified
Point each conversation leg at the same `/mcp` URL with header
`X-Conversation-Tag: leg-A` / `leg-B` (or `?tag=`). `whoami` reflects it
back, so each leg can prove which leg it is, and the server log shows
both legs interleaved.

## <span style="color:#2e86c1">What the probes have measured</span>

Measured server-side, from the log — not from what a client said about
itself. Both clients were asked directly to read `testy://readme` and use
`wiring_report`; neither request ever reached the server.

| | ChatGPT | Claude |
|---|---|---|
| User-Agent on tool calls | `openai-mcp/1.0.0` | `Claude-User` |
| User-Agent at registration | same | `python-httpx/…` — different caller |
| `clientInfo` at `initialize` | `openai-mcp` / `1.0.0` | `Anthropic` / `1.0.0` |
| `MCP-Protocol-Version` header | never sent | `2025-11-25` |
| initializes | per connection, not per call | per connection |
| surfaces resources | **no** — never requested | **no** — never requested |
| surfaces prompts | **no** — never requested | **no** — never requested |
| rewrites `search` results | adds `display_url`, `display_title` | no |

So the two capability probes have their answer: **neither major client
exposes MCP resources or prompts from a custom connector.** Both were
explicit about it when asked, and the log agrees — the absence is a
request never made, not a request that failed. Claude went as far as
trying `fetch(id="readme")` as a workaround, which is in the log as a
tool call with those arguments.

The identity findings are the argument for `whoami` reporting every
field rather than trusting one: ChatGPT is identifiable by User-Agent
and gives nothing in the protocol header, Claude is the reverse at
registration time, and neither populates the session id.

## <span style="color:#2e86c1">Deploy</span>

Set `app` in `fly.toml` to your own name first, then:

```
fly apps create <your-app>
fly deploy
curl https://<your-app>.fly.dev/healthz
```

No secrets, no volumes, no database — there is nothing else to set up.

## <span style="color:#2e86c1">Tests</span>

```
pip install -r requirements-dev.txt
pytest
```

`tests/test_tools.py` drives the server in-memory (tool list, `echo`
marshalling, the `search`→`fetch` round trip, the resource and prompt
probes). `tests/test_http.py` runs the real ASGI app under uvicorn on an
ephemeral port, because `whoami` reads HTTP headers — that is where the
conversation tags and `/healthz` are checked.

## <span style="color:#2e86c1">Scope guard</span>

No auth, no data, no state. This is a probe, not a template. Anything
real gets built on foxxe-mcp with its auth layer.
