"""Restore the chunk write policy.

Revision ID: 0010_chunk_write_policy
Revises: 0009_chunk_write_grant

`chunks` has FORCE ROW LEVEL SECURITY, so with only a `FOR SELECT` policy in
place every INSERT was rejected. Ingestion writes chunks outside any single
user's authorization context — the pipeline is not acting on behalf of a
reader — so writes need their own policy.

Reads stay governed by `chunk_acl_read`, which is the one that matters: it is
the second, independent lock behind the ACL predicate in the retrieval query.
A permissive write policy does not weaken it, because the disclosure risk is on
the read path.

Added as a new revision rather than by editing 0004. 0004 has been applied, and
amending an applied migration means two databases with the same version number
have different schemas — the failure mode expand-contract discipline exists to
prevent.
"""

from __future__ import annotations

from alembic import op

revision = "0010_chunk_write_policy"
down_revision = "0009_chunk_write_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS chunk_write ON chunks")
    op.execute("""
        CREATE POLICY chunk_write ON chunks
        FOR INSERT TO askau_app
        WITH CHECK (true)
    """)
    # UPDATE and DELETE are needed by the ACL reconciler and by reindexing.
    op.execute("""
        CREATE POLICY chunk_maintain ON chunks
        FOR UPDATE TO askau_app
        USING (true) WITH CHECK (true)
    """)
    op.execute("""
        CREATE POLICY chunk_remove ON chunks
        FOR DELETE TO askau_app
        USING (true)
    """)


def downgrade() -> None:
    for policy in ("chunk_remove", "chunk_maintain", "chunk_write"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON chunks")
