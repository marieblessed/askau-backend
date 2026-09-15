"""Expiry has to reach retrieval, not only the record.

`_expire_absent` marks a document `expired` and clears `is_current` on its
chunks. Whether that actually stops the document being cited is a separate
question from whether the columns changed, and only one of them matters to a
reader.

Worth stating why this is its own file rather than an assertion tacked onto the
expiry tests: those verify the pipeline's writes, and passing them proves the
bookkeeping. This verifies the *consequence* — that a withdrawn policy stops
being retrieved — which is the thing the feature was built for and the thing a
future change to `VersionPolicy` could silently undo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.domain.authz import AuthorizationContext, PrincipalId, UserId
from askau.domain.retrieval import RetrievalQuery
from askau.ingestion.pipeline import IngestionPipeline
from askau.retrieval.adapters.pgvector_hybrid import PgVectorHybridRetriever
from askau.settings import Settings
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

_PREFIX = "askau-expiry-test:"

_UNIQUE = "zanzibar quorum protocol"
_BODY = (
    f"The {_UNIQUE} governs how the standing committee reaches a decision when fewer than "
    "half of its members are present at a convened sitting. A sitting that fails to reach "
    "the threshold is adjourned and reconvened within fourteen days, at which point the "
    "quorum requirement is reduced by one third. Decisions taken under the reduced quorum "
    "are provisional until ratified at the next full sitting of the committee."
)


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


async def _all_staff_context(engine: AsyncEngine) -> AuthorizationContext:
    async with engine.connect() as conn:
        principal = (
            await conn.execute(
                text("SELECT id FROM principals WHERE external_id = 'grp-all-staff'")
            )
        ).scalar_one()
    return AuthorizationContext(
        user_id=UserId("expiry-test"),
        principals=frozenset({PrincipalId(int(principal))}),
        acl_version=1,
    )


async def _search(engine: AsyncEngine, settings: Settings, embedder, authz, *, historical: bool):  # type: ignore[no-untyped-def]
    return await PgVectorHybridRetriever(engine, settings).search(
        RetrievalQuery(
            text=_UNIQUE,
            embedding=await embedder.embed_query(_UNIQUE),
            authz=authz,
            include_historical=historical,
        )
    )


class TestExpiryReachesRetrieval:
    async def test_a_withdrawn_document_stops_being_retrieved(
        self,
        tmp_path: Path,
        admin_engine: AsyncEngine,
        pipeline: IngestionPipeline,
        source: str,
        settings: Settings,
        embedder,  # type: ignore[no-untyped-def]
    ) -> None:
        """The whole point of expiry, asserted where a reader would feel it.

        Marking the row is bookkeeping. This is the consequence — and it is the
        half a change to `VersionPolicy` could silently undo without failing any
        of the pipeline's own tests.
        """
        (tmp_path / "quorum.txt").write_text(_BODY, encoding="utf-8")
        # A second file that stays. Deleting the only document leaves an empty
        # directory, and an empty enumeration expires nothing *by design* — so
        # a single-file version of this test asserts against its own setup
        # rather than against expiry. It failed exactly that way first.
        (tmp_path / "keeper.txt").write_text(
            _BODY.replace(_UNIQUE, "standing committee attendance rule"), encoding="utf-8"
        )
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))

        authz = await _all_staff_context(admin_engine)
        found = await _search(admin_engine, settings, embedder, authz, historical=False)
        assert any(_UNIQUE in c.content for c in found.chunks), (
            "the document was not retrievable even before expiry; the test proves nothing"
        )

        (tmp_path / "quorum.txt").unlink()
        outcome = await pipeline.ingest_directory(
            tmp_path, source, await _run(admin_engine, source)
        )
        assert outcome.expired == 1, "expiry did not fire; the retrieval assertion below is moot"

        after = await _search(admin_engine, settings, embedder, authz, historical=False)
        assert not any(_UNIQUE in c.content for c in after.chunks), (
            "a withdrawn document is still being retrieved and would be cited as current"
        )

    async def test_it_is_still_reachable_when_history_is_requested(
        self,
        tmp_path: Path,
        admin_engine: AsyncEngine,
        pipeline: IngestionPipeline,
        source: str,
        settings: Settings,
        embedder,  # type: ignore[no-untyped-def]
    ) -> None:
        """Expired, not deleted (FR-018).

        Somebody asking what the rule *was* should still find it. If expiry made
        the content unreachable entirely, "expired rather than deleted" would be
        a distinction with no consequence.
        """
        (tmp_path / "quorum.txt").write_text(_BODY, encoding="utf-8")
        (tmp_path / "keeper.txt").write_text(
            _BODY.replace(_UNIQUE, "standing committee attendance rule"), encoding="utf-8"
        )
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))
        (tmp_path / "quorum.txt").unlink()
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))

        authz = await _all_staff_context(admin_engine)
        current = await _search(admin_engine, settings, embedder, authz, historical=False)
        historical = await _search(admin_engine, settings, embedder, authz, historical=True)

        assert not any(_UNIQUE in c.content for c in current.chunks)
        assert any(_UNIQUE in c.content for c in historical.chunks), (
            "expired content is unreachable even on request, so nothing was preserved"
        )


class TestSupersededContentIsLabelledAsSuch:
    """`chunks.lifecycle` must follow the document's.

    The chunk carries its own copy, and retrieval reads *that* one — the
    citation's lifecycle, and therefore the "Superseded" status the interface
    shows, comes from the chunk and never from `documents`.

    Both places that retire a document (`_write` superseding an older revision,
    `_expire_absent` retiring a withdrawn one) cleared `is_current` and left the
    chunk's lifecycle at `active`. That is not a disclosure — `is_current` still
    keeps the content out of default retrieval — but it has a consequence:
    material fetched with `include_historical` comes back labelled *Active*, and
    `outdated_from_citations` promotes an answer to the `outdated` state by
    looking for exactly that label. So the banner their UI renders for
    "based on an older approved document" could never fire.

    ADR-0022 recorded `outdated` as having no producer because versioning was
    broken. Versioning was fixed; this is the second reason, and it survived the
    first fix.
    """

    async def test_a_superseded_revision_labels_its_chunks_superseded(
        self,
        tmp_path: Path,
        admin_engine: AsyncEngine,
        pipeline: IngestionPipeline,
        source: str,
    ) -> None:
        (tmp_path / "policy.txt").write_text(_BODY, encoding="utf-8")
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))
        (tmp_path / "policy.txt").write_text(
            _BODY + " Amended by the twelfth revision of the committee rules.",
            encoding="utf-8",
        )
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))

        async with admin_engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                    SELECT d.version_seq, d.lifecycle::text AS doc_lifecycle,
                           c.lifecycle::text AS chunk_lifecycle, c.is_current
                    FROM documents d JOIN chunks c ON c.document_id = d.id
                    JOIN document_families f ON f.id = d.family_id
                    WHERE d.source_id = CAST(:s AS uuid) AND f.external_key = 'policy.txt'
                    ORDER BY d.version_seq
                    """),
                        {"s": source},
                    )
                )
                .mappings()
                .all()
            )

        assert rows, "no chunks were written"
        old = [r for r in rows if r["version_seq"] == 1]
        assert old, "the first revision was not kept"
        for row in old:
            assert row["doc_lifecycle"] == "superseded"
            assert row["is_current"] is False
            assert row["chunk_lifecycle"] == "superseded", (
                "the chunk still says 'active', so a citation to it renders as current "
                "and the 'outdated' answer state can never fire"
            )

    async def test_an_expired_document_labels_its_chunks_expired(
        self,
        tmp_path: Path,
        admin_engine: AsyncEngine,
        pipeline: IngestionPipeline,
        source: str,
    ) -> None:
        (tmp_path / "gone.txt").write_text(_BODY, encoding="utf-8")
        (tmp_path / "stays.txt").write_text(
            _BODY.replace(_UNIQUE, "standing committee attendance rule"), encoding="utf-8"
        )
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))
        (tmp_path / "gone.txt").unlink()
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))

        async with admin_engine.connect() as conn:
            lifecycles = {
                r[0]
                for r in (
                    await conn.execute(
                        text("""
                        SELECT DISTINCT c.lifecycle::text FROM chunks c
                        JOIN document_families f ON f.id = c.family_id
                        WHERE f.source_id = CAST(:s AS uuid) AND f.external_key = 'gone.txt'
                        """),
                        {"s": source},
                    )
                ).all()
            }
        assert lifecycles == {"expired"}


class TestTheOutdatedAnswerStateHasAProducer:
    """`outdated` was in the client's UI with nothing able to reach it.

    Their `AIMessage` renders a banner — *"Based on an older approved
    document"* — and the branch was unreachable for two independent reasons,
    each of which hid the other:

    1. `documents.version_seq` defaulted to 1 and nothing set it, so a second
       ingest of changed content failed on the unique constraint and no document
       ever became `superseded` (ADR-0022).
    2. `chunks.lifecycle` never followed the document's, so even a superseded
       document returned citations labelled *Active* — and
       `outdated_from_citations` promotes an answer by looking for exactly that
       label.

    Fixing the first did not fix the second, which is why this test exists at
    the level a reader would experience: the state, not the columns.
    """

    async def test_an_answer_resting_on_a_superseded_document_is_outdated(
        self,
        tmp_path: Path,
        admin_engine: AsyncEngine,
        pipeline: IngestionPipeline,
        source: str,
    ) -> None:
        from askau.api.mappers import citation_to_source, outdated_from_citations
        from askau.domain.conversation import Citation

        (tmp_path / "policy.txt").write_text(_BODY, encoding="utf-8")
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))
        (tmp_path / "policy.txt").write_text(
            _BODY + " Amended by the twelfth revision of the committee rules.",
            encoding="utf-8",
        )
        await pipeline.ingest_directory(tmp_path, source, await _run(admin_engine, source))

        async with admin_engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("""
                    SELECT c.id AS chunk_id, d.id::text AS document_id, d.title,
                           c.lifecycle::text AS lifecycle, c.classification::text AS cls
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    JOIN document_families f ON f.id = d.family_id
                    WHERE d.source_id = CAST(:s AS uuid)
                      AND f.external_key = 'policy.txt' AND d.version_seq = 1
                    LIMIT 1
                    """),
                        {"s": source},
                    )
                )
                .mappings()
                .one()
            )

        # Built from what retrieval would actually return for the superseded
        # revision, so the label under test is the one the corpus produces
        # rather than one the test asserts into existence.
        from askau.domain.enums import Classification

        citation = Citation(
            marker=1,
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            document_title=row["title"],
            source_name="test",
            classification=Classification(row["cls"]),
            lifecycle=row["lifecycle"],
            source_uri="https://example.invalid/policy",
            rank=1,
        )
        source_card = citation_to_source(citation)
        assert source_card.status == "Superseded", (
            f"the citation renders as {source_card.status!r}, so the banner cannot fire"
        )
        assert outdated_from_citations("grounded", [source_card]) == "outdated"
