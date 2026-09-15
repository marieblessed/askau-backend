"""Expose ACL synchronisation drift to the metrics endpoint.

`document_acl` is authoritative; `chunks.acl_principals` is a denormalised copy
the authorization predicate reads (ADR-0002). The gap between them is the window
in which a revoked person can still retrieve a document, and unlike a denial it
produces no event — a denial is somebody correctly refused, drift is somebody
possibly *not* refused, which is silent by construction.

`AclReconciler.max_lag_seconds` described itself as "the metric the runbook
alerts on" while being exposed nowhere, so the alert it named could not exist.
Wiring it into `/metrics` then failed for a reason worth recording, because the
naive version *looked* like it worked:

**RLS on `chunks` hides every row from the metrics query.** The endpoint runs as
`askau_app`, whose only SELECT policy on `chunks` is the ACL overlap, and no
principals are set for a scrape. So `count(*)` returns 0 and `min(acl_synced_at)`
returns NULL — and both gauges read zero for ever. Not zero because the corpus is
consistent: zero because the query can see nothing. A gauge that structurally
cannot leave zero is worse than no gauge, because somebody will trust it.

**Why a function and not a grant.** Giving `askau_app` an unfiltered SELECT on
`chunks` would delete the second lock this system's security argument rests on —
RLS exists precisely so that a bug in the application's own predicate is not
sufficient to disclose content. This function returns two integers and no rows,
which is the same contract `/metrics` already keeps: counts and latencies, never
content.

`SET search_path` is not decoration. A SECURITY DEFINER function without it can
be made to resolve `chunks` to an attacker-controlled table in a schema they can
create, and it would run as the owner.

Revision ID: 0015_acl_drift_metric
Revises: 0014_higher_intelligence
"""

from __future__ import annotations

from alembic import op

revision = "0015_acl_drift_metric"
down_revision = "0014_higher_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION askau_acl_drift()
    RETURNS TABLE (max_lag_seconds double precision, documents_drifted bigint)
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        SELECT
          coalesce(EXTRACT(EPOCH FROM (now() - min(c.acl_synced_at)))::double precision, 0),
          (SELECT count(DISTINCT c2.document_id) FROM chunks c2
           WHERE c2.acl_principals IS DISTINCT FROM coalesce(
               (SELECT array_agg(a.principal_id ORDER BY a.principal_id)
                FROM document_acl a WHERE a.document_id = c2.document_id),
               '{}'::bigint[]
           ))
        FROM chunks c
    $$
    """)
    # Execute only, and only to the application role. The function is the whole
    # of the privilege — there is no path from it to a chunk's content.
    op.execute("REVOKE ALL ON FUNCTION askau_acl_drift() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION askau_acl_drift() TO askau_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS askau_acl_drift()")
