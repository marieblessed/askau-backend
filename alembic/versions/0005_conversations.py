"""Conversations, messages, citations, feedback.

Revision ID: 0005_conversations
Revises: 0004_chunks
"""

from __future__ import annotations

from datetime import UTC, datetime

from alembic import op

revision = "0005_conversations"
down_revision = "0004_chunks"
branch_labels = None
depends_on = None


def _month_partitions(table: str, months_ahead: int = 3) -> None:
    """Create partitions for the current month and the next few.

    Ongoing creation is an operational job (ops/sql/partition_maintenance.sql).
    A missing partition surfaces as a write error, and the one time it bites is
    a failover during month rollover — hence the lookahead.
    """
    now = datetime.now(UTC)
    year, month = now.year, now.month
    for _ in range(months_ahead + 1):
        start = f"{year:04d}-{month:02d}-01"
        ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
        end = f"{ny:04d}-{nm:02d}-01"
        op.execute(
            f"CREATE TABLE IF NOT EXISTS {table}_{year:04d}{month:02d} "
            f"PARTITION OF {table} FOR VALUES FROM ('{start}') TO ('{end}')"
        )
        year, month = ny, nm


def upgrade() -> None:
    op.execute("""
        CREATE TABLE conversations (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title           TEXT,
            message_count   INTEGER NOT NULL DEFAULT 0,
            last_message_at timestamptz,
            is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      timestamptz NOT NULL DEFAULT now(),
            purge_after     timestamptz          -- configurable retention (§6.5)
        )
    """)
    op.execute("""
        CREATE INDEX ix_conversations_recent ON conversations (user_id, last_message_at DESC)
        WHERE NOT is_archived
    """)
    op.execute("""
        CREATE INDEX ix_conversations_purge ON conversations (purge_after)
        WHERE purge_after IS NOT NULL
    """)

    op.execute("""
        CREATE TABLE messages (
            id              UUID NOT NULL DEFAULT gen_random_uuid(),
            conversation_id UUID NOT NULL,
            user_id         UUID NOT NULL,      -- denormalized: enables per-user purge
            role            message_role NOT NULL,
            seq             INTEGER NOT NULL,
            content         TEXT NOT NULL,

            answer_state    answer_state,
            model_provider  TEXT,
            model_name      TEXT,
            retrieval_strategy TEXT,
            retrieved_count SMALLINT,
            reranked_count  SMALLINT,
            groundedness    NUMERIC(4,3),
            ttft_ms         INTEGER,            -- NFR-002a measurement
            total_ms        INTEGER,            -- NFR-002b measurement
            cache_hit       BOOLEAN NOT NULL DEFAULT FALSE,
            correlation_id  UUID NOT NULL,
            -- Token counts live in model_invocations (0006): one answer invokes a
            -- model up to four times, so a per-answer total is a rollup, not a fact.

            created_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at),
            UNIQUE (conversation_id, seq, created_at)
        ) PARTITION BY RANGE (created_at)
    """)
    _month_partitions("messages")
    op.execute("CREATE INDEX ix_messages_conv ON messages (conversation_id, seq)")
    op.execute("CREATE INDEX ix_messages_user ON messages (user_id, created_at DESC)")

    # FR-030: a citation that does not resolve to a real chunk cannot be inserted.
    # "No fabricated citations" is referential integrity, not a prompt instruction.
    op.execute("""
        CREATE TABLE citations (
            id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            message_id     UUID NOT NULL,
            message_created_at timestamptz NOT NULL,
            chunk_id       BIGINT NOT NULL,
            chunk_classification classification NOT NULL,
            document_id    UUID NOT NULL REFERENCES documents(id),
            marker         SMALLINT NOT NULL,
            rank           SMALLINT NOT NULL,
            retrieval_score NUMERIC(8,6),
            rerank_score   NUMERIC(8,6),
            quote          TEXT,
            page_from      SMALLINT,
            page_to        SMALLINT,
            section_ref    TEXT,
            verified       BOOLEAN NOT NULL DEFAULT FALSE,
            FOREIGN KEY (message_id, message_created_at)
                REFERENCES messages(id, created_at) ON DELETE CASCADE,
            FOREIGN KEY (chunk_id, chunk_classification)
                REFERENCES chunks(id, classification)
        )
    """)
    op.execute("CREATE INDEX ix_citations_message ON citations (message_id)")
    op.execute("CREATE INDEX ix_citations_document ON citations (document_id)")

    op.execute("""
        CREATE TABLE message_feedback (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            message_id  UUID NOT NULL,
            message_created_at timestamptz NOT NULL,
            user_id     UUID NOT NULL REFERENCES users(id),
            rating      feedback_rating NOT NULL,
            reason_code TEXT CHECK (reason_code IN
                          ('incorrect_answer','wrong_source','outdated_information',
                           'missing_information','not_relevant','unclear','other')),
            comment     TEXT,
            triaged_at  timestamptz,
            triaged_by  UUID REFERENCES users(id),
            resolution  TEXT,
            created_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (message_id, user_id),
            FOREIGN KEY (message_id, message_created_at)
                REFERENCES messages(id, created_at) ON DELETE CASCADE
        )
    """)

    # Grants are per-migration, not a one-off GRANT ON ALL TABLES: that form only
    # covers tables that already exist, so anything created in a later revision
    # silently ends up unreadable by the application role.
    for table in ("conversations", "messages", "citations", "message_feedback"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO askau_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO askau_app")


def downgrade() -> None:
    for t in ("message_feedback", "citations", "messages", "conversations"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
