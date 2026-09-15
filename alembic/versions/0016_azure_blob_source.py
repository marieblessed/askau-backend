"""Add `azure_blob` to the source_type enum.

Revision ID: 0016_azure_blob_source
Revises: 0015_acl_drift_metric

The AUC settled on Azure Blob Storage as the document store, with Entra ID for
identity. SharePoint is no longer the target — the connector stays because its
per-item permission mapping is the reference for anything similar, but new work
goes here.

`s3` was tempting to reuse and would have been a lie: an operator reading
`source_type = 's3'` on an Azure container learns something false, and the
enum exists so that the corpus can be reasoned about without opening `location`.

`ALTER TYPE ... ADD VALUE` runs inside a transaction on PostgreSQL 12+ provided
the new value is not *used* in the same transaction. Nothing here uses it.
"""

from __future__ import annotations

from alembic import op

revision = "0016_azure_blob_source"
down_revision = "0015_acl_drift_metric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'azure_blob'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum. Recreating the type would
    # mean rewriting every column that uses it while the application is running
    # against it, to undo something entirely additive. Left in place
    # deliberately, and said so rather than pretending the downgrade is complete.
    pass
