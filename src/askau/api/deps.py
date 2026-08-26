"""Dependency wiring.

Components are built once at startup and held on ``app.state``. The
``AuthorizationContext`` is resolved per request and is the only object the
retrieval layer receives — there is no dependency that hands a route the raw
token or a way to widen access.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from askau.core.errors import UnauthenticatedError
from askau.core.identity import TokenVerifier
from askau.domain.authz import AuthorizationContext
from askau.rag.orchestrator import RagOrchestrator
from askau.settings import Settings


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_orchestrator(request: Request) -> RagOrchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def get_verifier(request: Request) -> TokenVerifier:
    return request.app.state.verifier  # type: ignore[no-any-return]


async def current_authz(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthorizationContext:
    """Verify the bearer token and resolve the caller's principals.

    Everything downstream depends on this, so a route cannot accidentally run
    unauthenticated: omitting the dependency means no authorization context to
    pass to retrieval, and retrieval will not accept a query without one.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthenticatedError("A bearer token is required")

    identity = await request.app.state.verifier.verify(authorization[7:].strip())
    return await request.app.state.authz_resolver.resolve(identity)  # type: ignore[no-any-return]


AuthzDep = Annotated[AuthorizationContext, Depends(current_authz)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
OrchestratorDep = Annotated[RagOrchestrator, Depends(get_orchestrator)]
