"""Token verification — Entra ID in production, locally-signed tokens in dev.

Two verifiers behind one protocol. The dev verifier exists so the whole platform
is developable and CI-runnable without an Entra tenant; it is refused outright in
production by ``Settings`` validation, so it cannot become a live bypass.

The Entra verifier fails **closed**: if JWKS cannot be fetched, tokens are
rejected rather than accepted on the previous key set indefinitely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OctKey

from askau.core.errors import UnauthenticatedError
from askau.settings import Settings


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """What a valid token asserts. Not yet an authorization context — group
    claims still have to be resolved to principal IDs against the database."""

    subject: str
    email: str
    display_name: str
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    department: str | None = None


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedIdentity: ...


class DevTokenVerifier:
    """HS256 tokens signed with a local secret, for development and tests."""

    def __init__(self, settings: Settings) -> None:
        self._key = OctKey.import_key(settings.dev_token_secret)

    async def verify(self, token: str) -> VerifiedIdentity:
        try:
            decoded = jwt.decode(token, self._key, algorithms=["HS256"])
        except JoseError as exc:
            raise UnauthenticatedError("Token could not be verified") from exc

        claims = decoded.claims
        if claims.get("exp", 0) < time.time():
            raise UnauthenticatedError("Token has expired")
        if not claims.get("sub"):
            raise UnauthenticatedError("Token is missing a subject")

        return VerifiedIdentity(
            subject=str(claims["sub"]),
            email=str(claims.get("email", "")),
            display_name=str(claims.get("name", claims["sub"])),
            groups=tuple(claims.get("groups", ())),
            roles=tuple(claims.get("roles", ())),
            department=claims.get("department"),
        )

    def issue(
        self,
        subject: str,
        *,
        email: str,
        name: str,
        groups: tuple[str, ...] = (),
        roles: tuple[str, ...] = (),
        department: str | None = None,
        ttl_seconds: int = 3600,
    ) -> str:
        """Mint a token. Dev only — there is no equivalent on the Entra path,
        because AskAU must never be able to issue an organizational identity."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "email": email,
            "name": name,
            "groups": list(groups),
            "roles": list(roles),
            "iat": now,
            "exp": now + ttl_seconds,
            "iss": "askau-dev",
        }
        if department:
            claims["department"] = department
        return jwt.encode({"alg": "HS256"}, claims, self._key)


class EntraTokenVerifier:
    """Microsoft Entra ID via OIDC discovery and JWKS.

    Validates signature, issuer, audience and expiry. Every one of those matters:
    a token valid for a *different* application is still a genuine Entra token,
    so skipping the audience check would accept it.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._authority = f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0"
        self._keys: KeySet | None = None
        self._keys_fetched_at = 0.0
        self._jwks_uri: str | None = None
        self._ttl = 3600.0

    async def _discover(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        resp = await self._client.get(f"{self._authority}/.well-known/openid-configuration")
        resp.raise_for_status()
        self._jwks_uri = str(resp.json()["jwks_uri"])
        return self._jwks_uri

    async def _key_set(self, *, force: bool = False) -> KeySet:
        fresh = self._keys is not None and time.time() - self._keys_fetched_at < self._ttl
        if fresh and not force:
            assert self._keys is not None
            return self._keys
        try:
            resp = await self._client.get(await self._discover())
            resp.raise_for_status()
            self._keys = KeySet.import_key_set(resp.json())
            self._keys_fetched_at = time.time()
        except Exception as exc:
            # Fail closed. Serving on an unrefreshable key set would keep
            # accepting tokens signed by a key the tenant has since rotated out.
            raise UnauthenticatedError("Identity provider keys are unavailable") from exc
        return self._keys

    async def verify(self, token: str) -> VerifiedIdentity:
        keys = await self._key_set()
        try:
            decoded = jwt.decode(token, keys)
        except JoseError:
            # A rotated key is the common cause; retry once against fresh JWKS
            # before rejecting.
            keys = await self._key_set(force=True)
            try:
                decoded = jwt.decode(token, keys)
            except JoseError as exc:
                raise UnauthenticatedError("Token could not be verified") from exc

        claims = decoded.claims
        now = time.time()
        if claims.get("exp", 0) < now:
            raise UnauthenticatedError("Token has expired")
        if claims.get("nbf", 0) > now + 60:
            raise UnauthenticatedError("Token is not yet valid")
        if claims.get("aud") != self._settings.entra_audience:
            raise UnauthenticatedError("Token audience does not match this application")
        issuer = str(claims.get("iss", ""))
        if self._settings.entra_tenant_id not in issuer:
            raise UnauthenticatedError("Token issuer is not the configured tenant")

        return VerifiedIdentity(
            subject=str(claims.get("oid") or claims.get("sub", "")),
            email=str(claims.get("preferred_username") or claims.get("email", "")),
            display_name=str(claims.get("name", "")),
            groups=tuple(claims.get("groups", ())),
            roles=tuple(claims.get("roles", ())),
            department=claims.get("department"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def build_verifier(settings: Settings) -> TokenVerifier:
    return DevTokenVerifier(settings) if settings.is_dev_auth else EntraTokenVerifier(settings)
