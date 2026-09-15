"""Chunks: classification-partitioned, ACL-denormalized, HNSW-indexed, RLS-protected.

This is the table the entire design rests on. Four things about it are deliberate
and none are conventional:

1. ``acl_principals BIGINT[]`` is denormalized from ``document_acl`` so the
   authorization predicate is one GIN overlap test instead of three joins
   (ADR-0002). The array is derived; ``document_acl`` stays authoritative.
2. LIST partitioning on ``classification`` means most callers never scan the
   restricted partitions at all (ADR-0003).
3. ``tsv`` is a plain column, not generated: the text-search configuration
   depends on each document's language, and a generated column cannot select one
   per row (ADR-0015). Getting this wrong fails *silently*.
4. Row-level security is a second, independent lock. The application always
   passes the ACL predicate explicitly; RLS exists because the cost of one
   forgotten predicate is a disclosure incident, and "unauthorized retrieval = 0"
   is an acceptance criterion.

Revision ID: 0004_chunks
Revises: 0003_knowledge
"""

from __future__ import annotations

from alembic import op

revision = "0004_chunks"
down_revision = "0003_knowledge"
branch_labels = None
depends_on = None

PARTITIONS: dict[str, str] = {
    "chunks_public": "public",
    "chunks_internal": "internal",
    "chunks_confidential": "confidential",
    "chunks_restricted": "highly_restricted",
}

EMBEDDING_DIM = 1024  # ADR-0006 — 1024 over 1536: ~33% less HNSW memory


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE chunks (
            id             BIGINT GENERATED ALWAYS AS IDENTITY,
            document_id    UUID   NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            family_id      UUID   NOT NULL,
            ordinal        INTEGER NOT NULL,

            content        TEXT   NOT NULL,
            token_count    SMALLINT NOT NULL,

            -- citation anchors (FR-015, FR-029): what makes "§4.3, p.12" possible
            heading_path   TEXT[]  NOT NULL DEFAULT '{{}}',
            section_ref    TEXT,
            page_from      SMALLINT,
            page_to        SMALLINT,
            char_start     INTEGER,
            char_end       INTEGER,

            -- ── denormalized from documents: removes every join from the hot path
            classification classification NOT NULL,
            lifecycle      lifecycle_status NOT NULL,
            department     TEXT,
            language       TEXT NOT NULL DEFAULT 'en',
            effective_from DATE,
            effective_to   DATE,
            version_seq    INTEGER NOT NULL,
            is_current     BOOLEAN NOT NULL DEFAULT TRUE,
            acl_principals BIGINT[] NOT NULL,

            embedding      vector({EMBEDDING_DIM}),
            -- Written by the ingestion pipeline using the document's own
            -- text-search configuration. NOT generated — see module docstring.
            lang_config    REGCONFIG NOT NULL DEFAULT 'simple',
            tsv            TSVECTOR NOT NULL,

            acl_synced_at  timestamptz NOT NULL DEFAULT now(),
            created_at     timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (id, classification),
            UNIQUE (document_id, ordinal, classification)
        ) PARTITION BY LIST (classification)
    """)

    for table, value in PARTITIONS.items():
        op.execute(f"CREATE TABLE {table} PARTITION OF chunks FOR VALUES IN ('{value}')")

    for table in PARTITIONS:
        # Cosine distance: embeddings are normalized, and cosine is what the
        # retriever's `<=>` operator uses.
        op.execute(f"""
            CREATE INDEX ix_{table}_embedding ON {table}
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 128)
        """)
        # The authorization predicate. This index is the reason ADR-0002 pays off.
        op.execute(f"CREATE INDEX ix_{table}_acl ON {table} USING gin (acl_principals)")
        op.execute(f"CREATE INDEX ix_{table}_tsv ON {table} USING gin (tsv)")
        op.execute(f"CREATE INDEX ix_{table}_doc ON {table} (document_id, ordinal)")
        # Partial index for the dominant case: current, active content.
        op.execute(f"""
            CREATE INDEX ix_{table}_acl_live ON {table} USING gin (acl_principals)
            WHERE is_current AND lifecycle = 'active'
        """)
        # Trigram fallback carries fuzzy matching for languages with no stemmer
        # (Kiswahili, Amharic), where lang_config resolves to 'simple'.
        op.execute(
            f"CREATE INDEX ix_{table}_content_trgm ON {table} USING gin (content gin_trgm_ops)"
        )

    _create_app_role_and_rls()


def _create_app_role_and_rls() -> None:
    """Least-privilege application role plus the independent RLS lock.

    ``askau_app`` gets DML on data tables but no DDL, and deliberately no
    UPDATE/DELETE on ``audit_events`` (granted in 0006) so BR-008 holds even
    against the application itself.
    """
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'askau_app') THEN
                CREATE ROLE askau_app LOGIN PASSWORD 'askau';
            END IF;
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO askau_app")
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON ALL TABLES IN SCHEMA public TO askau_app
    """)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO askau_app")

    # chunks is the exception: the API only ever reads it. Ingestion writes under
    # the elevated role instead. Granting the API write access would mean an
    # `UPDATE ... RETURNING content` could read rows the SELECT policy forbids —
    # and would force a permissive write policy that, because PostgreSQL OR-combines
    # permissive policies, would silently neutralize the read policy entirely.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON chunks FROM askau_app")

    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    # FORCE so the policy applies to the table owner too.
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY chunk_acl_read ON chunks
        FOR SELECT TO askau_app
        USING (
            acl_principals && COALESCE(
                NULLIF(current_setting('askau.principals', true), '')::bigint[],
                '{}'::bigint[]
            )
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS chunk_acl_read ON chunks")
    op.execute("DROP TABLE IF EXISTS chunks CASCADE")  # cascades to partitions
