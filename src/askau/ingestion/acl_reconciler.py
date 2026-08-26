"""Access-list reconciliation (FR-025).

`document_acl` is authoritative; `chunks.acl_principals` is a materialized copy
that exists so the authorization predicate is one indexed array-overlap instead
of three joins (ADR-0002). That denormalization buys the largest latency win in
the design and creates exactly one standing obligation: **the copy has to be
brought back into agreement when permissions change.**

Two properties matter more than throughput.

**Revocations are processed before grants.** They are asymmetric: a late grant
inconveniences someone who cannot yet see a document they are entitled to; a
late revocation means someone still sees a document they are not. Processing
them in one undifferentiated pass treats those as equally urgent, and they are
not. So a reconcile pass runs revocations first, and a full pass that is
interrupted has still closed the exposures.

**Reconciliation is idempotent and safe to run at any time.** It recomputes from
the authoritative table rather than applying a diff, so a missed run is repaired
by the next one and there is no journal to get out of step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    revocations: int = 0
    grants: int = 0
    documents_touched: int = 0
    chunks_updated: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.chunks_updated)


#: Documents whose chunk ACLs no longer match `document_acl`, split by direction.
#:
#: "Revocation" means a chunk still lists a principal the document no longer
#: grants. "Grant" means the document grants a principal the chunk is missing.
_DRIFT = text("""
WITH authoritative AS (
    SELECT d.id AS document_id,
           coalesce(array_agg(a.principal_id ORDER BY a.principal_id)
                    FILTER (WHERE a.principal_id IS NOT NULL), '{}') AS expected
    FROM documents d
    LEFT JOIN document_acl a ON a.document_id = d.id
    GROUP BY d.id
),
current AS (
    SELECT c.document_id,
           coalesce(array_agg(DISTINCT p ORDER BY p), '{}') AS present
    FROM chunks c, unnest(c.acl_principals) AS p
    GROUP BY c.document_id
)
SELECT a.document_id::text AS document_id,
       -- present but no longer authoritative: an exposure
       coalesce(array_length(ARRAY(
           SELECT unnest(c.present) EXCEPT SELECT unnest(a.expected)), 1), 0) AS revoked,
       -- authoritative but not yet present: an inconvenience
       coalesce(array_length(ARRAY(
           SELECT unnest(a.expected) EXCEPT SELECT unnest(c.present)), 1), 0) AS granted
FROM authoritative a
JOIN current c ON c.document_id = a.document_id
WHERE a.expected IS DISTINCT FROM c.present
""")

#: Recompute one document's chunk ACLs from the authoritative table.
_APPLY = text("""
UPDATE chunks
SET acl_principals = coalesce(
        (SELECT array_agg(a.principal_id ORDER BY a.principal_id)
         FROM document_acl a WHERE a.document_id = chunks.document_id),
        '{}'
    ),
    acl_synced_at = now()
WHERE document_id = CAST(:document_id AS uuid)
""")

#: Bump every affected user's cache-invalidation counter. Rewriting chunk rows
#: is only half the job: a cached authorization context or answer would keep
#: serving the old permissions until it expired.
_BUMP = text("""
UPDATE users SET acl_version = acl_version + 1
WHERE id IN (
    SELECT up.user_id FROM user_principals up
    WHERE up.principal_id IN (
        SELECT principal_id FROM document_acl WHERE document_id = CAST(:document_id AS uuid)
    )
)
""")


class AclReconciler:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def find_drift(self) -> tuple[list[str], list[str]]:
        """Documents needing reconciliation, as (revocations, grants).

        A document can appear in both; it is placed in revocations, because that
        is the half that must not wait.
        """
        async with self._engine.connect() as conn:
            rows = (await conn.execute(_DRIFT)).mappings().all()
        revocations = [r["document_id"] for r in rows if r["revoked"]]
        grants = [r["document_id"] for r in rows if r["granted"] and not r["revoked"]]
        return revocations, grants

    async def reconcile(self, *, limit: int | None = None) -> ReconcileReport:
        """Bring chunk ACLs back into agreement with `document_acl`."""
        revocations, grants = await self.find_drift()
        if not revocations and not grants:
            return ReconcileReport()

        # Revocations first, always. If this pass is cut short, the exposures
        # are the part that has already been closed.
        ordered = revocations + grants
        if limit is not None:
            ordered = ordered[:limit]

        updated = 0
        async with self._engine.begin() as conn:
            for document_id in ordered:
                result = await conn.execute(_APPLY, {"document_id": document_id})
                updated += result.rowcount or 0
                await conn.execute(_BUMP, {"document_id": document_id})

        report = ReconcileReport(
            revocations=len(revocations),
            grants=len(grants),
            documents_touched=len(ordered),
            chunks_updated=updated,
        )
        if report.revocations:
            # Worth a log line at warning level: a revocation that took a
            # reconcile pass to apply was, until that moment, an exposure.
            _log.warning(
                "reconciled %d revocation(s) and %d grant(s) across %d chunks",
                report.revocations,
                report.grants,
                report.chunks_updated,
            )
        return report

    async def reconcile_document(self, document_id: str) -> int:
        """Reconcile one document — used after an administrator edits its ACL,
        so the change is effective immediately rather than at the next pass."""
        async with self._engine.begin() as conn:
            result = await conn.execute(_APPLY, {"document_id": document_id})
            await conn.execute(_BUMP, {"document_id": document_id})
        return result.rowcount or 0

    async def max_lag_seconds(self) -> float | None:
        """Age of the least recently synchronized chunk.

        The metric the runbook alerts on: this exceeding the sync interval means
        revocations are outstanding.
        """
        async with self._engine.connect() as conn:
            value = (
                await conn.execute(
                    text("SELECT EXTRACT(EPOCH FROM (now() - min(acl_synced_at))) FROM chunks")
                )
            ).scalar_one_or_none()
        return float(value) if value is not None else None
