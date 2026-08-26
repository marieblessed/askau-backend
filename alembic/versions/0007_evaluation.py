"""Evaluation harness tables (SRS §7.5 — a production acceptance gate).

Revision ID: 0007_evaluation
Revises: 0006_ops
"""

from __future__ import annotations

from alembic import op

revision = "0007_evaluation"
down_revision = "0006_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE eval_datasets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL CHECK (category IN
                       ('retrieval','generation','security','performance')),
            description TEXT,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE eval_questions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dataset_id UUID NOT NULL REFERENCES eval_datasets(id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            expected_answer TEXT,
            -- Ground truth references document FAMILIES, not versions, so a new
            -- revision does not silently invalidate the label.
            expected_family_ids UUID[],
            expected_state answer_state,
            as_principal_id BIGINT REFERENCES principals(id),
            -- The schema-level expression of "unauthorized retrieval = 0": the
            -- security suite asserts ABSENCE, as a specific identity.
            must_not_retrieve UUID[],
            tags TEXT[]
        )
    """)
    op.execute("""
        CREATE TABLE eval_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dataset_id UUID NOT NULL REFERENCES eval_datasets(id),
            git_sha TEXT, model TEXT, prompt_hash TEXT, retriever TEXT,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            metrics JSONB NOT NULL DEFAULT '{}',
            passed BOOLEAN
        )
    """)
    op.execute("""
        CREATE TABLE eval_results (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
            question_id UUID NOT NULL REFERENCES eval_questions(id),
            actual_answer TEXT,
            actual_state answer_state,
            retrieved_family_ids UUID[],
            scores JSONB NOT NULL DEFAULT '{}',
            passed BOOLEAN NOT NULL,
            failure_reason TEXT
        )
    """)
    op.execute("CREATE INDEX ix_eval_results_run ON eval_results (run_id, passed)")

    for table in ("eval_datasets", "eval_questions", "eval_runs", "eval_results"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO askau_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO askau_app")


def downgrade() -> None:
    for t in ("eval_results", "eval_runs", "eval_questions", "eval_datasets"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
