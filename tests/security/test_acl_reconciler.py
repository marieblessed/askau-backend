"""ACL reconciliation — FR-025, the security control behind ADR-0002.

The denormalization that makes retrieval fast is only correct while
`chunks.acl_principals` agrees with `document_acl`. These tests are what stand
between that assumption and a stale permission serving a document to someone
who no longer has access.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.ingestion.acl_reconciler import AclReconciler, BlindReconcilerError
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, pytest.mark.security, requires_db]


async def _document_and_principal(engine: AsyncEngine) -> tuple[str, int]:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("""
                SELECT d.id::text AS doc, a.principal_id
                FROM documents d
                JOIN document_acl a ON a.document_id = d.id
                JOIN chunks c ON c.document_id = d.id
                WHERE d.title = 'Budget Reallocation Procedure'
                  -- A principal somebody actually holds.
                  --
                  -- This was an unordered LIMIT 1 over the document's whole
                  -- ACL, which assumed every principal has members. It does
                  -- not: a group that exists because a directory names it, but
                  -- which nobody has signed in under yet, holds a grant and no
                  -- users. Picking that one made
                  -- `test_cache_invalidation_accompanies_the_rewrite` compare
                  -- 0 against 0 and fail, intermittently, depending on row
                  -- order.
                  AND EXISTS (
                    SELECT 1 FROM user_principals up WHERE up.principal_id = a.principal_id
                  )
                ORDER BY a.principal_id
                LIMIT 1
                """)
                )
            )
            .mappings()
            .one()
        )
    return row["doc"], row["principal_id"]


async def _chunk_acls(engine: AsyncEngine, document_id: str) -> list[list[int]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT acl_principals FROM chunks WHERE document_id = CAST(:d AS uuid)"),
                    {"d": document_id},
                )
            )
            .scalars()
            .all()
        )
    return [list(r) for r in rows]


class TestRevocation:
    async def test_a_revoked_principal_is_removed_from_chunks(
        self, admin_engine: AsyncEngine
    ) -> None:
        """The exposure case. Removing a grant from `document_acl` must reach
        the materialized copy, or retrieval keeps returning the document."""
        doc, principal = await _document_and_principal(admin_engine)
        assert all(principal in acl for acl in await _chunk_acls(admin_engine, doc))

        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                DELETE FROM document_acl
                WHERE document_id = CAST(:d AS uuid) AND principal_id = :p
                """),
                {"d": doc, "p": principal},
            )
        try:
            # Before reconciliation the chunks are stale — that is the window
            # FR-025 permits, and exactly what makes the reconciler necessary.
            assert any(principal in acl for acl in await _chunk_acls(admin_engine, doc))

            report = await AclReconciler(admin_engine).reconcile()
            assert report.revocations >= 1
            assert all(principal not in acl for acl in await _chunk_acls(admin_engine, doc))
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO document_acl (document_id, principal_id)
                    VALUES (CAST(:d AS uuid), :p) ON CONFLICT DO NOTHING
                    """),
                    {"d": doc, "p": principal},
                )
            await AclReconciler(admin_engine).reconcile()

    async def test_revocations_are_ordered_before_grants(self, admin_engine: AsyncEngine) -> None:
        """Asymmetric urgency: a missing grant inconveniences, a missing
        revocation exposes. An interrupted pass must have closed the exposures."""
        doc, principal = await _document_and_principal(admin_engine)
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                DELETE FROM document_acl
                WHERE document_id = CAST(:d AS uuid) AND principal_id = :p
                """),
                {"d": doc, "p": principal},
            )
        try:
            revocations, grants = await AclReconciler(admin_engine).find_drift()
            assert doc in revocations
            assert doc not in grants
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO document_acl (document_id, principal_id)
                    VALUES (CAST(:d AS uuid), :p) ON CONFLICT DO NOTHING
                    """),
                    {"d": doc, "p": principal},
                )
            await AclReconciler(admin_engine).reconcile()


class TestGrant:
    async def test_a_new_grant_reaches_chunks(self, admin_engine: AsyncEngine) -> None:
        doc, _ = await _document_and_principal(admin_engine)
        async with admin_engine.connect() as conn:
            outsider = (
                await conn.execute(
                    text("""
                    SELECT p.id FROM principals p
                    WHERE p.external_id = 'grp-misd'
                      AND NOT EXISTS (
                        SELECT 1 FROM document_acl a
                        WHERE a.document_id = CAST(:d AS uuid) AND a.principal_id = p.id)
                    """),
                    {"d": doc},
                )
            ).scalar_one()

        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO document_acl (document_id, principal_id)
                VALUES (CAST(:d AS uuid), :p)
                """),
                {"d": doc, "p": outsider},
            )
        try:
            await AclReconciler(admin_engine).reconcile()
            assert all(outsider in acl for acl in await _chunk_acls(admin_engine, doc))
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("""
                    DELETE FROM document_acl
                    WHERE document_id = CAST(:d AS uuid) AND principal_id = :p
                    """),
                    {"d": doc, "p": outsider},
                )
            await AclReconciler(admin_engine).reconcile()


class TestProperties:
    async def test_reconciling_a_settled_corpus_changes_nothing(
        self, admin_engine: AsyncEngine
    ) -> None:
        """Idempotent: it recomputes from the authoritative table rather than
        applying a diff, so a missed run is repaired by the next one."""
        reconciler = AclReconciler(admin_engine)
        await reconciler.reconcile()
        assert not (await reconciler.reconcile()).changed

    async def test_cache_invalidation_accompanies_the_rewrite(
        self, admin_engine: AsyncEngine
    ) -> None:
        """Rewriting chunk rows is half the job. A cached authorization context
        would keep serving the old permissions until it expired."""
        doc, principal = await _document_and_principal(admin_engine)
        async with admin_engine.connect() as conn:
            before = (
                await conn.execute(
                    text("""
                    SELECT coalesce(sum(acl_version), 0) FROM users u
                    JOIN user_principals up ON up.user_id = u.id
                    WHERE up.principal_id = :p
                    """),
                    {"p": principal},
                )
            ).scalar_one()

        await AclReconciler(admin_engine).reconcile_document(doc)

        async with admin_engine.connect() as conn:
            after = (
                await conn.execute(
                    text("""
                    SELECT coalesce(sum(acl_version), 0) FROM users u
                    JOIN user_principals up ON up.user_id = u.id
                    WHERE up.principal_id = :p
                    """),
                    {"p": principal},
                )
            ).scalar_one()
        assert after > before

    async def test_lag_is_measurable(self, admin_engine: AsyncEngine) -> None:
        """The figure the runbook alerts on."""
        lag = await AclReconciler(admin_engine).max_lag_seconds()
        assert lag is not None and lag >= 0


class TestTheReconcilerRefusesToRunBlind:
    """The failure this suite could not have caught, because of how it is set up.

    Every test above uses `admin_engine` — the migration role, which RLS does
    not filter. In production the reconciler would be handed the application
    connection, and `askau_app` has exactly one SELECT policy on `chunks`: an
    overlap against the session principals. A maintenance pass sets none.

    So the reconciler read **zero chunks**, found no drift, and reported
    "nothing to do" — indistinguishable in every log and every metric from a
    perfectly consistent corpus. For the component whose whole job is closing
    revocation windows, a silent success means the window never closes and
    nobody is told.

    Found by running it for real rather than by reading it, which is the only
    way this class of bug surfaces: the code is correct, the tests pass, and the
    privilege it runs under is wrong.
    """

    async def test_it_raises_rather_than_reporting_a_clean_corpus(
        self, engine: AsyncEngine
    ) -> None:
        """`engine` is the *application* engine, deliberately — the one it would
        actually be given."""
        with pytest.raises(BlindReconcilerError) as caught:
            await AclReconciler(engine).reconcile()

        # The message has to name the cause and the fix. Whoever reads it is
        # looking at a failed maintenance job with no other clue.
        message = str(caught.value)
        assert "row-level security" in message.lower()
        assert "migration" in message.lower() or "owner" in message.lower()

    async def test_find_drift_refuses_too(self, engine: AsyncEngine) -> None:
        """Both entry points, because `find_drift` is what the runbook calls to
        decide whether a reconcile is needed — and it answering "no drift" is
        the same lie one level earlier."""
        with pytest.raises(BlindReconcilerError):
            await AclReconciler(engine).find_drift()

    async def test_a_privileged_connection_still_works(self, admin_engine: AsyncEngine) -> None:
        """The guard must not break the supported configuration."""
        report = await AclReconciler(admin_engine).reconcile()
        assert report is not None

    async def test_an_empty_corpus_is_not_treated_as_blindness(
        self, admin_engine: AsyncEngine
    ) -> None:
        """A fresh deployment has no chunks and no indexed documents, and that
        is a legitimate state.

        The guard therefore asks "are there indexed documents whose chunks I
        cannot see", not "is chunks empty" — otherwise the first reconcile pass
        on a new environment would fail with a security error.
        """
        async with admin_engine.connect() as conn:
            indexed = (
                await conn.execute(text("SELECT count(*) FROM documents WHERE chunk_count > 0"))
            ).scalar_one()
        if indexed:
            pytest.skip("the seeded corpus is not empty; the empty case is covered by the guard")
        assert (await AclReconciler(admin_engine).reconcile()) is not None
