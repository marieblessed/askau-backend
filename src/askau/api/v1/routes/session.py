"""Session lifecycle (FR-001, FR-001a).

The OIDC handshake itself happens in the browser against Entra; this is what
AskAU does with the resulting token: verify it, resolve the organisational
identity, create a server session, and support secure logout.

In `dev` auth mode the same endpoints work against locally-signed tokens, so the
flow is exercisable without an Entra tenant — and `Settings` refuses dev mode in
production, so this cannot become a live bypass.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.errors import UnauthenticatedError
from askau.core.redaction import hash_ip
from askau.domain.enums import AuditOutcome

router = APIRouter(prefix="/auth", tags=["auth"])


class SessionIn(BaseModel):
    """The verified bearer token. AskAU never handles the authorization code or
    a client secret — that exchange belongs to the browser and Entra."""

    access_token: str


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    """Public discovery metadata for the sign-in page.

    Deliberately contains no secret: the client id and authority are public by
    design in an authorization-code flow, and the client secret never leaves
    the server.
    """
    settings = request.app.state.settings
    if settings.is_dev_auth:
        return {
            "mode": "dev",
            "detail": (
                "Development mode. Tokens are minted locally with "
                "`make dev-token USER=<username>`; there is no Entra handshake."
            ),
        }
    return {
        "mode": "entra",
        "authority": f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0",
        "client_id": settings.entra_client_id,
        "scopes": [f"api://{settings.entra_audience}/access_as_user"],
        "response_type": "code",
        "pkce": True,
    }


@router.post("/session", status_code=201)
async def create_session(body: SessionIn, request: Request) -> dict[str, Any]:
    """Verify the token, upsert the user, and open a session (FR-001, FR-001a)."""
    verifier = request.app.state.verifier
    identity = await verifier.verify(body.access_token)

    engine = request.app.state.engine
    async with engine.begin() as conn:
        user = (
            (
                await conn.execute(
                    text("SELECT id::text, is_active FROM users WHERE entra_oid = :oid"),
                    {"oid": identity.subject},
                )
            )
            .mappings()
            .first()
        )
        if user is None:
            # Not auto-provisioned. A valid organisational token is not the same
            # as an AskAU account: onboarding assigns a department and group
            # membership, and inventing those from a token would create a user
            # whose authorization nobody decided.
            request.app.state.audit.record(
                AuditEvent(
                    event_type=EventType.LOGIN_FAILED,
                    outcome=AuditOutcome.DENIED,
                    actor_email=identity.email,
                    detail={"reason": "no_askau_account"},
                )
            )
            raise UnauthenticatedError("No AskAU account exists for this identity")
        if not user["is_active"]:
            raise UnauthenticatedError("This account is disabled")

        session_id = str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO sessions (user_id, expires_at, ip_hash, user_agent)
                    VALUES (CAST(:uid AS uuid), now() + interval '8 hours', :ip, :ua)
                    RETURNING id
                    """),
                    {
                        "uid": user["id"],
                        "ip": hash_ip(request.client.host if request.client else None),
                        "ua": request.headers.get("user-agent", "")[:400],
                    },
                )
            ).scalar_one()
        )
        await conn.execute(
            text("UPDATE users SET last_login_at = now() WHERE id = CAST(:uid AS uuid)"),
            {"uid": user["id"]},
        )

    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.LOGIN,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user["id"],
            actor_email=identity.email,
            resource_type="session",
            resource_id=session_id,
        )
    )
    return {"session_id": session_id, "expires_in": 28_800}


@router.post("/session/refresh")
async def refresh_session(authz: AuthzDep, request: Request) -> dict[str, Any]:
    async with request.app.state.engine.begin() as conn:
        session_id = str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO sessions (user_id, expires_at)
                    VALUES (CAST(:uid AS uuid), now() + interval '8 hours')
                    RETURNING id
                    """),
                    {"uid": str(authz.user_id)},
                )
            ).scalar_one()
        )
    return {"session_id": session_id, "expires_in": 28_800}


@router.delete("/session", status_code=204)
async def logout(authz: AuthzDep, request: Request) -> None:
    """Revoke every session and evict the cached authorization context.

    Both halves matter: a revoked session with a warm authz cache would keep
    answering until the cache expired.
    """
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text("""
            UPDATE sessions SET revoked_at = now()
            WHERE user_id = CAST(:uid AS uuid) AND revoked_at IS NULL
            """),
            {"uid": str(authz.user_id)},
        )
    await request.app.state.authz_resolver.invalidate(str(authz.user_id))
    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.LOGOUT,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
        )
    )
