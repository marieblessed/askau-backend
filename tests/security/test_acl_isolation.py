"""TC-SEC-* — the authorization isolation suite.

The only suite whose target is zero, which is why it runs in full on every
change rather than being sampled. Each test asserts *absence*: that a document
does not appear. Tests that only assert presence cannot detect a leak.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.domain.authz import AuthorizationContext
from askau.domain.retrieval import RetrievalQuery
from askau.llm.ports import Embedder
from askau.retrieval.adapters.pgvector_hybrid import PgVectorHybridRetriever
from askau.scripts.corpus import MUST_NOT_RETRIEVE
from tests.conftest import requires_db

pytestmark = [pytest.mark.security, pytest.mark.integration, requires_db]

AuthzFactory = Callable[[str], Awaitable[AuthorizationContext]]
DocResolver = Callable[[str], Awaitable[str]]

# Deliberately broad: phrased to pull from every classification tier at once, so
# a leak has the best possible chance of surfacing.
PROBE = (
    "budget reallocation thresholds, disciplinary procedure, mission security, "
    "executive deliberations, salary bands and travel allowances"
)


async def _search(
    retriever: PgVectorHybridRetriever,
    embedder: Embedder,
    authz: AuthorizationContext,
    question: str = PROBE,
    **kw: object,
) -> tuple[str, ...]:
    result = await retriever.search(
        RetrievalQuery(
            text=question,
            embedding=await embedder.embed_query(question),
            authz=authz,
            candidate_k=200,
            top_k=100,
            ts_config="english",
            **kw,  # type: ignore[arg-type]
        )
    )
    return tuple(c.document_title for c in result.chunks)


class TestCrossDepartmentIsolation:
    """TC-SEC-003 / TC-SEC-004 — the core requirement (FR-023, FR-024, BR-005)."""

    @pytest.mark.parametrize("username", sorted(MUST_NOT_RETRIEVE))
    async def test_forbidden_documents_never_retrieved(
        self,
        username: str,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
        admin_engine: AsyncEngine,
    ) -> None:
        authz = await authz_for(username)
        titles = await _search(retriever, embedder, authz)

        async with admin_engine.connect() as conn:
            forbidden = {
                str(
                    (
                        await conn.execute(
                            text("SELECT title FROM documents WHERE source_uri LIKE :p"),
                            {"p": f"%/{key}"},
                        )
                    ).scalar_one()
                )
                for key in MUST_NOT_RETRIEVE[username]
            }

        leaked = forbidden.intersection(titles)
        assert not leaked, f"{username} retrieved forbidden documents: {sorted(leaked)}"

    async def test_finance_sees_finance_confidential(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        """The positive control. Without it, a retriever that returns nothing at
        all would pass every absence assertion above."""
        titles = await _search(retriever, embedder, await authz_for("staff.finance"))
        assert "Budget Reallocation Procedure" in titles

    async def test_two_identities_get_different_results(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        finance = set(await _search(retriever, embedder, await authz_for("staff.finance")))
        misd = set(await _search(retriever, embedder, await authz_for("staff.misd")))
        assert "Budget Reallocation Procedure" in finance - misd

    async def test_least_privileged_user_sees_no_restricted_tier(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        authz = await authz_for("staff.new")
        result = await retriever.search(
            RetrievalQuery(
                text=PROBE,
                embedding=await embedder.embed_query(PROBE),
                authz=authz,
                candidate_k=200,
                top_k=100,
                ts_config="english",
            )
        )
        tiers = {c.classification.value for c in result.chunks}
        assert tiers <= {"public", "internal"}, f"leaked tiers: {tiers}"


class TestBothRetrievalArms:
    """A filter on one arm only is a leak on the other."""

    async def test_keyword_arm_alone_cannot_leak(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        """An exact-phrase query hits the keyword arm hard, bypassing semantic
        similarity entirely — the path a semantic-only ACL filter would miss."""
        titles = await _search(
            retriever,
            embedder,
            await authz_for("staff.misd"),
            question="Sub-Committee on Budget Matters endorsement reallocation appropriations",
        )
        assert "Budget Reallocation Procedure" not in titles

    async def test_verbatim_restricted_phrase_cannot_leak(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        titles = await _search(
            retriever,
            embedder,
            await authz_for("staff.finance"),
            question="restructuring of two directorates redeployment of ninety-four posts",
        )
        assert "Executive Council Deliberation Note" not in titles


class TestRowLevelSecurity:
    """TC-SEC-004b — remove the primary control and prove the secondary holds.

    A defence-in-depth claim that is never tested with the first layer disabled
    is an assumption, not a control. This is the single most important test in
    the suite.
    """

    async def test_rls_blocks_when_acl_predicate_is_removed(
        self, engine: AsyncEngine, authz_for: AuthzFactory
    ) -> None:
        authz = await authz_for("staff.misd")
        principals = "{" + ",".join(str(p) for p in authz.principal_array()) + "}"

        async with engine.connect() as conn:
            # Set the session principals RLS reads, then run a query with NO
            # application-level ACL predicate at all.
            await conn.execute(
                text("SELECT set_config('askau.principals', :p, false)"),
                {"p": principals},
            )
            rows = (
                (
                    await conn.execute(
                        text("""
                    SELECT DISTINCT d.title
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE d.classification IN ('confidential','highly_restricted')
                    """)
                    )
                )
                .scalars()
                .all()
            )

        assert rows == [], (
            "RLS did not contain the query when the application predicate was "
            f"removed; leaked: {rows}"
        )

    async def test_rls_permits_what_the_user_may_see(
        self, engine: AsyncEngine, authz_for: AuthzFactory
    ) -> None:
        """The negative control for the test above: RLS must not block everything."""
        authz = await authz_for("staff.finance")
        principals = "{" + ",".join(str(p) for p in authz.principal_array()) + "}"

        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('askau.principals', :p, false)"),
                {"p": principals},
            )
            count = (
                await conn.execute(
                    text("""
                    SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE d.classification = 'confidential'
                    """)
                )
            ).scalar_one()

        assert count > 0, "RLS blocked content the user is authorized for"

    async def test_no_principals_set_means_no_rows(self, engine: AsyncEngine) -> None:
        """An unset session variable must fail closed, not open."""
        async with engine.connect() as conn:
            await conn.execute(text("SELECT set_config('askau.principals', '', false)"))
            count = (await conn.execute(text("SELECT count(*) FROM chunks"))).scalar_one()
        assert count == 0, "chunks were readable with no principals set"


class TestPermissionRevocation:
    """TC-SEC-005 — FR-025. Revocation must take effect within the sync window."""

    async def test_revoking_a_group_removes_access(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
        admin_engine: AsyncEngine,
    ) -> None:
        before = await _search(retriever, embedder, await authz_for("staff.leaver"))
        assert "Budget Reallocation Procedure" in before, "fixture precondition failed"

        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                DELETE FROM user_principals
                WHERE user_id = (SELECT id FROM users WHERE entra_oid = 'oid-staff.leaver')
                  AND principal_id IN (
                      SELECT id FROM principals
                      WHERE external_id IN ('grp-finance','grp-finance-officers')
                  )
                """)
            )
            # Bumping acl_version is what invalidates the cached context.
            await conn.execute(
                text("""UPDATE users SET acl_version = acl_version + 1
                        WHERE entra_oid = 'oid-staff.leaver'""")
            )

        try:
            after = await _search(retriever, embedder, await authz_for("staff.leaver"))
            assert "Budget Reallocation Procedure" not in after, (
                "revoked user still retrieved the document"
            )
        finally:
            # Restore so the suite is order-independent and re-runnable.
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO user_principals (user_id, principal_id)
                    SELECT u.id, p.id FROM users u, principals p
                    WHERE u.entra_oid = 'oid-staff.leaver'
                      AND p.external_id IN ('grp-finance','grp-finance-officers')
                    ON CONFLICT DO NOTHING
                    """)
                )


class TestVersionAndLifecycle:
    """FR-017 / FR-018 — outdated policy is a correctness *and* trust failure."""

    async def test_superseded_version_is_excluded(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        question = "daily subsistence allowance for travel within the continent"
        result = await retriever.search(
            RetrievalQuery(
                text=question,
                embedding=await embedder.embed_query(question),
                authz=await authz_for("staff.misd"),
                candidate_k=200,
                top_k=100,
                ts_config="english",
            )
        )
        travel = [c for c in result.chunks if c.document_title == "Official Travel Policy"]
        assert travel, "expected the current travel policy"
        assert all(c.version_seq == 2 for c in travel), "superseded Rev 1 leaked into results"
        assert all("USD 150" not in c.content for c in travel)

    async def test_expired_document_is_excluded(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        titles = await _search(
            retriever,
            embedder,
            await authz_for("staff.hr"),
            question="remote working protocol during public health emergency",
        )
        assert "Temporary Remote Working Protocol" not in titles

    async def test_historical_is_available_when_explicitly_requested(
        self,
        retriever: PgVectorHybridRetriever,
        embedder: Embedder,
        authz_for: AuthzFactory,
    ) -> None:
        """FR-018 permits historical content on explicit request — and even then
        the ACL still applies."""
        question = "remote working protocol during public health emergency"
        result = await retriever.search(
            RetrievalQuery(
                text=question,
                embedding=await embedder.embed_query(question),
                authz=await authz_for("staff.hr"),
                candidate_k=200,
                top_k=100,
                ts_config="english",
                include_historical=True,
            )
        )
        titles = {c.document_title for c in result.chunks}
        assert "Temporary Remote Working Protocol" in titles
        assert "Executive Council Deliberation Note" not in titles, (
            "include_historical must not widen authorization"
        )
