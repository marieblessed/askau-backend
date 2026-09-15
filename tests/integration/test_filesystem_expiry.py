"""A file removed from a local export is a document withdrawn.

Expiry was added for the connector path first, and only the connector path —
an oversight, and a live one rather than theoretical: the filesystem export is
what actually runs today against the synthetic corpus. A rescinded policy left
in the corpus keeps being retrieved and cited in a form indistinguishable from
a live one.

The safety conditions are the same as the remote path's and are worth asserting
separately, because a filesystem walk fails differently — an unmounted share or
a permissions change on the root looks exactly like an empty directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.ingestion.pipeline import IngestionPipeline
from askau.settings import Settings
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

_BODY = (
    "Staff accrue thirty days of annual leave in each calendar year under this policy. "
    "Leave accrues monthly from the date of appointment and may be carried over to the "
    "following year only with the written approval of the responsible director. Unused "
    "leave above the carry-over limit lapses at the end of the leave year, and staff on "
    "probation accrue at the same rate but may not take leave until they are confirmed."
)

_PREFIX = "askau-fs-test:"


@pytest.fixture
def pipeline(admin_engine: AsyncEngine, settings: Settings, embedder):  # type: ignore[no-untyped-def]
    return IngestionPipeline(admin_engine, embedder, settings)


@pytest.fixture
async def source(admin_engine: AsyncEngine):  # type: ignore[no-untyped-def]
    async with admin_engine.begin() as conn:
        owner = (await conn.execute(text("SELECT id::text FROM users LIMIT 1"))).scalar_one()
        sid = str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO knowledge_sources
                      (name, source_type, business_owner_id, department,
                       default_classification, location, access_rules,
                       status, approved_by, approved_at)
                    VALUES (:n, 'filesystem', CAST(:o AS uuid), 'MISD', 'internal',
                            '{}'::jsonb, '{"principals": ["grp-all-staff"]}'::jsonb,
                            'active', CAST(:o AS uuid), now())
                    RETURNING id
                    """),
                    {"n": f"{_PREFIX}export", "o": owner},
                )
            ).scalar_one()
        )
    yield sid
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM knowledge_sources WHERE name LIKE :m"), {"m": f"{_PREFIX}%"}
        )


async def _run(engine: AsyncEngine, source_id: str) -> str:
    async with engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO ingestion_runs (source_id, trigger, status)
                    VALUES (CAST(:s AS uuid), 'manual', 'running') RETURNING id
                    """),
                    {"s": source_id},
                )
            ).scalar_one()
        )


async def _lifecycle(engine: AsyncEngine, source_id: str, key: str) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("""
                    SELECT d.lifecycle::text FROM documents d
                    JOIN document_families f ON f.id = d.family_id
                    WHERE d.source_id = CAST(:s AS uuid) AND f.external_key = :k
                    ORDER BY d.version_seq DESC LIMIT 1
                    """),
                    {"s": source_id, "k": key},
                )
            ).scalar_one()
        )


class TestFilesystemExpiry:
    async def test_a_file_deleted_from_the_export_is_expired(
        self, tmp_path: Path, admin_engine: AsyncEngine, pipeline: IngestionPipeline, source: str
    ) -> None:
        (tmp_path / "kept.txt").write_text(_BODY, encoding="utf-8")
        (tmp_path / "removed.txt").write_text(_BODY + " Second document.", encoding="utf-8")
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))
        assert await _lifecycle(admin_engine, source, "removed.txt") == "active"

        (tmp_path / "removed.txt").unlink()
        outcome = await pipeline.ingest_directory(
            tmp_path, source, await _run(admin_engine, source)
        )

        assert outcome.expired == 1
        assert await _lifecycle(admin_engine, source, "removed.txt") == "expired"
        assert await _lifecycle(admin_engine, source, "kept.txt") == "active"

    async def test_an_emptied_directory_expires_nothing(
        self, tmp_path: Path, admin_engine: AsyncEngine, pipeline: IngestionPipeline, source: str
    ) -> None:
        """An empty directory and an unmounted share look identical from here.

        One means every document was withdrawn; the other means the export is
        not there. Acting on the first reading when the second is true empties
        the corpus, so this declines to guess — the cost of being wrong the
        other way is one stale document until the next good sync.
        """
        (tmp_path / "only.txt").write_text(_BODY, encoding="utf-8")
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))

        (tmp_path / "only.txt").unlink()
        outcome = await pipeline.ingest_directory(
            tmp_path, source, await _run(admin_engine, source)
        )
        assert outcome.expired == 0
        assert await _lifecycle(admin_engine, source, "only.txt") == "active"

    async def test_an_unreadable_root_closes_the_run_rather_than_leaving_it_running(
        self, admin_engine: AsyncEngine, pipeline: IngestionPipeline, source: str
    ) -> None:
        """The same trap the connector path had.

        A run left `running` refuses every future sync of the source, so an
        export on a share that was unmounted once would need a hand-edited
        database before it could ever sync again.
        """
        run_id = await _run(admin_engine, source)
        # `NotADirectoryError`, not a silent empty walk. `Path.rglob` on a
        # missing directory yields nothing, so without an explicit check an
        # unmounted share reports a successful sync of an empty export.
        with pytest.raises(NotADirectoryError):
            await pipeline.ingest_directory(Path("/nonexistent/askau/export"), source, run_id)

        async with admin_engine.connect() as conn:
            status = (
                await conn.execute(
                    text("SELECT status FROM ingestion_runs WHERE id = CAST(:r AS uuid)"),
                    {"r": run_id},
                )
            ).scalar_one()
        assert status == "failed"
