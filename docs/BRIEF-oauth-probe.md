# <span style="color:#c9a84c">CC Brief — Testy OAuth Probe (`testy-auth`)</span>

<span style="color:#888">**Repo:**</span> `todd427/testy`
<span style="color:#888">**Branch:**</span> `master`
<span style="color:#888">**Date:**</span> 23 September 2026
<span style="color:#888">**Origin:**</span> Chat session 2026-09-23. Mnemos brief `todd427/mnemos:docs/mnemos/cc_brief_oauth_owner_gate_and_provenance.md` (commits `9cd54ad`, `12f23a4`) leaves open what ChatGPT (and Gemini) actually send during OAuth. Testy is no-auth, so it never sees that handshake. This brief adds a probe that does.

---

## <span style="color:#c9a84c">0. Purpose and questions to answer</span>

Stand up a second, OAuth-protected Testy deployment whose only job is to **log what each MCP client does during OAuth**. It grants access to nothing but the same read-only probe tools, so auto-approving every client is harmless here (it is exactly what must never happen on Mnemos).

Questions it must answer, per client (ChatGPT, Claude, Gemini where possible):

1. **DCR registration body** — `client_name`, `redirect_uris` (exact strings), `grant_types`, `response_types`, `token_endpoint_auth_method`, `scope`, any other fields. Mnemos slugs `client_name` into provenance and needs the real ChatGPT value.
2. **Discovery order** — which `.well-known` URLs each client requests, in what order, including RFC 9728 path-suffixed variants and any 404s it tolerates.
3. **`/authorize` request** — `redirect_uri`, `scope`, `resource` (RFC 8707), `code_challenge_method`, presence of `state`, `client_id` shape.
4. **`/token` request** — `grant_type`, `resource`, `redirect_uri`, client authentication method actually used, refresh behaviour.
5. **RFC 9207 effect** — with `iss` on the authorization response advertised and sent, does ChatGPT switch from `https://chatgpt.com/connector/oauth/{callback_id}` to the stable `https://chatgpt.com/connector_platform_oauth_redirect`? (OpenAI Apps SDK auth docs, `developers.openai.com/plugins/build/auth`, say it does; observe it rather than assume.)
6. **MCP-level identity after auth** — `initialize` `clientInfo.name/version` for the same client, so we know whether it matches the DCR `client_name` (they are different fields).

---

## <span style="color:#c9a84c">1. Architecture — do not deviate without flagging</span>

### <span style="color:#5b8def">1.1 Separate Fly app, same repo</span>

- New entrypoint `probe.py`; new config `fly.probe.toml` for app **`testy-auth-foxxelabs`** (`lhr`, `shared-cpu-1x`, 256 MB, `min_machines_running = 0`, health check `/healthz`).
- The existing `testy-foxxelabs` app, `server.py`, and its `/mcp` no-auth endpoint are **unchanged in behaviour**. ChatGPT's existing Testy connection must keep working.
- Why not a second path in the same app: one FastMCP instance carries one auth configuration, and OAuth discovery lives at root `/.well-known/...`. Bolting an authed path onto the no-auth app would put authorization-server metadata on the host every no-auth client probes, contaminating the very discovery behaviour we're trying to observe.
- Same Dockerfile; `fly.probe.toml` overrides the process command to run `probe:app` under uvicorn. If the Dockerfile hardcodes the command, add `[processes] app = "uvicorn probe:app --host 0.0.0.0 --port 8000"` in `fly.probe.toml` rather than editing the Dockerfile's default.

### <span style="color:#5b8def">1.2 Stateless OAuth (signed artefacts)</span>

`fly.toml` scales to zero and has no volume; the existing test suite already documents that Fly may route consecutive requests to different machines. An in-memory client/code/token store would lose a registration between `/register`, `/authorize` and `/token`. So **every OAuth artefact is self-contained and HMAC-signed**; the server holds no OAuth state.

- Secret: Fly secret `TESTY_PROBE_SECRET` (32+ random bytes, base64). Refuse to start without it.
- Encoding: `base64url(zlib(json)) + "." + base64url(hmac_sha256(secret, kind || payload))` where `kind` is one of `client`, `code`, `access`, `refresh` — so an artefact of one kind can never be replayed as another.
- **client_id** = signed `{v:1, name, redirect_uris, grant_types, token_endpoint_auth_method, iat}`. `get_client()` verifies and decodes; no lookup. Log its length on every registration — if a client rejects a long `client_id`, that is itself a finding (record it; fallback noted in §6).
- **authorization code** = signed `{client_id_hash, redirect_uri, code_challenge, method, scopes, resource, exp: now+300}`.
- **access token** = signed `{client_id_hash, scopes, resource, exp: now+3600}`; **refresh token** = signed `{client_id_hash, scopes, resource, exp: now+30d}`. Issue both so refresh behaviour is observable.
- PKCE S256 enforced on the token exchange (verify against the code's `code_challenge`).
- No revocation list (stateless). `/revoke` returns 200 and logs the call; that's all.

### <span style="color:#5b8def">1.3 Framework</span>

`fastmcp>=3.4,<4` is already pinned. Implement the authorization server as a subclass of FastMCP's OAuth provider base (the FastMCP wrapper around the MCP SDK's `OAuthAuthorizationServerProvider`: `get_client`, `register_client`, `authorize`, `load_authorization_code`, `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`, `load_access_token`, `revoke_token`). **Verify the exact class name, import path, and constructor (issuer / base URL, client-registration options, required scopes) against the installed fastmcp version before writing code**; do not copy an API shape from memory. If FastMCP 3 no longer exposes a server-side provider base that supports DCR, stop and leave a TODO rather than hand-rolling the OAuth routes.

- `authorize()` auto-approves: log (§2), mint the signed code, return the client redirect with `code`, `state`, and (when §1.4 is on) `iss`.
- `register_client()`: log the **full inbound registration JSON** (§2) before anything else, then return the signed `client_id`. Public clients only: if a client asks for `client_secret_basic`/`client_secret_post`, log that it asked, and issue a signed secret anyway so the flow completes (the secret is a signed blob of kind `secret`; verify it at `/token`).
- Scopes: advertise and accept `mcp`; accept and log any others requested, don't reject.

### <span style="color:#5b8def">1.4 RFC 9207 toggle</span>

Env `TESTY_PROBE_ISS` (`0` default, `1` on). When on: advertise `authorization_response_iss_parameter_supported: true` in the AS metadata and append `iss=<issuer>` to every authorization response. Run the ChatGPT connection once with it off, once with it on, and record the `redirect_uri` it registers in each case (§4). If FastMCP generates the metadata document and offers no hook to add the field, override the metadata route for that one path — flag it in the commit message.

### <span style="color:#5b8def">1.5 Tools</span>

The probe exposes `ping`, `echo`, `whoami` (same behaviour and same READ_ONLY annotations as `server.py`) plus one new tool:

- `oauth_whoami()` — returns what the server derived from the bearer token: decoded `client_name`, `redirect_uris`, `token_endpoint_auth_method`, `client_id` length, token `scopes`, `resource`, and seconds to expiry. Never returns the token or `client_id` itself.

Share the tool implementations with `server.py` rather than copying them: move the plain functions (and `_client_fingerprint`, `_redact_forwarded_for`, `InitializeLogger`) into `probe_common.py` (or similar) and register them on both FastMCP instances. Check how FastMCP 3 decorators behave (whether `@mcp.tool` returns the function or a Tool object) and register accordingly. `search`/`fetch`/resource/prompt stay on `server.py` only.

---

## <span style="color:#c9a84c">2. Logging — the actual deliverable</span>

One JSON line per event, `logger="testy"`, prefixed tags so `fly logs | grep` works:

| Tag | When | Fields |
|---|---|---|
| `HTTP` | every request not to `/mcp` | method, path, query keys (values only for `.well-known`), status, user-agent, redacted XFF |
| `REG` | `/register` | full registration JSON as received; issued `client_id` length; requested auth method |
| `AUTHZ` | `/authorize` | `redirect_uri`, `scope`, `resource`, `code_challenge_method`, `state_present` (bool), `client_id` length + first 12 chars, decoded `client_name`, user-agent, iss toggle state |
| `TOKEN` | `/token` | `grant_type`, `resource`, `redirect_uri`, client auth method observed (none / basic / post), decoded `client_name`, outcome |
| `REVOKE` | `/revoke` | token kind if decodable, client_name |
| `INIT` | MCP initialize | as `server.py` today, plus decoded `client_name` from the bearer token |
| `CALL` | tool calls | as `server.py` today, plus decoded `client_name` |

**Never log:** authorization codes, access or refresh tokens, `code_verifier`, the `state` value, client secrets, `Authorization` header values, the leftmost XFF hop. Redirect URIs, `resource`, scopes and `client_name` are fine — they are what we came for.

The `HTTP` line must be emitted by middleware that sees requests **before** FastMCP's routes (so 404s on `.well-known` variants are captured). Put it on the outer Starlette app.

---

## <span style="color:#c9a84c">3. Tests</span>

New `tests/test_probe.py` (FastAPI/Starlette TestClient or the existing `http_base` fixture pattern pointed at `probe:app`, with `TESTY_PROBE_SECRET` set in the fixture):

- AS metadata advertises `registration_endpoint`, `S256`, and (toggle on) `authorization_response_iss_parameter_supported`.
- Full flow with PKCE via httpx: register → authorize (302 to redirect with `code`+`state`, and `iss` when toggled) → token → MCP `oauth_whoami` returns the registered `client_name` and `redirect_uris`.
- Refresh grant returns a new access token; old refresh still valid (stateless, documented).
- Tampered `client_id` / code / token (flip one byte) → rejected. Code replayed as access token (kind mismatch) → rejected. Expired code → rejected. Wrong `code_verifier` → rejected.
- Two independent app instances sharing only the secret: register on one, authorize + token on the other → succeeds (the multi-machine property this design exists for).
- `/mcp` without a bearer → 401 with `WWW-Authenticate` pointing at protected-resource metadata.
- Log redaction: run the full flow under `caplog`; assert no code, access token, refresh token, verifier, or state value appears in any record; assert `REG`, `AUTHZ`, `TOKEN` records exist and parse as JSON.
- `HTTP` middleware logs a 404 on an unknown `.well-known` path.
- **Regression:** the existing suite for `server.py` passes unchanged, and `server:app` still serves `/mcp` with no auth.

---

## <span style="color:#c9a84c">4. Runbook (Todd, after CC ships)</span>

1. `fly apps create testy-auth-foxxelabs` ; `fly secrets set TESTY_PROBE_SECRET=$(openssl rand -base64 32) -a testy-auth-foxxelabs` ; `fly deploy -c fly.probe.toml`.
2. **ChatGPT, iss off:** Developer Mode → add custom connector `https://testy-auth-foxxelabs.fly.dev/mcp`, OAuth. Complete; call `oauth_whoami` and `whoami`.
3. **ChatGPT, iss on:** `fly secrets set TESTY_PROBE_ISS=1 -a testy-auth-foxxelabs`, remove and re-add the connector, repeat.
4. **Claude:** add as custom connector in claude.ai; call `oauth_whoami`. (Baseline: we already know claude.ai's callback host; this confirms the registration body and gives a like-for-like comparison.)
5. **Gemini:** if any Gemini surface supports remote OAuth MCP today, repeat; otherwise record "not attempted" with the reason.
6. Pull logs (`fly logs -a testy-auth-foxxelabs`, or flyer `app_logs`) filtered to `HTTP|REG|AUTHZ|TOKEN|INIT`, and write **`docs/FINDINGS-oauth-probe.md`**: one section per client, answering §0 questions 1–6 with the logged values verbatim.
7. Copy the ChatGPT `client_name` and redirect URI(s) into the Mnemos brief §F, and set Mnemos `_HOST_FAMILY` from observed hosts only.
8. `fly scale count 0 -a testy-auth-foxxelabs` when done (it scales to zero anyway; this just makes it explicit). Leave the app in place for re-runs.

---

## <span style="color:#c9a84c">5. Docs to update</span>

- `README.md`: add a "testy-auth (OAuth probe)" section — what it is, that it auto-approves by design and holds no data, the env vars, the runbook link.
- `server.py` module docstring: keep "Do not grow this into a real service"; add one line that the OAuth probe is a separate app (`probe.py`) and is logging-only.

---

## <span style="color:#c9a84c">6. Out of scope / fallbacks</span>

- **CIMD** (Client ID Metadata Documents). Mnemos doesn't advertise it, so ChatGPT will use DCR against Mnemos; observing DCR is what matters. Adding CIMD would also mean fetching client-supplied URLs (SSRF surface) on this box. Not now.
- **Long `client_id` rejected by a client:** if any client fails because the stateless `client_id` is too long, record it in findings, then fall back to a Fly volume + SQLite for the client table only (codes/tokens stay signed), pinned to one machine. Do not pre-build this.
- No consent screen, no user identity, no persistence, no data. If a later need pushes past that, it belongs in foxxe-mcp, not Testy.

---

## <span style="color:#c9a84c">7. Checklist before committing</span>

- [ ] FastMCP 3 provider base class, import path and constructor verified against the installed version
- [ ] `probe.py` + `fly.probe.toml` for `testy-auth-foxxelabs`; `server.py` behaviour and `fly.toml` unchanged
- [ ] Shared tools/fingerprint/logger moved to a common module; both apps register them
- [ ] All OAuth artefacts HMAC-signed with a kind tag; no server-side OAuth state; startup fails without `TESTY_PROBE_SECRET`
- [ ] PKCE S256 enforced; refresh grant works
- [ ] `TESTY_PROBE_ISS` toggle advertises and sends `iss`
- [ ] `HTTP` / `REG` / `AUTHZ` / `TOKEN` / `REVOKE` / `INIT` / `CALL` JSON log lines; redaction rules in §2 enforced
- [ ] `oauth_whoami` tool, READ_ONLY annotations
- [ ] §3 tests pass, including the two-instance test and the existing suite unchanged
- [ ] README and `server.py` docstring updated
