"""
ProbeOAuthProvider — the OAuth 2.1 authorization server behind testy-auth.

Same methods and semantics as todd427/mnemos:server/oauth_provider.py, with
four deliberate differences, all from docs/BRIEF-oauth-probe.md §1.2:

  1. State is four plain dicts and is NEVER persisted. Where the provider
     keeps state is invisible to a client — the handlers, metadata, error
     bodies and redirects are the SDK's either way — so a volume would buy
     nothing the probe needs. Auto-stop wipes it, and what a client does
     next IS question 7.
  2. authorize() auto-approves. Harmless here and nowhere else: the token
     unlocks three read-only probe tools and no data.
  3. Any https redirect host is allowed, plus localhost/127.0.0.1. The probe
     must not reject a client we are trying to observe. Every redirect host
     is logged.
  4. Every method logs. That is the deliverable.

PKCE verification and redirect-URI matching on /token are the SDK token
handler's job — it recomputes sha256(code_verifier) and compares redirect
URIs itself. Neither is reimplemented here; load_authorization_code just has
to return a faithful AuthorizationCode.
"""

import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

log = logging.getLogger("testy")

SCOPE = "mcp"
TOKEN_LIFETIME = 3600            # 1 hour
REFRESH_LIFETIME = 7 * 24 * 3600  # 7 days
CODE_LIFETIME = 300              # 5 minutes

# 8 characters is enough to correlate two log lines and not enough to use.
ID_PREFIX = 8


def _emit(tag: str, **fields: Any) -> None:
    log.info("%s %s", tag, json.dumps(fields, default=str))


def _prefix(client_id: str | None) -> str:
    return (client_id or "")[:ID_PREFIX]


def redirect_allowed(uri: str) -> tuple[bool, str]:
    """(allowed, host). Any https host, plus localhost/127.0.0.1 on http.

    Deliberately wide — see the module docstring. Mnemos's equivalent is an
    exact allow-list and must stay that way; this is the opposite choice for
    the opposite reason.
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False, ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return True, host
    if parsed.scheme == "http" and host in ("localhost", "127.0.0.1"):
        return True, host
    return False, host


class ProbeOAuthProvider(OAuthAuthorizationServerProvider):
    """In-memory OAuth provider that logs everything a client does."""

    def __init__(self, issuer: str, send_iss: bool = False):
        self.issuer = issuer.rstrip("/")
        self.send_iss = send_iss
        self._clients: dict[str, Any] = {}
        self._codes: dict[str, Any] = {}
        self._tokens: dict[str, Any] = {}
        self._refresh: dict[str, Any] = {}

    # ── client registration ────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._clients.get(client_id)
        if not data:
            # The state-loss signal: a client presenting an id this process
            # has never issued. Question 7 turns on these lines.
            _emit("UNKNOWN", lookup="get_client", client_id_prefix=_prefix(client_id))
            return None
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            _emit("UNKNOWN", lookup="get_client", client_id_prefix=_prefix(client_id),
                  error=str(exc))
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info.model_dump(mode="json")
        _emit("REG_OK",
              client_id_prefix=_prefix(client_info.client_id),
              client_name=getattr(client_info, "client_name", None),
              redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
              redirect_hosts=[redirect_allowed(str(u))[1] for u in (client_info.redirect_uris or [])],
              token_endpoint_auth_method=getattr(client_info, "token_endpoint_auth_method", None),
              grant_types=getattr(client_info, "grant_types", None),
              response_types=getattr(client_info, "response_types", None),
              scope=getattr(client_info, "scope", None),
              secret_issued=bool(getattr(client_info, "client_secret", None)),
              client_id_issued_at=getattr(client_info, "client_id_issued_at", None))

    # ── authorization ──────────────────────────────────────────────────────

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        redirect_uri = str(getattr(params, "redirect_uri", "") or "")
        allowed, host = redirect_allowed(redirect_uri)

        scopes = getattr(params, "scopes", None) or [SCOPE]
        challenge = getattr(params, "code_challenge", None)
        state = getattr(params, "state", None)
        resource = getattr(params, "resource", None)

        _emit("AUTHZ",
              client_id_prefix=_prefix(client.client_id),
              client_name=getattr(client, "client_name", None),
              redirect_uri=redirect_uri,
              redirect_host=host,
              redirect_allowed=allowed,
              scopes=scopes,
              resource=str(resource) if resource else None,
              state_present=state is not None,
              code_challenge_present=challenge is not None,
              code_challenge_method=getattr(params, "code_challenge_method", None),
              iss_toggle=self.send_iss)

        if not redirect_uri:
            raise ValueError("redirect_uri is required")
        if not allowed:
            raise ValueError(f"redirect_uri not allowed: {redirect_uri}")

        code = secrets.token_urlsafe(32)
        self._codes[code] = {
            "code": code,
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "scopes": scopes,
            "code_challenge": str(challenge) if challenge is not None else None,
            "code_challenge_method": str(getattr(params, "code_challenge_method", "S256") or "S256"),
            "expires_at": time.time() + CODE_LIFETIME,
        }

        extra = {"iss": self.issuer} if self.send_iss else {}
        return construct_redirect_uri(redirect_uri, code=code, state=state, **extra)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        data = self._codes.get(authorization_code)
        if not data:
            _emit("UNKNOWN", lookup="load_authorization_code",
                  client_id_prefix=_prefix(client.client_id))
            return None
        if time.time() > data["expires_at"]:
            self._codes.pop(authorization_code, None)
            _emit("UNKNOWN", lookup="load_authorization_code", reason="expired",
                  client_id_prefix=_prefix(client.client_id))
            return None
        if data["client_id"] != client.client_id:
            _emit("UNKNOWN", lookup="load_authorization_code", reason="client_mismatch",
                  client_id_prefix=_prefix(client.client_id))
            return None
        return AuthorizationCode(
            code=data["code"],
            client_id=data["client_id"],
            redirect_uri=AnyHttpUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
            expires_at=data["expires_at"],
            scopes=data["scopes"],
            code_challenge=data.get("code_challenge"),
            code_challenge_method=data.get("code_challenge_method", "S256"),
        )

    # ── token issue and refresh ────────────────────────────────────────────

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        access = secrets.token_urlsafe(48)
        refresh = secrets.token_urlsafe(48)
        scopes = authorization_code.scopes or [SCOPE]
        now = time.time()

        self._tokens[access] = {"token": access, "client_id": client.client_id,
                                "scopes": scopes, "expires_at": now + TOKEN_LIFETIME}
        self._refresh[refresh] = {"token": refresh, "client_id": client.client_id,
                                  "scopes": scopes, "access_token": access,
                                  "expires_at": now + REFRESH_LIFETIME}

        _emit("TOKEN", stage="exchange_code",
              client_id_prefix=_prefix(client.client_id),
              client_name=getattr(client, "client_name", None),
              scopes=scopes, expires_in=TOKEN_LIFETIME)

        return OAuthToken(access_token=access, token_type="bearer",
                          expires_in=TOKEN_LIFETIME, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._tokens.get(token)
        if not data:
            _emit("UNKNOWN", lookup="load_access_token")
            return None
        if time.time() > data["expires_at"]:
            _emit("UNKNOWN", lookup="load_access_token", reason="expired",
                  client_id_prefix=_prefix(data["client_id"]))
            return None
        return AccessToken(token=data["token"], client_id=data["client_id"],
                           scopes=data.get("scopes", [SCOPE]),
                           expires_at=int(data["expires_at"]))

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        data = self._refresh.get(refresh_token)
        if not data:
            _emit("UNKNOWN", lookup="load_refresh_token",
                  client_id_prefix=_prefix(client.client_id))
            return None
        if data["client_id"] != client.client_id:
            _emit("UNKNOWN", lookup="load_refresh_token", reason="client_mismatch",
                  client_id_prefix=_prefix(client.client_id))
            return None
        return RefreshToken(token=data["token"], client_id=data["client_id"],
                            scopes=data.get("scopes", [SCOPE]))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        new_access = secrets.token_urlsafe(48)
        use_scopes = scopes or refresh_token.scopes or [SCOPE]
        self._tokens[new_access] = {"token": new_access, "client_id": client.client_id,
                                    "scopes": use_scopes,
                                    "expires_at": time.time() + TOKEN_LIFETIME}
        if refresh_token.token in self._refresh:
            self._refresh[refresh_token.token]["access_token"] = new_access

        _emit("TOKEN", stage="exchange_refresh",
              client_id_prefix=_prefix(client.client_id),
              client_name=getattr(client, "client_name", None),
              scopes=use_scopes, expires_in=TOKEN_LIFETIME)

        return OAuthToken(access_token=new_access, token_type="bearer",
                          expires_in=TOKEN_LIFETIME, scope=" ".join(use_scopes),
                          refresh_token=refresh_token.token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        if isinstance(token, AccessToken):
            self._tokens.pop(token.token, None)
        else:
            data = self._refresh.pop(token.token, None)
            if data:
                self._tokens.pop(data.get("access_token", ""), None)
        client = self._clients.get(token.client_id) or {}
        _emit("REVOKE", kind=kind, client_id_prefix=_prefix(token.client_id),
              client_name=client.get("client_name"))
