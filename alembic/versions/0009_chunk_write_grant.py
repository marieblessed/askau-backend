"""Grant DML on the chunks parent table.

Revision ID: 0009_chunk_write_grant
Revises: 0008_phase3_seams

`GRANT ... ON ALL TABLES IN SCHEMA public` in 0004 left the partitioned *parent*
with SELECT only, while every partition received full DML. Writing through a
partitioned table requires the privilege on the parent, so the ingestion
pipeline could read chunks and not write them.

It went unnoticed because the seed writes with the migration role, which is a
superuser. The first code to insert chunks as `askau_app` was the real ingestion
pipeline — which is the argument for seeding through the same privileges the
application actually runs with, rather than through elevated ones.
"""

from __future__ import annotations

from alembic import op

revision = "0009_chunk_write_grant"
down_revision = "0008_phase3_seams"
branch_labels = None
depends_on = None

_PARTITIONED = ("chunks", "messages", "audit_events", "model_invocations")


def upgrade() -> None:
    for table in _PARTITIONED:
        if table == "audit_events":
            # BR-008: append and read only, never rewrite history.
            op.execute(f"GRANT SELECT, INSERT ON {table} TO askau_app")
        else:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO askau_app")


def downgrade() -> None:
    for table in _PARTITIONED:
        op.execute(f"REVOKE INSERT, UPDATE, DELETE ON {table} FROM askau_app")
