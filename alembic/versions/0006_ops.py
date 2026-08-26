"""Operations: audit, ingestion runs and tasks, model invocations.

Revision ID: 0006_ops
Revises: 0005_conversations
"""

from __future__ import annotations

from datetime import UTC, datetime

from alembic import op

revision = "0006_ops"
down_revision = "0005_conversations"
branch_labels = None
depends_on = None


def _month_partitions(table: str, months_ahead: int = 3) -> None:
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
    # FR-051/052. Append-only by grant, not by convention: BR-008 requires the
    # log be trustworthy to a reviewer who does not trust the application.
    op.execute("""
        CREATE TABLE audit_events (
            id             BIGINT GENERATED ALWAYS AS IDENTITY,
            occurred_at    timestamptz NOT NULL DEFAULT now(),
            correlation_id UUID NOT NULL,
            event_type     TEXT NOT NULL,
            event_category TEXT NOT NULL CHECK (event_category IN
                             ('authentication','authorization','query','retrieval',
                              'document_access','administration','configuration',
                              'security','ai_safety','ingestion')),
            outcome        audit_outcome NOT NULL,
            -- Deliberately NO foreign keys: deleting a knowledge source must not
            -- delete the record of what it once answered (SRS §6.9, records mgmt).
            actor_user_id  UUID,
            actor_email    TEXT,
            resource_type  TEXT,
            resource_id    TEXT,
            ip_hash        BYTEA,
            user_agent     TEXT,
            -- FR-052: metadata only. Never question or answer text.
            detail         JSONB NOT NULL DEFAULT '{}',
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
    """)
    _month_partitions("audit_events")
    op.execute("CREATE INDEX ix_audit_actor ON audit_events (actor_user_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_audit_category ON audit_events (event_category, occurred_at DESC)")
    op.execute("CREATE INDEX ix_audit_correlation ON audit_events (correlation_id)")
    op.execute("""
        CREATE INDEX ix_audit_security ON audit_events (event_type, occurred_at DESC)
        WHERE event_category IN ('security','ai_safety')
    """)
    # BR-008 as a positive grant, not a revoke. A REVOKE of privileges that were
    # never granted silently succeeds and proves nothing; granting exactly
    # SELECT+INSERT means the application *cannot* rewrite history even if a
    # future migration adds a blanket GRANT ON ALL TABLES.
    op.execute("GRANT SELECT, INSERT ON audit_events TO askau_app")

    op.execute("""
        CREATE TABLE ingestion_runs (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id     UUID NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
            trigger       TEXT NOT NULL CHECK (trigger IN
                            ('schedule','manual','webhook','reindex')),
            triggered_by  UUID REFERENCES users(id),
            status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN
                            ('running','completed','failed','cancelled','partial')),
            docs_discovered  INTEGER NOT NULL DEFAULT 0,
            docs_processed   INTEGER NOT NULL DEFAULT 0,
            docs_skipped     INTEGER NOT NULL DEFAULT 0,
            docs_failed      INTEGER NOT NULL DEFAULT 0,
            chunks_written   INTEGER NOT NULL DEFAULT 0,
            embedding_tokens BIGINT  NOT NULL DEFAULT 0,  -- rollup, written at completion
            error         JSONB,
            started_at    timestamptz NOT NULL DEFAULT now(),
            finished_at   timestamptz
        )
    """)
    op.execute("CREATE INDEX ix_runs_source ON ingestion_runs (source_id, started_at DESC)")

    # Per-document checkpoint: this is what makes a failed run resumable rather
    # than restartable (FR-049, FR-050).
    op.execute("""
        CREATE TABLE ingestion_tasks (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id       UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
            document_id  UUID REFERENCES documents(id) ON DELETE SET NULL,
            external_key TEXT NOT NULL,
            stage        ingest_status NOT NULL DEFAULT 'pending',
            attempts     SMALLINT NOT NULL DEFAULT 0,
            error_code   TEXT,
            error_detail JSONB,
            stage_timings JSONB NOT NULL DEFAULT '{}',
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_tasks_run ON ingestion_tasks (run_id, stage)")
    op.execute("CREATE INDEX ix_tasks_failed ON ingestion_tasks (stage) WHERE stage = 'failed'")

    # FR-053. One row per MODEL CALL, not per answer: a single grounded answer
    # invokes a model up to four times, and knowing which stage consumed what is
    # the difference between "answers are slow" and "reranking is slow".
    op.execute("""
        CREATE TABLE model_invocations (
            id            BIGINT GENERATED ALWAYS AS IDENTITY,
            occurred_at   timestamptz NOT NULL DEFAULT now(),
            operation     TEXT NOT NULL CHECK (operation IN
                            ('understand','embed_query','embed_document','rerank','chat')),
            provider      TEXT NOT NULL,
            model         TEXT NOT NULL,
            message_id       UUID,
            ingestion_run_id UUID REFERENCES ingestion_runs(id) ON DELETE SET NULL,
            user_id          UUID REFERENCES users(id) ON DELETE SET NULL,
            department       TEXT,   -- denormalized for §7.4 per-directorate rollup
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms    INTEGER,
            -- A cache hit is a real operation with real latency and zero tokens.
            -- Without this flag consumption looks like it is falling when demand is flat.
            cached        BOOLEAN NOT NULL DEFAULT FALSE,
            outcome       TEXT NOT NULL DEFAULT 'success'
                          CHECK (outcome IN ('success','error','timeout')),
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
    """)
    _month_partitions("model_invocations")
    op.execute("CREATE INDEX ix_usage_op ON model_invocations (occurred_at DESC, operation, model)")
    op.execute("CREATE INDEX ix_usage_dept ON model_invocations (department, occurred_at DESC)")
    op.execute("CREATE INDEX ix_usage_message ON model_invocations (message_id)")
    op.execute("""
        CREATE INDEX ix_usage_run ON model_invocations (ingestion_run_id)
        WHERE ingestion_run_id IS NOT NULL
    """)

    for table in ("ingestion_runs", "ingestion_tasks", "model_invocations"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO askau_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO askau_app")


def downgrade() -> None:
    for t in ("model_invocations", "ingestion_tasks", "ingestion_runs", "audit_events"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
