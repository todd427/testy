# <span style="color:#c9a84c">CC Brief — Testy OAuth Probe (`testy-auth`)</span>

<span style="color:#888">**Repo:**</span> `todd427/testy`
<span style="color:#888">**Branch:**</span> `main`
<span style="color:#888">**Date:**</span> 23 September 2026 (revised twice same day: rebuilt on foxxe-mcp / SDK `MCPServer`, stateless design withdrawn; then volume dropped — in-memory state on one machine. See §1.2)
<span style="color:#888">**Depends on:**</span> `docs/BRIEF-foxxe-mcp-migration.md` — **complete, deploy and verify that first.** This brief uses its `testy_common.py` (fingerprint, `BodyTap`, tool functions).
<span style="color:#888">**Origin:**</span> Mnemos brief `todd427/mnemos:docs/mnemos/cc_brief_oauth_owner_gate_and_provenance.md` (commits `9cd54ad`, `12f23a4`) leaves open what ChatGPT (and Gemini) actually send during OAuth. Testy is no-auth, so it never sees that handshake. This brief adds a probe that does.

---

## <span style="color:#c9a84c">0. Purpose and questions to answer</span>

Stand up a second, OAuth-protected Testy deployment whose only job is to **log what each MCP client does during OAuth, against the same SDK authorization-server code Mnemos runs.** It grants access to nothing but read-only probe tools, so auto-approving every client is harmless here (it is exactly what must never happen on Mnemos).

Questions it must answer, per client (ChatGPT, Claude, Gemini where possible):

1. **DCR registration body, raw** — `client_name`, `redirect_uris` (exact strings), `grant_types`, `response_types`, `token_endpoint_auth_method`, `scope`, anything else sent. Mnemos slugs `client_name` into provenance and needs the real ChatGPT value.
2. **Discovery order** — which `.well-known` URLs each client requests, in what order, including RFC 9728 path-suffixed variants and any 404s it tolerates.
3. **`/authorize` request** — `redirect_uri`, `scope`, `resource` (RFC 8707), `code_challenge_method`, presence of `state`.
4. **`/token` request** — `grant_type`, `resource`, `redirect_uri`, client authentication actually used (the SDK issues a secret and defaults to `client_secret_post` unless the client registers `none` — see §1.2), refresh behaviour.
5. **RFC 9207 effect** — with `iss` advertised and sent on the authorization response, does ChatGPT switch from `https://chatgpt.com/connector/oauth/{callback_id}` to the stable `https://chatgpt.com/connector_platform_oauth_redirect`? (OpenAI Apps SDK auth docs, `developers.openai.com/plugins/build/auth`, say it does; observe it.)
6. **MCP-level identity after auth** — `initialize` `clientInfo.name/version` for the same client, so we know whether it matches the DCR `client_name` (different fields).
7. **Recovery from server-side state loss** — when the probe's machine auto-stops, its OAuth state is gone (§1.2). What does the client do with a token the server no longer recognises: try the refresh token, re-register, or surface a reconnect prompt to the user?

Because the probe runs the same SDK and foxxe-mcp as Mnemos, answers to 1–6 transfer to Mnemos directly — including any registration the SDK itself rejects before the provider sees it. Answer 7 tells us what a Mnemos OAuth-state loss would look like to each client.

---

## <span style="color:#c9a84c">1. Architecture — do not deviate without flagging</span>

### <span style="color:#5b8def">1.1 Separate Fly app, same repo</span>

- New entrypoint `probe.py`; new config `fly.probe.toml` for app **`testy-auth-foxxelabs`** (`lhr`, `shared-cpu-1x`, 256 MB, `internal_port = 8080`, `auto_stop_machines = "stop"`, `min_machines_running = 0`, health check `/health`, **no volume**, `[processes] app = "python probe.py"`).
- **Exactly one machine.** First deploy with `--ha=false`: Fly otherwise creates two machines on an app's first deploy (a standby for availability); later deploys update existing machines and do not add one. Why it matters is in §1.2.
- The migrated `testy-foxxelabs` app and its no-auth `/mcp` are **unchanged**. ChatGPT's existing Testy connection must keep working.
- Why not a second path on the same app: an `MCPServer` carries one auth configuration, and OAuth discovery lives at root `/.well-known/...`. Putting an authorization server on the no-auth host would change the discovery behaviour every no-auth client sees — contaminating what Testy exists to observe.

### <span style="color:#5b8def">1.2 State: in memory, one machine, no volume</span>

**The probe must hold registration state.** Verified against mcp v1.29.1 `server/auth/handlers/register.py`: the SDK's `RegistrationHandler` generates `client_id = str(uuid4())` itself (and a `client_secret` via `secrets.token_hex(32)` unless the client registers `token_endpoint_auth_method = "none"`, defaulting a missing method to `client_secret_post`), then hands the finished record to `provider.register_client()`. The provider cannot choose or encode the id, so `get_client()` has to look it up. This is why the first draft's signed, self-contained artefacts were withdrawn.

**It does not need to persist that state.** Where the provider keeps it is invisible to every client: the handlers, metadata, error bodies and redirects are the SDK's either way. So `ProbeOAuthProvider` keeps four plain dicts — `clients`, `codes`, `tokens`, `refresh` — with the same methods and semantics as `todd427/mnemos:server/oauth_provider.py`, minus `_load`/`_save`. No volume, no file.

**Why exactly one machine.** The dance is three separate requests — `/register`, `/authorize`, `/token`. With two machines behind the Fly proxy, registration can land on one and authorization on the other, which has never seen that `client_id`; the SDK answers with an invalid-client error. That would be logged as client behaviour when it is our artefact. One machine removes the ambiguity. It is not about disk.

**What in-memory costs, and why that's acceptable here.** Auto-stop after idle wipes all four dicts. The client's next call then carries a token the server doesn't know: the SDK answers 401, a refresh attempt fails (`invalid_grant`), and the client must re-register. For a probe that is fine — and it is question 7, so log it rather than prevent it. Run each client's connect-and-call steps in one sitting (§4). If mid-session restarts ever become a nuisance, set `min_machines_running = 1` for the duration of a probing session; do not add a volume.

Differences from Mnemos's provider, and only these:

- In-memory state (above).
- `authorize()` auto-approves (Mnemos's will not, after its owner-gate brief) and appends `iss` when §1.4 is on.
- Allowed redirect hosts: **any https host, plus `localhost`/`127.0.0.1`** — the probe must not reject a client we want to observe. Log every redirect host. (Safe only because the token unlocks nothing.)
- Every provider method logs (§2).
- Short lifetimes: access token 1 h, refresh 7 d, code 5 min.
- PKCE and redirect-URI matching on `/token` are **done by the SDK's token handler** (it recomputes `sha256(code_verifier)` and compares redirect URIs). Do not reimplement either; just return a correct `AuthorizationCode` from `load_authorization_code`.

### <span style="color:#5b8def">1.3 Framework</span>

```python
from foxxe_mcp import MCPServer, build_app, serve
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

mcp = MCPServer(
    "testy-auth",
    instructions=...,
    auth_server_provider=ProbeOAuthProvider(issuer=ISSUER),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(ISSUER),
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]),
        required_scopes=["mcp"],
    ),
)
```

`ISSUER` = `https://testy-auth-foxxelabs.fly.dev` (env override `TESTY_PROBE_ISSUER` for local runs). This is the Mnemos constructor shape, carried through foxxe-mcp untouched (foxxe-mcp PRD OQ2). `ProbeOAuthProvider` subclasses the SDK's `OAuthAuthorizationServerProvider`, as Mnemos's does.

`valid_scopes=["mcp"]` means the SDK rejects a registration asking for other scopes — **that rejection is a finding, not a bug**; the raw body and the 400 are logged by the tap (§2). Do not widen it; Mnemos has the same setting.

### <span style="color:#5b8def">1.4 RFC 9207 toggle</span>

Env `TESTY_PROBE_ISS` (`0` default, `1` on). When on: advertise `authorization_response_iss_parameter_supported: true` in the authorization-server metadata and append `iss=<issuer>` to the redirect `authorize()` returns. The SDK builds the metadata (`server/auth/routes.py: build_metadata`) with no hook for extra fields: when the toggle is on, add a route for `/.well-known/oauth-authorization-server` **ahead of** the SDK's (via `build_app(routes=[...])`, which prepends) that returns the SDK's metadata plus the field. Say so in the commit message.

### <span style="color:#5b8def">1.5 Tools</span>

`ping`, `echo`, `whoami` — the same functions from `testy_common.py` that `server.py` registers (the SDK decorators return the original function, so registering them on a second instance is plain), plus one new tool:

- `oauth_whoami(ctx)` — what the server knows about the bearer's client: `client_name`, `redirect_uris`, `token_endpoint_auth_method`, `grant_types`, `client_id_issued_at`, token `scopes`, seconds to expiry, and **seconds since this process started** (so a client-side call after a state wipe is visibly a fresh process). Get the token via `mcp.server.auth.middleware.auth_context.get_access_token()` (the same call Mnemos's `_caller_source()` uses) and the client via the provider. Never return the token, the `client_id`, or a secret.

All with `ToolAnnotations(readOnlyHint=True, openWorldHint=False)`. No `search`/`fetch`/resource/prompt on the probe.

### <span style="color:#5b8def">1.6 Bootstrap</span>

```python
app = build_app(mcp, stateless_http=True, routes=iss_routes)   # iss_routes empty unless toggle on
app.add_middleware(BodyTap, on_body=log_probe_body)
app.add_middleware(HttpLogger)                                  # outermost: sees 404s too
if __name__ == "__main__":
    serve(mcp, app=app)
```

Log a `BOOT` line at startup (process start time, SDK and foxxe-mcp versions, iss toggle) so every state wipe is visible in the log stream next to the client behaviour that follows it.

---

## <span style="color:#c9a84c">2. Logging — the actual deliverable</span>

One line per event on logger `testy`, message `"<TAG> {json}"` (same convention as `CALL`/`INIT`, so `fly logs | grep` works under foxxe-mcp's JSON formatter):

| Tag | Source | Fields |
|---|---|---|
| `BOOT` | startup | process start time, `mcp` / `foxxe-mcp` versions, iss toggle |
| `HTTP` | `HttpLogger`, pure ASGI, outermost | method, path, query **keys** (values too for `.well-known`), response status, user-agent, redacted XFF |
| `REG` | `BodyTap` on `POST /register` | the **raw JSON body as received** (before the SDK parses or rejects it), plus the response status |
| `REG_OK` | `provider.register_client()` | issued `client_id` prefix (8 chars), `client_name`, `redirect_uris`, `token_endpoint_auth_method` as stored, whether a secret was issued |
| `AUTHZ` | `provider.authorize()` | `redirect_uri`, redirect host, `scopes`, `resource`, `state_present` (bool), `code_challenge` present (bool), `client_name`, iss toggle |
| `TOKEN` | `BodyTap` on `POST /token` + provider exchange methods | `grant_type`, `resource`, `redirect_uri`, client auth observed (none / `client_secret_post` / `client_secret_basic` — from body keys and the presence of a Basic `Authorization` header), `client_name` (or `unknown_client` when the id isn't in memory), response status |
| `UNKNOWN` | `load_access_token` / `load_refresh_token` / `get_client` miss | which lookup missed, `client_id` prefix (8 chars) — the state-loss signal for question 7 |
| `REVOKE` | `provider.revoke_token()` | token kind, `client_name` |
| `INIT` / `CALL` | as `server.py` | plus `client_name` resolved from the bearer |

**Never log:** authorization codes, access or refresh tokens, `code_verifier`, the `state` value, `client_secret`, full `client_id`, `Authorization` header values, the leftmost XFF hop. The `TOKEN` tap logs form **keys** plus the allow-listed values above — an allow-list, not a deny-list, so a field added by a client later can't leak. Redirect URIs, `resource`, scopes and `client_name` are fine — they are what we came for.

---

## <span style="color:#c9a84c">3. Tests</span>

`tests/test_probe.py`, driving `probe:app` with the SDK client pattern from the migration:

- Authorization-server metadata advertises `registration_endpoint` and `S256`; with the toggle on, also `authorization_response_iss_parameter_supported`, and the SDK's own fields are all still present.
- Full dance via httpx with PKCE: register → authorize (302 to redirect with `code` + `state`, and `iss` when toggled) → token → MCP `oauth_whoami` returns the registered `client_name` and `redirect_uris`. Run it twice: public client (`token_endpoint_auth_method = "none"`) and a client that omits the method (SDK issues a secret; exchange with `client_secret_post`).
- Refresh grant issues a new access token.
- **State loss behaves as the SDK would in production:** complete the dance, then rebuild the app with a fresh provider. The old access token on `/mcp` → 401 with `WWW-Authenticate`; the old refresh token on `/token` → `invalid_grant` (or whatever the SDK returns — assert the actual value and note it); `/authorize` with the old `client_id` → the SDK's invalid-client response; a fresh registration then completes. `UNKNOWN` lines are logged for each miss.
- Registration asking for scope `openid` → SDK 400, and a `REG` line records the raw body and the 400.
- `/mcp` without a bearer → 401 with `WWW-Authenticate` pointing at protected-resource metadata.
- Redaction: run the full dance under `caplog`; assert no code, access token, refresh token, verifier, `state` value, secret or full `client_id` appears in any record; assert `BOOT`, `HTTP`, `REG`, `REG_OK`, `AUTHZ`, `TOKEN` records exist and parse.
- `HttpLogger` records a 404 on an unknown `.well-known` path.
- **Regression:** the migrated `server.py` suite passes unchanged; `server:app` still serves `/mcp` with no auth.

---

## <span style="color:#c9a84c">4. Runbook (Todd, after CC ships)</span>

1. `fly apps create testy-auth-foxxelabs` ; `fly deploy -c fly.probe.toml --ha=false --remote-only --depot=false`. Confirm `fly machine list -a testy-auth-foxxelabs` shows one machine.
2. **ChatGPT, iss off:** Developer Mode → add custom connector `https://testy-auth-foxxelabs.fly.dev/mcp`, OAuth. Complete; call `oauth_whoami` and `whoami`. Do this in one sitting.
3. **State loss (question 7):** leave it until `fly machine list` shows the machine stopped, then call `oauth_whoami` from the same ChatGPT connector. Record what ChatGPT does and what the log shows (`BOOT`, `UNKNOWN`, then whatever follows).
4. **ChatGPT, iss on:** `fly secrets set TESTY_PROBE_ISS=1 -a testy-auth-foxxelabs`, remove and re-add the connector, repeat step 2.
5. **Claude:** add as a custom connector in claude.ai; call `oauth_whoami`; repeat step 3 for Claude. Like-for-like baseline against ChatGPT.
6. **Gemini:** if any Gemini surface supports remote OAuth MCP today, repeat; otherwise record "not attempted" and why.
7. Pull logs (`fly logs -a testy-auth-foxxelabs`, or flyer `app_logs`) filtered to `BOOT|HTTP|REG|AUTHZ|TOKEN|UNKNOWN|INIT`, and write **`docs/FINDINGS-oauth-probe.md`**: one section per client, answering §0 questions 1–7 with logged values verbatim.
8. Copy ChatGPT's `client_name` and redirect URI(s) into the Mnemos brief §F, and set Mnemos `_HOST_FAMILY` from observed hosts only.
9. Leave the app in place for re-runs; it scales to zero and holds nothing.

---

## <span style="color:#c9a84c">5. Docs to update</span>

- `README.md`: a "testy-auth (OAuth probe)" section — what it is, that it auto-approves by design, keeps OAuth state in memory only (lost on every stop, deliberately), the env vars, the runbook.
- `server.py` docstring: keep "Do not grow this into a real service"; one line noting the probe is a separate app (`probe.py`) and logging-only.
- `foxxe-mcp:fleet.toml`: add a `testy-auth` entry (`app = "testy-auth-foxxelabs"`, `migrated = true`, `shape = "serve"`, note: OAuth probe, auto-approve by design, one machine, in-memory OAuth state, no volume).

---

## <span style="color:#c9a84c">6. Out of scope</span>

- **Persistence.** No volume, no file, no external store. If a future probe run genuinely needs state to survive a stop, use `min_machines_running = 1` for that session.
- **CIMD** (Client ID Metadata Documents). Mnemos doesn't advertise it, so ChatGPT will use DCR against Mnemos; DCR is what to observe. CIMD would also mean fetching client-supplied URLs on this box.
- Consent screen, user identity, any data. If a later need pushes past that, it belongs in a real service, not Testy.

---

## <span style="color:#c9a84c">7. Checklist</span>

- [ ] Migration brief shipped and verified in production first
- [ ] `probe.py` + `fly.probe.toml` for `testy-auth-foxxelabs`, no volume, first deploy `--ha=false`, one machine confirmed; `testy-foxxelabs` untouched
- [ ] `MCPServer(auth_server_provider=..., auth=AuthSettings(...))` — Mnemos constructor shape
- [ ] `ProbeOAuthProvider`: Mnemos's methods and semantics, in-memory dicts, only the §1.2 differences; no PKCE reimplementation
- [ ] `TESTY_PROBE_ISS` toggle: metadata route prepended, `iss` on the redirect
- [ ] `BOOT` / `HTTP` / `REG` / `REG_OK` / `AUTHZ` / `TOKEN` / `UNKNOWN` / `REVOKE` / `INIT` / `CALL` lines; `TOKEN` tap is an allow-list; redaction rules enforced
- [ ] `oauth_whoami` via `get_access_token()`, includes process uptime, read-only annotations
- [ ] §3 tests pass, including the state-loss test, SDK-rejected registration, and the unchanged `server.py` suite
- [ ] README, docstring, `fleet.toml` entry
