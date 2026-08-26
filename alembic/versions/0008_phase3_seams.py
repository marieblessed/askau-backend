"""Phase 3 seams — tables only, no writers in Phase 1.

Created now so the agent layer is additive (SRS §7.1) rather than a migration of
live tables under load.

Revision ID: 0008_phase3_seams
Revises: 0007_evaluation
"""

from __future__ import annotations

from alembic import op

revision = "0008_phase3_seams"
down_revision = "0007_evaluation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tool_registry (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL UNIQUE,
            system TEXT NOT NULL,
            openapi_ref TEXT,
            requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
            required_scopes TEXT[] NOT NULL DEFAULT '{}',
            is_enabled BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    op.execute("""
        CREATE TABLE approval_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requested_by UUID NOT NULL REFERENCES users(id),
            tool_id UUID NOT NULL REFERENCES tool_registry(id),
            payload JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                     ('pending','approved','rejected','expired','executed')),
            approver_id UUID REFERENCES users(id),
            decided_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    for table in ("tool_registry", "approval_requests"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO askau_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS approval_requests CASCADE")
    op.execute("DROP TABLE IF EXISTS tool_registry CASCADE")
