"""Identity and authorization tables.

Revision ID: 0002_identity
Revises: 0001_extensions
"""

from __future__ import annotations

from alembic import op

revision = "0002_identity"
down_revision = "0001_extensions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # BIGINT identity, not UUID: these values live inside chunks.acl_principals
    # on the hot path, where array size determines cache residency (ADR-0002).
    op.execute("""
        CREATE TABLE principals (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind         principal_kind NOT NULL,
            external_id  TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            synced_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (kind, external_id)
        )
    """)

    op.execute("""
        CREATE TABLE users (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id  BIGINT NOT NULL UNIQUE REFERENCES principals(id),
            entra_oid     TEXT NOT NULL UNIQUE,
            email         TEXT NOT NULL UNIQUE,
            display_name  TEXT NOT NULL,
            department    TEXT,
            job_title     TEXT,
            preferred_language TEXT NOT NULL DEFAULT 'en',
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            -- Bumped on any membership change. Invalidates the cached
            -- authorization context and every answer-cache entry keyed by it,
            -- in one UPDATE rather than a cache-wide sweep (FR-025).
            acl_version   INTEGER NOT NULL DEFAULT 1,
            last_login_at timestamptz,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)

    # Transitive group membership, flattened at sync time so no request ever
    # walks a group graph.
    op.execute("""
        CREATE TABLE user_principals (
            user_id      UUID   NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            principal_id BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
            granted_via  TEXT   NOT NULL DEFAULT 'entra_sync',
            synced_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, principal_id)
        )
    """)
    op.execute("CREATE INDEX ix_user_principals_principal ON user_principals (principal_id)")

    # AskAU application roles, deliberately separate from Entra groups: authority
    # over AskAU itself is granted explicitly and audited, not inherited from a
    # directory group someone joined for an unrelated reason.
    op.execute("""
        CREATE TABLE app_role_assignments (
            user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role       TEXT NOT NULL CHECK (role IN
                         ('end_user','knowledge_admin','system_admin','security_admin')),
            granted_by UUID REFERENCES users(id),
            granted_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, role)
        )
    """)

    op.execute("""
        CREATE TABLE sessions (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            issued_at  timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            ip_hash    BYTEA,          -- hashed, never raw (NFR-005)
            user_agent TEXT
        )
    """)
    op.execute("""
        CREATE INDEX ix_sessions_active ON sessions (user_id)
        WHERE revoked_at IS NULL
    """)


def downgrade() -> None:
    for t in ("sessions", "app_role_assignments", "user_principals", "users", "principals"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
