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
| `whoami` | client identity: User-Agent, negotiated `MCP-Protocol-Version`, session id, origin, Beirt leg tag |
| `search(query)` / `fetch(id)` | ChatGPT deep-research tool-shape requirement (read-only, canned corpus with `TESTY-OK-*` markers) |
| resource `testy://readme` | whether the client surfaces MCP resources |
| prompt `wiring_report` | whether the client surfaces MCP prompts |

Every call is logged server-side with the client fingerprint —
`flyer:app_logs` on `testy-foxxelabs` shows the server's view of each
wiring attempt.

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

## <span style="color:#2e86c1">Scope guard</span>

No auth, no data, no state. This is a probe, not a template. Anything
real gets built on foxxe-mcp with its auth layer.
