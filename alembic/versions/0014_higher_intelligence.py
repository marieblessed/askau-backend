"""Per-user consent to escalate retrieval.

The client's third unwired settings toggle. Its copy is worth reading exactly:
*"AskAU can automatically use more thorough retrieval when answering complex
questions."* That is not a quality tier the reader picks — it is permission for
the system to spend more when it judges the first attempt weak.

Default FALSE, unlike the other two preferences. Those default TRUE because the
useful behaviour is the expected one and the toggle is an opt-out. This one
authorises additional work per question against a shared database, so it is an
opt-in: nobody's questions get more expensive because a column appeared.

Revision ID: 0014_higher_intelligence
Revises: 0013_save_history
"""

from __future__ import annotations

from alembic import op

revision = "0014_higher_intelligence"
down_revision = "0013_save_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "higher_intelligence BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS higher_intelligence")
