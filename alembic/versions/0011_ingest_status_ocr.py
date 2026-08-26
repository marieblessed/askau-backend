"""Add an `ocr` stage to ingest_status.

OCR is a distinct, slow stage — a scanned page is seconds where extraction is
milliseconds. Folding it into `extracting` would make a task that is working
indistinguishable from one that is stuck, which is precisely the question an
administrator is trying to answer when they open the run.

Additive by design: `ALTER TYPE ... ADD VALUE` neither rewrites the table nor
invalidates any existing row, so this is safe to apply ahead of the code that
writes the value (expand-contract).

The downgrade recreates the type, because PostgreSQL cannot remove an enum
label. Any row still sitting in the `ocr` stage is moved to `extracting` first
— a task mid-OCR is, accurately, mid-extraction — because the cast would
otherwise fail and leave the migration half-applied.

Revision ID: 0011_ingest_status_ocr
Revises: 0010_chunk_write_policy
"""

from __future__ import annotations

from alembic import op

revision = "0011_ingest_status_ocr"
down_revision = "0010_chunk_write_policy"
branch_labels = None
depends_on = None

#: The value set as it stands *before* this revision, used to rebuild the type
#: on downgrade. Written out rather than derived: 0001 is free to change its own
#: list in a future revision, and a downgrade that silently followed it would
#: reintroduce whatever that change was.
_WITHOUT_OCR = (
    "pending",
    "fetching",
    "extracting",
    "chunking",
    "embedding",
    "indexed",
    "failed",
    "quarantined",
    "skipped_unchanged",
)


def upgrade() -> None:
    # IF NOT EXISTS so re-running against a database that already has the label
    # (a rebuilt dev environment, say) is not an error.
    op.execute("ALTER TYPE ingest_status ADD VALUE IF NOT EXISTS 'ocr' AFTER 'extracting'")


def downgrade() -> None:
    labels = ", ".join(f"'{value}'" for value in _WITHOUT_OCR)

    # Nothing may reference the label by the time the old type replaces it.
    op.execute("UPDATE ingestion_tasks SET stage = 'extracting' WHERE stage = 'ocr'")
    op.execute("UPDATE documents SET ingest_status = 'extracting' WHERE ingest_status = 'ocr'")

    op.execute(f"CREATE TYPE ingest_status__old AS ENUM ({labels})")

    # The defaults are typed to the enum too, and PostgreSQL will not cast them
    # implicitly — the column change fails with "default for column cannot be
    # cast automatically" unless they are dropped first and restored after.
    op.execute("ALTER TABLE ingestion_tasks ALTER COLUMN stage DROP DEFAULT")
    op.execute("ALTER TABLE documents ALTER COLUMN ingest_status DROP DEFAULT")

    # A *partial* index carries the old type inside its predicate, so the column
    # change fails with "operator does not exist: ingest_status__old =
    # ingest_status". Plain indexes on the same columns rebuild themselves and
    # need no help; only the one with a WHERE clause does.
    op.execute("DROP INDEX IF EXISTS ix_tasks_failed")

    op.execute(
        "ALTER TABLE ingestion_tasks ALTER COLUMN stage TYPE ingest_status__old "
        "USING stage::text::ingest_status__old"
    )
    op.execute(
        "ALTER TABLE documents ALTER COLUMN ingest_status TYPE ingest_status__old "
        "USING ingest_status::text::ingest_status__old"
    )

    op.execute("DROP TYPE ingest_status")
    op.execute("ALTER TYPE ingest_status__old RENAME TO ingest_status")

    op.execute("ALTER TABLE ingestion_tasks ALTER COLUMN stage SET DEFAULT 'pending'")
    op.execute("ALTER TABLE documents ALTER COLUMN ingest_status SET DEFAULT 'pending'")

    op.execute("CREATE INDEX ix_tasks_failed ON ingestion_tasks (stage) WHERE stage = 'failed'")
