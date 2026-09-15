"""Extensions and enum types.

Revision ID: 0001_extensions
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001_extensions"
down_revision = None
branch_labels = None
depends_on = None

ENUMS: dict[str, tuple[str, ...]] = {
    "classification": ("public", "internal", "confidential", "highly_restricted"),
    "lifecycle_status": ("draft", "active", "review_required", "expired", "superseded"),
    "ingest_status": (
        "pending",
        "fetching",
        "extracting",
        "chunking",
        "embedding",
        "indexed",
        "failed",
        "quarantined",
        "skipped_unchanged",
    ),
    "principal_kind": ("user", "group", "role", "department"),
    "source_type": ("sharepoint", "dms", "filesystem", "s3", "http", "manual"),
    "source_status": ("draft", "active", "paused", "error", "archived"),
    "message_role": ("user", "assistant"),
    "answer_state": (
        "grounded",
        "partially_grounded",
        "conflict",
        "insufficient_evidence",
        "clarification_needed",
        "out_of_scope",
        "refused_safety",
        "error",
    ),
    "feedback_rating": ("helpful", "not_helpful"),
    "audit_outcome": ("success", "failure", "denied"),
}


def upgrade() -> None:
    # pgvector is a hard dependency, not an optional accelerator: without it the
    # chunks table cannot be created at all. If this fails, the database was
    # provisioned from an image without the extension (see ADR-0014).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    for name, values in ENUMS.items():
        rendered = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")


def downgrade() -> None:
    for name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
    # Extensions are left in place: other databases on a shared server may use
    # them, and dropping pg_trgm or pgcrypto could break unrelated schemas.
