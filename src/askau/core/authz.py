"""Principal resolution — turning a verified identity into an authorization context.

This is the last step before retrieval, and the only place principals are
computed. Everything downstream consumes the resulting frozen context; nothing
downstream can widen it.

Resolution is cached in Redis under the user's ``acl_version``. Bumping that
column invalidates the entry immediately, which is the cheap half of FR-025 —
the other half is the reconciler that rewrites ``chunks.acl_principals``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.core.errors import UnauthenticatedError
from askau.core.identity import VerifiedIdentity
from askau.domain.authz import AuthorizationContext, PrincipalId, UserId
from askau.domain.enums import AppRole
from askau.settings import Settings

_VALID_ROLES = frozenset(r.value for r in AppRole)

_RESOLVE_SQL = text("""
    SELECT u.id::text        AS user_id,
           u.email,
           u.department,
           u.acl_version,
           u.is_active,
           ARRAY(
               SELECT up.principal_id
               FROM user_principals up
               JOIN principals p ON p.id = up.principal_id
               WHERE up.user_id = u.id AND p.is_active
           )                 AS principals,
           ARRAY(
               SELECT ara.role FROM app_role_assignments ara WHERE ara.user_id = u.id
           )                 AS roles
    FROM users u
    WHERE u.entra_oid = :oid
""")


@dataclass(frozen=True, slots=True)
class _UserRow:
    """Typed projection of the resolution query."""

    user_id: str
    email: str
    department: str | None
    acl_version: int
    is_active: bool
    principals: tuple[int, ...]
    roles: tuple[str, ...]


class AuthorizationResolver:
    def __init__(self, engine: AsyncEngine, redis: Redis, settings: Settings) -> None:
        self._engine = engine
        self._redis = redis
        self._ttl = settings.authz_cache_ttl

    async def resolve(self, identity: VerifiedIdentity) -> AuthorizationContext:
        row = await self._load(identity.subject)
        if row is None:
            raise UnauthenticatedError("No AskAU account exists for this identity")
        if not row.is_active:
            raise UnauthenticatedError("This account is disabled")

        cached = await self._from_cache(row.user_id, row.acl_version)
        principals = cached if cached is not None else list(row.principals)
        if cached is None:
            await self._to_cache(row.user_id, row.acl_version, principals)

        if not principals:
            # Every user has at least their own user principal. An empty set means
            # the identity sync did not complete, and treating that as "authorized
            # for nothing" would look like a working system returning no answers.
            raise UnauthenticatedError(
                "Account has no resolved principals; identity synchronization "
                "may not have completed"
            )

        return AuthorizationContext(
            user_id=UserId(row.user_id),
            principals=frozenset(PrincipalId(p) for p in principals),
            acl_version=row.acl_version,
            roles=frozenset(AppRole(r) for r in row.roles if r in _VALID_ROLES),
            department=row.department,
            email=row.email,
        )

    async def invalidate(self, user_id: str) -> None:
        """Drop every cached context for a user, across all acl_versions."""
        async for key in self._redis.scan_iter(match=f"authz:{user_id}:*"):
            await self._redis.delete(key)

    # ── internals ───────────────────────────────────────────────────────────

    async def _load(self, oid: str) -> _UserRow | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(_RESOLVE_SQL, {"oid": oid})
            row = result.mappings().first()
        if row is None:
            return None
        return _UserRow(
            user_id=str(row["user_id"]),
            email=str(row["email"]),
            department=row["department"],
            acl_version=int(row["acl_version"]),
            is_active=bool(row["is_active"]),
            principals=tuple(int(p) for p in row["principals"]),
            roles=tuple(str(r) for r in row["roles"]),
        )

    def _key(self, user_id: str, acl_version: int) -> str:
        return f"authz:{user_id}:{acl_version}"

    async def _from_cache(self, user_id: str, acl_version: int) -> list[int] | None:
        try:
            raw = await self._redis.get(self._key(user_id, acl_version))
        except Exception:
            # A cache outage must degrade to a database read, never to a denial
            # and never to a stale permit.
            return None
        if raw is None:
            return None
        try:
            return [int(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return None

    async def _to_cache(self, user_id: str, acl_version: int, principals: list[int]) -> None:
        try:
            await self._redis.setex(
                self._key(user_id, acl_version), self._ttl, json.dumps(principals)
            )
        except Exception:
            # Caching is an optimization; failing to cache must not fail a request.
            return
