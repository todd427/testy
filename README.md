# <span style="color:#2e86c1">Testy</span> — MCP wiring tester

One no-auth, stateless, Streamable HTTP MCP server whose only job is to
prove a client is wired up, and to identify **which** client connected.

Endpoint after deploy: `https://testy-foxxelabs.fly.dev/mcp`
Health: `https://testy-foxxelabs.fly.dev/healthz`

## <span style="color:#2e86c1">Tools</span>

| Tool | Proves |
|---|---|
| `ping` | connect → list → call round trip |
| `echo(text)` | argument marshalling both directions |
| `whoami` | client identity: User-Agent, `MCP-Protocol-Version`, session id, origin, Beirt leg tag — each reported only if the client actually sends it. Session id is blank by design (stateless, so none is ever issued). ChatGPT sends no protocol-version header, so that field is blank for it too — see the `INIT` log line below |
| `search(query)` / `fetch(id)` | ChatGPT deep-research tool-shape requirement (read-only, canned corpus with `TESTY-OK-*` markers) |
| resource `testy://readme` | whether the client surfaces MCP resources |
| prompt `wiring_report` | whether the client surfaces MCP prompts |

Every call is logged server-side with the client fingerprint —
`flyer:app_logs` on `testy-foxxelabs` shows the server's view of each
wiring attempt.

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

### ChatGPT
Settings → enable Developer Mode (Plus/Pro/Business/Enterprise/Edu) →
add connector with the `/mcp` URL, auth: none. Then in a chat, enable
the app and run the `wiring_report` sequence. For the deep-research /
data-only path, `search` + `fetch` are the two tools it will use.

### Gemini
Web app → connected/custom apps → add MCP server URL. A connected
custom app then works in Spark on web and mobile. Gemini CLI:
add to `mcp.json` as an HTTP server. Gemini Enterprise wants OAuth or a
GCP service-account token — out of scope for this tester.

### Claude
Settings → Connectors → add custom connector, `/mcp` URL, no auth.

### Beirt
Point each conversation leg at the same `/mcp` URL with header
`X-Beirt-Conversation: leg-A` / `leg-B` (or `?tag=`). `whoami` reflects
it back, so each leg can prove which leg it is, and the server log
shows both legs interleaved.

## <span style="color:#2e86c1">Deploy</span>

```
fly deploy
curl https://testy-foxxelabs.fly.dev/healthz
```

## <span style="color:#2e86c1">Tests</span>

```
pip install -r requirements-dev.txt
pytest
```

`tests/test_tools.py` drives the server in-memory (tool list, `echo`
marshalling, the `search`→`fetch` round trip, the resource and prompt
probes). `tests/test_http.py` runs the real ASGI app under uvicorn on an
ephemeral port, because `whoami` reads HTTP headers — that is where the
Beirt leg tags and `/healthz` are checked.

## <span style="color:#2e86c1">Scope guard</span>

No auth, no data, no state. This is a probe, not a template. Anything
real gets built on foxxe-mcp with its auth layer.
