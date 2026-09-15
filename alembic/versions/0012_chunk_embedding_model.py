"""Record which model produced each embedding.

`chunks.embedding` held a 1024-dimensional vector with nothing saying where it
came from. That is not a cosmetic omission — a vector is only meaningful
relative to the model that produced it, and without the model recorded:

* A query embedded by a different model is silently compared against it. The
  arithmetic succeeds and returns nonsense, so retrieval degrades with no error
  anywhere. This cost two debugging sessions before the cause was obvious.
* Re-embedding cannot be incremental. There is no way to ask which rows are
  stale, so switching models means rebuilding the whole corpus.
* An operator cannot answer "what is this index built from" without inferring it
  from configuration that may since have changed.

Nullable, because the rows that exist were written before this column did and
their provenance genuinely is unknown. Claiming otherwise by back-filling a
guess would defeat the purpose — an unknown provenance is a fact worth keeping.

Revision ID: 0012_chunk_embedding_model
Revises: 0011_ingest_status_ocr
"""

from __future__ import annotations

from alembic import op

revision = "0012_chunk_embedding_model"
down_revision = "0011_ingest_status_ocr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # On the partitioned parent, which propagates to every partition. Adding a
    # nullable column with no default is a catalogue-only change: no table
    # rewrite, no long lock, safe on a populated corpus.
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT")

    # Partial index, because the question this column exists to answer is
    # "which rows are stale" — always a filter on a specific model, never a scan
    # of every row.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_model "
        "ON chunks (embedding_model) WHERE embedding_model IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_model")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding_model")
