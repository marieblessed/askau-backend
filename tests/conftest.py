"""Shared fixtures.

Integration and security tests run against a real PostgreSQL with pgvector.
Mocking the database here would mock the thing under test: the ACL predicate,
partition pruning and HNSW behaviour only exist in a real engine.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from askau.core.identity import DevTokenVerifier
from askau.domain.authz import AuthorizationContext, PrincipalId, UserId
from askau.domain.enums import AppRole
from askau.llm.ports import Embedder
from askau.llm.registry import build_embedder
from askau.main import create_app
from askau.retrieval.adapters.pgvector_hybrid import PgVectorHybridRetriever
from askau.scripts.corpus import USERS
from askau.settings import Settings, get_settings


def _db_available() -> bool:
    return bool(os.environ.get("ASKAU_DATABASE_URL"))


requires_db = pytest.mark.skipif(
    not _db_available(), reason="ASKAU_DATABASE_URL not set; integration tests skipped"
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


def _fresh_engine(url: str) -> AsyncEngine:
    """A per-test engine with no pooling.

    Engines cache connections against the event loop that created them, and each
    test gets its own loop — so a shared engine yields "attached to a different
    loop" failures that look like database problems but are not.
    """
    return create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    )


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """The *application* engine — least privilege, RLS applies."""
    eng = _fresh_engine(settings.database_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def admin_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = _fresh_engine(settings.migration_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture(scope="session")
def embedder(settings: Settings) -> Embedder:
    return build_embedder(settings)


@pytest.fixture
def retriever(engine: AsyncEngine, settings: Settings) -> PgVectorHybridRetriever:
    return PgVectorHybridRetriever(engine, settings)


@pytest_asyncio.fixture
async def authz_for(engine: AsyncEngine) -> AsyncIterator[object]:
    """Build a real AuthorizationContext for a seeded username.

    Reads principals from the database rather than constructing them, so the
    tests exercise the same resolution path production uses.
    """

    async def _build(username: str) -> AuthorizationContext:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("""
                    SELECT u.id::text AS uid, u.acl_version, u.department, u.email,
                           ARRAY(SELECT up.principal_id FROM user_principals up
                                 WHERE up.user_id = u.id) AS principals,
                           ARRAY(SELECT ara.role FROM app_role_assignments ara
                                 WHERE ara.user_id = u.id) AS roles
                    FROM users u WHERE u.entra_oid = :oid
                    """),
                        {"oid": f"oid-{username}"},
                    )
                )
                .mappings()
                .one()
            )
        valid = {r.value for r in AppRole}
        return AuthorizationContext(
            user_id=UserId(row["uid"]),
            principals=frozenset(PrincipalId(p) for p in row["principals"]),
            acl_version=row["acl_version"],
            roles=frozenset(AppRole(r) for r in row["roles"] if r in valid),
            department=row["department"],
            email=row["email"],
        )

    yield _build


@pytest_asyncio.fixture
async def doc_id_of(admin_engine: AsyncEngine) -> AsyncIterator[object]:
    """Resolve a corpus key to its document id."""

    async def _resolve(key: str) -> str:
        async with admin_engine.connect() as conn:
            return str(
                (
                    await conn.execute(
                        text("SELECT id FROM documents WHERE source_uri LIKE :p"),
                        {"p": f"%/{key}"},
                    )
                ).scalar_one()
            )

    yield _resolve


# ── HTTP fixtures ───────────────────────────────────────────────────────────
# Shared by the integration and security suites: the RBAC matrix is exercised
# over real HTTP, because a role guard that is only unit-tested proves the
# function works, not that the route calls it.


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


@pytest.fixture
def token(settings: Settings):  # type: ignore[no-untyped-def]
    """Mint a dev bearer token for a seeded identity."""
    verifier = DevTokenVerifier(settings)

    def _issue(username: str) -> str:
        user = next(u for u in USERS if u.username == username)
        return verifier.issue(
            f"oid-{user.username}",
            email=user.email,
            name=user.name,
            groups=user.groups,
            roles=user.roles,
            department=user.department,
        )

    return _issue
