"""Role guards for administrative surfaces.

Distinct from document authorization. Document access is decided in the
retrieval predicate; these guards decide who may reach an *administrative*
endpoint at all. They return 403 rather than 404 because role membership is not
sensitive in the way document existence is (ADR-0009).
"""

from __future__ import annotations

from askau.core.errors import InsufficientRoleError
from askau.domain.authz import AuthorizationContext
from askau.domain.enums import AppRole


def require_roles(authz: AuthorizationContext, *roles: AppRole) -> None:
    if not authz.has_any_role(*roles):
        wanted = ", ".join(r.value for r in roles)
        raise InsufficientRoleError(f"This action requires one of: {wanted}")


def require_knowledge_admin(authz: AuthorizationContext) -> None:
    require_roles(authz, AppRole.KNOWLEDGE_ADMIN, AppRole.SYSTEM_ADMIN)


def require_system_admin(authz: AuthorizationContext) -> None:
    require_roles(authz, AppRole.SYSTEM_ADMIN)


def require_security_admin(authz: AuthorizationContext) -> None:
    require_roles(authz, AppRole.SECURITY_ADMIN)
