"""Per-user opt-out of conversation persistence.

The web client has offered a "Save conversation history" toggle since its first
commit, wired to nothing. A switch that says questions are not being kept, while
`messages` records every one, is worse than no switch: it is a privacy promise
the product does not keep.

What the flag does and does not cover is the whole design, and the line falls in
an unobvious place:

* It stops the *conversation* being kept — no `conversations` row, no
  `messages`, no `citations`. Nothing to re-read later, and nothing in the
  history list.
* It does **not** stop the audit record. BR-008 makes `audit_events`
  append-only and non-optional: that somebody asked something, and which
  documents their question reached, is a security record and not a convenience
  feature anyone may decline. What that record never contains is the question or
  the answer text — verified, and asserted by a test — so honouring the flag
  costs the audit log nothing.

Default TRUE: history is useful, and FR-006 requires it to be available. This is
an opt-out, not an opt-in.

Revision ID: 0013_save_history
Revises: 0012_chunk_embedding_model
"""

from __future__ import annotations

from alembic import op

revision = "0013_save_history"
down_revision = "0012_chunk_embedding_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS save_history BOOLEAN NOT NULL DEFAULT TRUE"
    )
    # `share_analytics` alongside it, because the two are the same kind of thing
    # and adding one column twice is two migrations for no reason. Its meaning is
    # narrower than the client's copy suggests — see `docs/architecture` — it
    # governs whether this person's questions may be sampled into the evaluation
    # corpus, and nothing else. It cannot govern audit or usage reporting, both
    # of which are required.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS share_analytics BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS share_analytics")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS save_history")
