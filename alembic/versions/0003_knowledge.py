"""Knowledge sources, document families, documents, and the authoritative ACL.

Revision ID: 0003_knowledge
Revises: 0002_identity
"""

from __future__ import annotations

from alembic import op

revision = "0003_knowledge"
down_revision = "0002_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE knowledge_sources (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name              TEXT NOT NULL UNIQUE,
            source_type       source_type NOT NULL,
            description       TEXT,
            business_owner_id UUID NOT NULL REFERENCES users(id),
            department        TEXT NOT NULL,
            default_classification classification NOT NULL,
            location          JSONB NOT NULL,
            access_rules      JSONB NOT NULL DEFAULT '{}',
            sync_cron         TEXT,
            status            source_status NOT NULL DEFAULT 'draft',
            approved_by       UUID REFERENCES users(id),
            approved_at       timestamptz,
            last_sync_at      timestamptz,
            last_sync_status  TEXT,
            next_sync_at      timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            -- BR-001 as a database constraint rather than application discipline:
            -- a source cannot become active without a recorded approver.
            CONSTRAINT active_requires_approval
                CHECK (status <> 'active' OR approved_by IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE INDEX ix_sources_due ON knowledge_sources (status, next_sync_at)
        WHERE status = 'active'
    """)

    # A family groups every version of one logical document, so version-aware
    # retrieval (FR-017) is a predicate rather than a correlated subquery.
    op.execute("""
        CREATE TABLE document_families (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id       UUID NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
            external_key    TEXT NOT NULL,
            canonical_title TEXT NOT NULL,
            current_document_id UUID,
            UNIQUE (source_id, external_key)
        )
    """)

    op.execute("""
        CREATE TABLE documents (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            family_id      UUID NOT NULL REFERENCES document_families(id) ON DELETE CASCADE,
            source_id      UUID NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,

            title          TEXT NOT NULL,
            doc_type       TEXT,
            language       TEXT NOT NULL DEFAULT 'en',
            source_uri     TEXT NOT NULL,      -- BR-004: the authoritative original
            mime_type      TEXT NOT NULL,
            byte_size      BIGINT,
            page_count     INTEGER,
            content_hash   BYTEA NOT NULL,     -- drives skip-unchanged on re-sync

            classification classification NOT NULL,
            owner_user_id  UUID REFERENCES users(id),
            department     TEXT,

            version_label  TEXT,
            version_seq    INTEGER NOT NULL DEFAULT 1,
            lifecycle      lifecycle_status NOT NULL DEFAULT 'draft',
            supersedes_id  UUID REFERENCES documents(id),
            published_at   DATE,
            effective_from DATE,
            effective_to   DATE,

            ingest_status  ingest_status NOT NULL DEFAULT 'pending',
            ingest_error   JSONB,
            chunk_count    INTEGER NOT NULL DEFAULT 0,
            indexed_at     timestamptz,
            last_synced_at timestamptz,
            injection_risk SMALLINT NOT NULL DEFAULT 0,   -- FR-036 ingest screening
            review_required BOOLEAN NOT NULL DEFAULT FALSE,

            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT valid_effective_window
                CHECK (effective_to IS NULL OR effective_from IS NULL
                       OR effective_to >= effective_from),
            CONSTRAINT injection_risk_range CHECK (injection_risk BETWEEN 0 AND 100),
            UNIQUE (family_id, version_seq)
        )
    """)
    op.execute("""
        ALTER TABLE document_families
            ADD CONSTRAINT fk_current_doc
            FOREIGN KEY (current_document_id) REFERENCES documents(id)
    """)
    op.execute("CREATE INDEX ix_documents_source_status ON documents (source_id, ingest_status)")
    op.execute("CREATE INDEX ix_documents_family_ver ON documents (family_id, version_seq DESC)")
    op.execute("""
        CREATE INDEX ix_documents_live ON documents (lifecycle)
        WHERE lifecycle IN ('active','review_required')
    """)
    op.execute("CREATE INDEX ix_documents_title_trgm ON documents USING gin (title gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_documents_review ON documents (review_required) WHERE review_required"
    )

    # The authoritative access list. chunks.acl_principals is DERIVED from this
    # and never edited directly — the reconciler is the only writer of that array.
    op.execute("""
        CREATE TABLE document_acl (
            document_id     UUID   NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            principal_id    BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
            source_of_truth TEXT NOT NULL DEFAULT 'source_repository'
                            CHECK (source_of_truth IN ('source_repository','askau_override')),
            synced_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (document_id, principal_id)
        )
    """)
    op.execute("CREATE INDEX ix_document_acl_principal ON document_acl (principal_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE document_families DROP CONSTRAINT IF EXISTS fk_current_doc")
    for t in ("document_acl", "documents", "document_families", "knowledge_sources"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
