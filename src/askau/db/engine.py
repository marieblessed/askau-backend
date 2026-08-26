"""Database engines and session management.

Two engines: a read-write engine for mutations and migrations-adjacent work, and
a read-only engine for retrieval. Retrieval is the highest-volume path and is
purely read, so routing it separately is what lets a replica absorb it later
without touching call sites.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from askau.core.errors import ConfigurationError
from askau.settings import Settings

_engines: dict[str, AsyncEngine] = {}


def _create(url: str, settings: Settings, *, readonly: bool) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        # PgBouncer in transaction mode cannot support server-side prepared
        # statements; disabling the cache keeps the app deployable behind it.
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        execution_options={"postgresql_readonly": True} if readonly else {},
        echo=False,
    )


def get_engine(settings: Settings) -> AsyncEngine:
    if "rw" not in _engines:
        _engines["rw"] = _create(settings.database_url, settings, readonly=False)
    return _engines["rw"]


def get_read_engine(settings: Settings) -> AsyncEngine:
    if "ro" not in _engines:
        _engines["ro"] = _create(settings.read_url, settings, readonly=False)
    return _engines["ro"]


def get_admin_engine(settings: Settings) -> AsyncEngine:
    """Engine for administrative tooling: seeding, reindexing, partition upkeep.

    Uses the migration role because the application role deliberately lacks
    TRUNCATE and DDL — the running service must not be able to wipe the
    knowledge base, and separating the engines is what makes that true rather
    than merely intended.
    """
    if "admin" not in _engines:
        _engines["admin"] = _create(settings.migration_url, settings, readonly=False)
    return _engines["admin"]


async def dispose_engines() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()


@asynccontextmanager
async def read_connection(settings: Settings) -> AsyncIterator[AsyncConnection]:
    """A connection configured for retrieval.

    ``hnsw.iterative_scan`` is set per session and is not optional: without it a
    selective ACL filter causes HNSW to return fewer than k rows. The failure
    mode is silent recall loss, not an error — which is why the pgvector floor is
    0.8 and why this is set here rather than left to a DBA's postgresql.conf.
    """
    engine = get_read_engine(settings)
    async with engine.connect() as conn:
        await conn.execute(
            text(f"SET LOCAL hnsw.iterative_scan = '{settings.hnsw_iterative_scan}'")
        )
        await conn.execute(
            text(f"SET LOCAL hnsw.max_scan_tuples = {settings.hnsw_max_scan_tuples}")
        )
        yield conn


async def assert_database_ready(settings: Settings) -> None:
    """Verify the properties retrieval depends on, at startup.

    Infrastructure is provisioned by another team (ADR-0014), so the application
    verifies rather than assumes. A pgvector below 0.8 does not error at query
    time — it quietly under-returns — so this check is the only thing standing
    between a misprovisioned database and months of degraded answers nobody
    attributes to the right cause.
    """
    engine = get_engine(settings)
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        ).scalar_one_or_none()

    if row is None:
        raise ConfigurationError(
            "The 'vector' extension is not installed in this database. AskAU cannot "
            "run without pgvector — provision the database from an image that "
            "includes it (e.g. pgvector/pgvector:pg17)."
        )

    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split(".")[:3])

    if parts(row) < parts(settings.min_pgvector_version):
        raise ConfigurationError(
            f"pgvector {row} is below the required minimum "
            f"{settings.min_pgvector_version}. Below 0.8 the iterative index scan "
            "is unavailable, so an ACL-filtered vector search silently returns "
            "fewer results than requested — degraded retrieval with no error."
        )
