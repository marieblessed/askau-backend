"""Per-document access control from a connector (BR-006, FR-002).

Until the connector port existed, every document in a source inherited that
source's `access_rules` — correct for a filesystem export, which has no per-file
permissions, and catastrophic for a real document store, where two documents in
one container routinely have different audiences.

These tests drive the real pipeline against a fake connector and then read
`document_acl` directly, because the question is not "did the API respond" but
"who can actually see this row afterwards". The API is the one path that was
never in doubt.

The distinction every assertion here turns on:

* `principals is None` — the source has no per-item access control. Inherit the
  source's rules.
* `principals == ()` — the connector looked and found no grantee it could map.
  **Nobody may read it.**

Collapsing those two is a one-word change (`principals or source_rules`), it
reads like a sensible default, and it publishes every unmappable document to
everyone who can reach its library.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.ingestion.connectors.ports import RemoteDocument, SourcePrincipal
from askau.ingestion.pipeline import IngestionPipeline
from askau.settings import Settings
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

#: Long enough to clear `MIN_EXTRACTED_CHARS` (200). Validation rejects thin
#: extractions as cover sheets or failed exports, which is correct behaviour and
#: not what these tests are about — a shorter fixture fails every one of them
#: for a reason unrelated to access control.
_TEXT = (
    b"Staff accrue thirty days of annual leave in each calendar year under this policy. "
    b"Leave accrues monthly from the date of appointment and may be carried over to the "
    b"following year only with the written approval of the responsible director. Unused "
    b"leave in excess of the carry-over limit lapses at the end of the leave year, and "
    b"staff on probation accrue at the same rate but may not take leave until confirmed."
)


class FakeConnector:
    """Yields documents the test has already decided the audience for.

    Deliberately not the Azure Blob connector: what is under test here is what
    the *pipeline* does with an audience, and driving it through storage mocks
    as well would make a failure ambiguous between the two.
    """

    source_type = "test"

    def __init__(self, docs: list[RemoteDocument]) -> None:
        self._docs = docs

    async def probe(self) -> dict[str, object]:
        return {"ok": True}

    async def documents(self) -> AsyncIterator[RemoteDocument]:
        for doc in self._docs:
            yield doc

    async def aclose(self) -> None:
        return None


def _doc(
    key: str, principals: tuple[SourcePrincipal, ...] | None, *, fails: bool = False
) -> RemoteDocument:
    async def fetch() -> bytes:
        return _TEXT

    return RemoteDocument(
        key=key,
        title=f"{key} policy",
        uri=f"https://auc.sharepoint.com/{key}",
        filename=f"{key}.txt",
        principals=principals,
        # Set on the document, not raised from `fetch`: that is where the real
        # connector puts it, because raising out of the enumeration would abort
        # the whole sync.
        access_error="permissions unreadable for item unreadable: 403 denied" if fails else None,
        fetch=fetch,
    )


class Harness:
    """Creates a throwaway source and cleans up after itself."""

    PREFIX = "askau-acl-test:"

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def source(self, *, access_rules: str = '{"principals": ["grp-all-staff"]}') -> str:
        async with self.engine.begin() as conn:
            owner = (await conn.execute(text("SELECT id::text FROM users LIMIT 1"))).scalar_one()
            return str(
                (
                    await conn.execute(
                        text("""
                        INSERT INTO knowledge_sources
                          (name, source_type, business_owner_id, department,
                           default_classification, location, access_rules,
                           status, approved_by, approved_at)
                        VALUES (:name, 'filesystem', CAST(:owner AS uuid), 'MISD',
                                'internal', '{}'::jsonb, CAST(:rules AS jsonb),
                                'active', CAST(:owner AS uuid), now())
                        RETURNING id
                        """),
                        {
                            "name": f"{self.PREFIX}{id(self)}",
                            "owner": owner,
                            "rules": access_rules,
                        },
                    )
                ).scalar_one()
            )

    async def run(self, source_id: str) -> str:
        async with self.engine.begin() as conn:
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

    async def acl_of(self, source_id: str, key: str) -> set[str]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("""
                    SELECT p.external_id
                    FROM documents d
                    JOIN document_families f ON f.id = d.family_id
                    JOIN document_acl a ON a.document_id = d.id
                    JOIN principals p ON p.id = a.principal_id
                    WHERE d.source_id = CAST(:s AS uuid) AND f.external_key = :k
                    """),
                    {"s": source_id, "k": key},
                )
            ).all()
        return {r[0] for r in rows}

    async def chunk_acl_of(self, source_id: str, key: str) -> set[str]:
        """The denormalised array retrieval actually reads.

        `document_acl` is authoritative, but the authorization predicate is an
        overlap against `chunks.acl_principals` (ADR-0002). Asserting only on
        the authoritative table would pass while the copy retrieval consults
        still held the old audience — which is exactly the exposure the
        denormalisation risks.
        """
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("""
                    SELECT DISTINCT p.external_id
                    FROM chunks c
                    JOIN document_families f ON f.id = c.family_id
                    JOIN principals p ON p.id = ANY(c.acl_principals)
                    WHERE f.source_id = CAST(:s AS uuid) AND f.external_key = :k
                      AND c.is_current
                    """),
                    {"s": source_id, "k": key},
                )
            ).all()
        return {r[0] for r in rows}

    async def lifecycle_of(self, source_id: str, key: str) -> str:
        async with self.engine.connect() as conn:
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

    async def current_chunks(self, source_id: str, key: str) -> int:
        """Chunks default retrieval would still consider."""
        async with self.engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text("""
                        SELECT count(*) FROM chunks c
                        JOIN document_families f ON f.id = c.family_id
                        WHERE f.source_id = CAST(:s AS uuid) AND f.external_key = :k
                          AND c.is_current
                        """),
                        {"s": source_id, "k": key},
                    )
                ).scalar_one()
            )

    async def acl_sync_lag(self, source_id: str, key: str) -> float:
        """Seconds since this document's chunks were last stamped synchronised."""
        async with self.engine.connect() as conn:
            return float(
                (
                    await conn.execute(
                        text("""
                        SELECT EXTRACT(EPOCH FROM (now() - min(c.acl_synced_at)))
                        FROM chunks c JOIN document_families f ON f.id = c.family_id
                        WHERE f.source_id = CAST(:s AS uuid) AND f.external_key = :k
                        """),
                        {"s": source_id, "k": key},
                    )
                ).scalar_one()
            )

    async def run_status(self, run_id: str) -> str:
        async with self.engine.connect() as conn:
            return str(
                (
                    await conn.execute(
                        text("SELECT status FROM ingestion_runs WHERE id = CAST(:r AS uuid)"),
                        {"r": run_id},
                    )
                ).scalar_one()
            )

    async def runs_in_progress(self, source_id: str) -> int:
        async with self.engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text("""
                        SELECT count(*) FROM ingestion_runs
                        WHERE source_id = CAST(:s AS uuid) AND status = 'running'
                        """),
                        {"s": source_id},
                    )
                ).scalar_one()
            )

    async def document_exists(self, source_id: str, key: str) -> bool:
        async with self.engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text("""
                        SELECT count(*) FROM documents d
                        JOIN document_families f ON f.id = d.family_id
                        WHERE d.source_id = CAST(:s AS uuid) AND f.external_key = :k
                        """),
                        {"s": source_id, "k": key},
                    )
                ).scalar_one()
            )

    async def cleanup(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM knowledge_sources WHERE name LIKE :m"),
                {"m": f"{self.PREFIX}%"},
            )


@pytest.fixture
async def harness(admin_engine: AsyncEngine):  # type: ignore[no-untyped-def]
    h = Harness(admin_engine)
    yield h
    await h.cleanup()


@pytest.fixture
def pipeline(admin_engine: AsyncEngine, settings: Settings, embedder):  # type: ignore[no-untyped-def]
    return IngestionPipeline(admin_engine, embedder, settings)


class TestPerDocumentAudience:
    async def test_two_documents_in_one_source_get_different_audiences(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """The property the connector exists for.

        Before this, both would have carried the source's `access_rules` and
        the confidential one would have been readable by all staff.
        """
        sid = await harness.source()
        run = await harness.run(sid)
        await pipeline.ingest_connector(
            FakeConnector(
                [
                    _doc("open", (SourcePrincipal("group", "grp-all-staff", "All Staff"),)),
                    _doc("restricted", (SourcePrincipal("group", "grp-legal", "Legal"),)),
                ]
            ),
            sid,
            run,
        )

        assert await harness.acl_of(sid, "open") == {"grp-all-staff"}
        assert await harness.acl_of(sid, "restricted") == {"grp-legal"}

    async def test_an_empty_audience_means_nobody_not_the_source_default(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """The assertion that catches `principals or source_rules`.

        The source grants `grp-all-staff`. A document the connector determined
        has no mappable grantee must end up readable by nobody — not by all
        staff because the tidy-looking fallback fired.
        """
        sid = await harness.source(access_rules='{"principals": ["grp-all-staff"]}')
        run = await harness.run(sid)
        await pipeline.ingest_connector(FakeConnector([_doc("orphan", ())]), sid, run)

        assert await harness.document_exists(sid, "orphan"), (
            "the document should be ingested and simply unreadable, not absent"
        )
        assert await harness.acl_of(sid, "orphan") == set()

    async def test_a_filesystem_source_still_inherits_the_source_rules(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """`None` and `()` must not behave the same.

        A filesystem export has no per-file permissions, so the source's rules
        are the only answer there is — and the existing corpus depends on it.
        """
        sid = await harness.source(access_rules='{"principals": ["grp-hr"]}')
        run = await harness.run(sid)
        await pipeline.ingest_connector(FakeConnector([_doc("inherited", None)]), sid, run)

        assert await harness.acl_of(sid, "inherited") == {"grp-hr"}

    async def test_an_unknown_grantee_is_created_rather_than_dropped(
        self, harness: Harness, pipeline: IngestionPipeline, admin_engine: AsyncEngine
    ) -> None:
        """A grantee the directory has and we have never seen is a normal state
        — somebody joined, a group was created.

        Dropping the grant because the principal row is missing would make
        access depend on the order two systems happened to be synced in.
        """
        sid = await harness.source()
        run = await harness.run(sid)
        novel = SourcePrincipal("group", "aad-group-brand-new", "Newly Created Team")
        await pipeline.ingest_connector(FakeConnector([_doc("fresh", (novel,))]), sid, run)

        assert await harness.acl_of(sid, "fresh") == {"aad-group-brand-new"}
        async with admin_engine.connect() as conn:
            name = (
                await conn.execute(
                    text("SELECT display_name FROM principals WHERE external_id = :e"),
                    {"e": "aad-group-brand-new"},
                )
            ).scalar_one()
        assert name == "Newly Created Team"

    async def test_a_revoked_grant_disappears_on_the_next_sync(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """An ACL that only ever grows is an ACL that never revokes.

        Two failures are covered, and the second is the subtle one.

        A plain `INSERT ... ON CONFLICT DO NOTHING` would leave a removed
        grantee with access forever. And scoping the replacement to the newest
        revision would leave a hole with a simple shape: somebody whose access
        was revoked could still retrieve *last year's* version of the same
        policy through `include_historical`. A family is one item in the source
        repository and its permissions are on the item, not on each revision, so
        the item's current permissions govern its history too.
        """
        sid = await harness.source()
        both = (
            SourcePrincipal("group", "grp-hr", "HR"),
            SourcePrincipal("group", "grp-legal", "Legal"),
        )
        await pipeline.ingest_connector(
            FakeConnector([_doc("shrinking", both)]), sid, await harness.run(sid)
        )
        assert await harness.acl_of(sid, "shrinking") == {"grp-hr", "grp-legal"}

        # Re-ingested with one grant removed. The content changes so the
        # unchanged-hash short circuit does not skip the write.
        narrowed = _doc("shrinking", (SourcePrincipal("group", "grp-hr", "HR"),))

        async def changed() -> bytes:
            return _TEXT + b" Amended in a later revision."

        object.__setattr__(narrowed, "fetch", changed)
        await pipeline.ingest_connector(FakeConnector([narrowed]), sid, await harness.run(sid))

        # `acl_of` spans every revision in the family on purpose: if the
        # superseded version kept `grp-legal`, this is where it shows.
        assert await harness.acl_of(sid, "shrinking") == {"grp-hr"}

        async with harness.engine.connect() as conn:
            total, superseded = (
                await conn.execute(
                    text("""
                    SELECT count(*), count(*) FILTER (WHERE d.lifecycle = 'superseded')
                    FROM documents d JOIN document_families f ON f.id = d.family_id
                    WHERE d.source_id = CAST(:s AS uuid) AND f.external_key = 'shrinking'
                    """),
                    {"s": sid},
                )
            ).one()
        # Two revisions kept, the older superseded. Both halves were broken:
        # `version_seq` defaulted to 1 and nothing set it, so a second ingest of
        # changed content failed on the unique constraint and re-sync did not
        # work at all — which also left `lifecycle = 'superseded'` with no way
        # to arise, and so the `outdated` answer state with no producer.
        assert (total, superseded) == (2, 1)


class TestFailuresAreReported:
    async def test_a_document_whose_access_is_unknown_is_failed_not_skipped(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """An administrator must see "1 document could not be assessed".

        Skipping quietly would leave a gap in the corpus with no explanation,
        and the alternative failure — ingesting it under a default — is the
        disclosure this whole design avoids.
        """
        sid = await harness.source()
        outcome = await pipeline.ingest_connector(
            FakeConnector([_doc("unreadable", (), fails=True)]), sid, await harness.run(sid)
        )
        assert outcome.failed == 1
        assert not await harness.document_exists(sid, "unreadable")
        [(name, failure)] = outcome.failures
        assert name == "unreadable.txt"
        # The remedy has to name the action, because the person reading it is
        # an administrator who cannot see the exception.
        assert "permission" in failure.remedy.lower()

    async def test_one_failure_does_not_abandon_the_run(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        sid = await harness.source()
        outcome = await pipeline.ingest_connector(
            FakeConnector(
                [
                    _doc("bad", (), fails=True),
                    _doc("good", (SourcePrincipal("group", "grp-hr", "HR"),)),
                ]
            ),
            sid,
            await harness.run(sid),
        )
        assert outcome.failed == 1
        assert outcome.indexed == 1
        assert await harness.acl_of(sid, "good") == {"grp-hr"}


class TestPermissionOnlyChanges:
    """A permission changed with the content untouched.

    The most common change a document repository sees, and the one the content
    hash is blind to: somebody leaves a team, a library is re-shared, an
    over-broad grant is tightened. None of it alters a byte of the file.
    """

    async def test_a_revocation_lands_even_though_the_content_is_identical(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """`_unchanged` exists to avoid re-extracting and re-embedding, which is
        expensive and pointless for a byte-identical file. It must not also skip
        the access list.

        If it does, the failure is permanent and silent: the revoked grantee
        keeps access until somebody happens to edit the document. `document_acl`
        stays stale, and the reconciler faithfully copies the stale value into
        `chunks.acl_principals`, so the denormalised copy agrees with the
        authoritative table and nothing anywhere looks wrong.
        """
        sid = await harness.source()
        both = (
            SourcePrincipal("group", "grp-hr", "HR"),
            SourcePrincipal("group", "grp-legal", "Legal"),
        )
        await pipeline.ingest_connector(
            FakeConnector([_doc("static", both)]), sid, await harness.run(sid)
        )
        assert await harness.acl_of(sid, "static") == {"grp-hr", "grp-legal"}

        # Same bytes. Only the audience changed.
        narrowed = _doc("static", (SourcePrincipal("group", "grp-hr", "HR"),))
        outcome = await pipeline.ingest_connector(
            FakeConnector([narrowed]), sid, await harness.run(sid)
        )

        assert outcome.skipped == 1, "the content really is unchanged; re-indexing it is waste"
        assert await harness.acl_of(sid, "static") == {"grp-hr"}
        # The array retrieval reads, not only the authoritative table. Updating
        # one without the other leaves the predicate answering from the old
        # audience while every table looks consistent.
        assert await harness.chunk_acl_of(sid, "static") == {"grp-hr"}
        # And the chunks are stamped as synchronised. `acl_synced_at` is what
        # `AclReconciler.max_lag_seconds` measures, and the runbook alerts on it
        # to catch outstanding revocations — chunks whose ACL was just rewritten
        # but not stamped would read as permanently stale, which is an alert
        # that fires for ever and therefore an alert nobody reads.
        assert await harness.acl_sync_lag(sid, "static") < 60

    async def test_a_grant_added_to_unchanged_content_also_lands(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """The other direction. Less urgent than a revocation — somebody cannot
        yet see what they are entitled to, rather than still seeing what they
        are not — but a sync that only ever narrows would be its own puzzle."""
        sid = await harness.source()
        await pipeline.ingest_connector(
            FakeConnector([_doc("widening", (SourcePrincipal("group", "grp-hr", "HR"),))]),
            sid,
            await harness.run(sid),
        )
        widened = _doc(
            "widening",
            (
                SourcePrincipal("group", "grp-hr", "HR"),
                SourcePrincipal("group", "grp-finance", "Finance"),
            ),
        )
        await pipeline.ingest_connector(FakeConnector([widened]), sid, await harness.run(sid))
        assert await harness.acl_of(sid, "widening") == {"grp-hr", "grp-finance"}
        assert await harness.chunk_acl_of(sid, "widening") == {"grp-hr", "grp-finance"}

    async def test_a_filesystem_source_keeps_the_source_rules_across_syncs(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """`principals is None` still means "inherit the source's rules".

        Stated precisely, because a mutation showed this test claims less than
        its first name suggested: running the refresh for a filesystem source
        anyway is *behaviourally identical* — `_write_acl(None)` re-derives the
        same rules and inserts nothing new — so this asserts the outcome, not
        that the branch was skipped. The branch exists to avoid rewriting every
        unchanged file's ACL on every sync, which is waste rather than
        incorrectness, and catching it would mean counting queries for a benefit
        that does not justify the coupling.
        """
        sid = await harness.source(access_rules='{"principals": ["grp-hr"]}')
        await pipeline.ingest_connector(
            FakeConnector([_doc("plain", None)]), sid, await harness.run(sid)
        )
        outcome = await pipeline.ingest_connector(
            FakeConnector([_doc("plain", None)]), sid, await harness.run(sid)
        )
        assert outcome.skipped == 1
        assert await harness.acl_of(sid, "plain") == {"grp-hr"}


class TestDocumentsRemovedAtTheSource:
    """A document that has disappeared from the library.

    For a policy assistant this is not housekeeping. A rescinded policy that
    stays in the corpus keeps being retrieved and cited as current, and the
    reader has no way to tell — the citation looks exactly like a live one.
    """

    async def test_a_document_absent_from_a_complete_sync_is_expired(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)
        await pipeline.ingest_connector(
            FakeConnector([_doc("kept", grant), _doc("withdrawn", grant)]),
            sid,
            await harness.run(sid),
        )
        assert await harness.document_exists(sid, "withdrawn")

        # The second sync sees only one of them.
        outcome = await pipeline.ingest_connector(
            FakeConnector([_doc("kept", grant)]), sid, await harness.run(sid)
        )
        assert outcome.expired == 1

        assert await harness.lifecycle_of(sid, "withdrawn") == "expired"
        assert await harness.lifecycle_of(sid, "kept") == "active"

    async def test_an_expired_document_leaves_default_retrieval(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """Marking the row is half the job.

        A document expired in `documents` whose chunks still say `is_current` is
        expired in the record and live in the index — and the index is what
        answers questions. The row would satisfy an auditor and change nothing
        for a reader.
        """
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)
        await pipeline.ingest_connector(
            FakeConnector([_doc("gone", grant), _doc("stays", grant)]),
            sid,
            await harness.run(sid),
        )
        await pipeline.ingest_connector(
            FakeConnector([_doc("stays", grant)]), sid, await harness.run(sid)
        )
        assert await harness.current_chunks(sid, "gone") == 0
        assert await harness.current_chunks(sid, "stays") > 0

    async def test_the_document_is_expired_and_not_deleted(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """It was real and it was cited.

        An audit row naming it must still resolve, and FR-018 makes historical
        content retrievable on request. Deleting would break both to save a row.
        """
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)
        await pipeline.ingest_connector(
            FakeConnector([_doc("historic", grant), _doc("live", grant)]),
            sid,
            await harness.run(sid),
        )
        await pipeline.ingest_connector(
            FakeConnector([_doc("live", grant)]), sid, await harness.run(sid)
        )
        assert await harness.document_exists(sid, "historic")


class TestExpiryIsSafeWhenASyncFails:
    """The condition the whole feature rests on.

    "Absent from this run" and "deleted at the source" are the same observation
    and only the same fact when the run finished. Getting this wrong means the
    most destructive possible outcome — expiring most of the corpus — is
    triggered by the most ordinary possible failure.
    """

    async def test_an_enumeration_that_dies_partway_expires_nothing(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)
        await pipeline.ingest_connector(
            FakeConnector([_doc("one", grant), _doc("two", grant), _doc("three", grant)]),
            sid,
            await harness.run(sid),
        )

        class DiesPartway(FakeConnector):
            async def documents(self):  # type: ignore[no-untyped-def]
                yield _doc("one", grant)
                raise ConnectionError("throttled out mid-enumeration")

        dead_run = await harness.run(sid)
        with pytest.raises(ConnectionError):
            await pipeline.ingest_connector(DiesPartway([]), sid, dead_run)

        # `two` and `three` were never reached. They are not gone — they were
        # not looked at, and a sync that cannot tell the difference must not act.
        for key in ("one", "two", "three"):
            assert await harness.lifecycle_of(sid, key) == "active", (
                f"{key} was expired by a sync that never finished"
            )

        # And the run is closed. This is the part that used to brick the source:
        # `_close_run` was unreachable when the enumeration raised, so the row
        # stayed `running` — and `_start_run` refuses a second run while one is
        # in progress, so the source could never be synced again.
        assert await harness.run_status(dead_run) == "failed"

    async def test_a_failed_sync_does_not_block_the_next_one(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """The consequence of the row above, stated as the thing an operator
        cares about: a throttle must cost one run, not the source."""
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)

        class Broken(FakeConnector):
            async def documents(self):  # type: ignore[no-untyped-def]
                raise ConnectionError("throttled")
                yield  # pragma: no cover - unreachable, makes this a generator

        with pytest.raises(ConnectionError):
            await pipeline.ingest_connector(Broken([]), sid, await harness.run(sid))

        assert await harness.runs_in_progress(sid) == 0, (
            "a dead run left in progress refuses every future sync of this source"
        )
        outcome = await pipeline.ingest_connector(
            FakeConnector([_doc("after", grant)]), sid, await harness.run(sid)
        )
        assert outcome.indexed == 1

    async def test_an_empty_enumeration_expires_nothing(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """A library that legitimately returns zero items is indistinguishable
        from a misconfigured one that lists nothing, and the two demand opposite
        actions. Declining to guess costs a stale document; guessing wrong costs
        the corpus."""
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)
        await pipeline.ingest_connector(
            FakeConnector([_doc("survivor", grant)]), sid, await harness.run(sid)
        )

        outcome = await pipeline.ingest_connector(FakeConnector([]), sid, await harness.run(sid))
        assert outcome.expired == 0
        assert await harness.lifecycle_of(sid, "survivor") == "active"

    async def test_a_document_that_failed_this_run_is_not_expired(
        self, harness: Harness, pipeline: IngestionPipeline
    ) -> None:
        """It was seen. That it could not be assessed is a different problem
        from it being gone, and conflating the two would quietly retire every
        document whose permissions the application lost the right to read."""
        sid = await harness.source()
        grant = (SourcePrincipal("group", "grp-hr", "HR"),)
        await pipeline.ingest_connector(
            FakeConnector([_doc("fragile", grant)]), sid, await harness.run(sid)
        )
        outcome = await pipeline.ingest_connector(
            FakeConnector([_doc("fragile", (), fails=True)]), sid, await harness.run(sid)
        )
        assert outcome.failed == 1
        assert outcome.expired == 0
        assert await harness.lifecycle_of(sid, "fragile") == "active"
