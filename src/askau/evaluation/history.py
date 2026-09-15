"""Persisting evaluation runs, so quality has a history (SRS §7.5).

The harness could already score a run and fail a build on it. What it could not
do is answer the question anybody actually asks after a change: **did this make
retrieval worse?** A score with nothing to compare against is a number, not a
measurement — `eval_runs` and `eval_results` have existed since the first
migration and nothing had ever written to them.

## What makes two runs comparable

`eval_runs` records four things beside the metrics, and they are the reason the
table is useful rather than merely full: `git_sha`, `model`, `prompt_hash` and
`retriever`. A drop in mean reciprocal rank means one thing if those four match
the previous run and nothing at all if they do not — comparing a run on
`bge-m3` against a run on hash embeddings would produce a confident,
meaningless regression.

`prompt_hash` is there because the system prompt is a file, not code. It can
change without a commit that looks related to retrieval, and a quality shift
with no apparent cause is exactly what it produces.

## Why the questions are materialised

`eval_results.question_id` is a foreign key to `eval_questions`, and the
harness's datasets live in Python (`datasets.py`) rather than in the database.
So a run first upserts its questions as rows. That is not bookkeeping: it puts
the curated datasets and the questions sampled from real use (§5.3,
`db/eval_sampling.py`) in one table, which is what lets a future run be scored
over both.

Upserted on `(dataset, external key)` rather than inserted, so re-running a
dataset does not multiply its questions — and so a question whose wording is
corrected keeps its id and its history rather than starting a new series.

## Failure is not fatal

Recording a run must never fail the run. A quality measurement that cannot be
saved is still a quality measurement, and turning a full-disk into a red build
that looks like a retrieval regression would waste exactly the time this table
exists to save.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from askau.evaluation.runner import EvalQuestion, EvalReport

_log = logging.getLogger(__name__)

_PROMPT = Path(__file__).resolve().parents[1] / "rag" / "prompts" / "system_v1.md"


def prompt_hash() -> str:
    """Short digest of the system prompt.

    The prompt is a file that ships with the wheel and can change without any
    commit that looks related to retrieval quality. Recording it turns "scores
    moved and nobody knows why" into a one-line diff.
    """
    try:
        return hashlib.sha256(_PROMPT.read_bytes()).hexdigest()[:12]
    except OSError:  # pragma: no cover - the packaging test covers its presence
        return ""


def git_sha() -> str | None:
    """The working tree's commit, or `None` outside a checkout.

    Read from `.git` rather than by running `git`. A deployed container often
    has neither the binary nor the repository, and shelling out for a string
    that is sitting in a file is a subprocess and a failure mode for nothing.

    `None` rather than a placeholder: a run recorded where the revision is
    genuinely unknown should have an empty column a query can filter on, not the
    word "unknown" sitting where a revision belongs.
    """
    # Walked rather than counted. A fixed `parents[n]` is wrong the moment the
    # package moves or the wheel is installed somewhere else, and it fails
    # silently — returning `None` as though the revision were unknowable.
    root = None
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            root = candidate / ".git"
            break
    if root is None:
        return None

    try:
        head = (root / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not head.startswith("ref:"):
        return head[:12] or None  # detached HEAD holds the sha directly

    ref = head.removeprefix("ref:").strip()
    try:
        return (root / ref).read_text(encoding="utf-8").strip()[:12] or None
    except OSError:
        pass
    # Packed refs: a freshly cloned or gc'd repository has no loose ref file.
    try:
        for line in (root / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0][:12]
    except OSError:
        pass
    return None


class EvalHistory:
    """Writes a run and its per-question results."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self,
        *,
        dataset: str,
        questions: list[EvalQuestion],
        report: EvalReport,
        passed: bool,
        breaches: list[str],
        model: str,
        retriever: str,
    ) -> str | None:
        """Persist one run. Returns its id, or `None` if it could not be saved."""
        try:
            async with self._engine.begin() as conn:
                dataset_id = (
                    await conn.execute(
                        text("""
                        INSERT INTO eval_datasets (name, category, description)
                        VALUES (:name, 'retrieval', :desc)
                        ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description
                        RETURNING id
                        """),
                        {
                            "name": f"harness-{dataset}",
                            "desc": f"Curated {dataset} questions from askau.evaluation.datasets.",
                        },
                    )
                ).scalar_one()

                question_ids = await self._upsert_questions(conn, dataset_id, questions)

                metrics: dict[str, Any] = dict(report.metrics())
                metrics["breaches"] = breaches
                metrics["duration_ms"] = report.duration_ms

                run_id = str(
                    (
                        await conn.execute(
                            text("""
                            INSERT INTO eval_runs
                                (dataset_id, git_sha, model, prompt_hash, retriever,
                                 finished_at, metrics, passed)
                            VALUES (:d, :sha, :model, :ph, :retriever,
                                    now(), CAST(:metrics AS jsonb), :passed)
                            RETURNING id
                            """),
                            {
                                "d": dataset_id,
                                "sha": git_sha(),
                                "model": model,
                                "ph": prompt_hash(),
                                "retriever": retriever,
                                "metrics": _json(metrics),
                                "passed": passed,
                            },
                        )
                    ).scalar_one()
                )

                for result in report.results:
                    question_id = question_ids.get(result.question_id)
                    if question_id is None:  # pragma: no cover - upsert covers every question
                        continue
                    await conn.execute(
                        text("""
                        INSERT INTO eval_results
                            (run_id, question_id, actual_answer, actual_state,
                             retrieved_family_ids, scores, passed, failure_reason)
                        VALUES (CAST(:run AS uuid), :q, NULL,
                                CAST(:state AS answer_state), CAST(:fams AS uuid[]),
                                CAST(:scores AS jsonb), :passed, :failure)
                        """),
                        {
                            "run": run_id,
                            "q": question_id,
                            # `actual_answer` stays NULL. The answer to a
                            # curated question is reproducible by re-running it,
                            # and storing generated text for every question of
                            # every run would grow this table without adding a
                            # fact the scores do not already carry.
                            "state": result.actual_state or None,
                            "fams": [f for f in result.retrieved_families if _is_uuid(f)],
                            "scores": _json(
                                {
                                    "groundedness": result.groundedness,
                                    "latency_ms": result.latency_ms,
                                    "unsupported": list(result.unsupported),
                                    "known_limitation": result.known_limitation,
                                }
                            ),
                            "passed": result.passed,
                            "failure": result.failure,
                        },
                    )
                return run_id
        except Exception:
            # Never fatal. A measurement that cannot be saved is still a
            # measurement, and a full disk reported as a red build looks exactly
            # like the retrieval regression this table exists to detect.
            _log.warning("evaluation run could not be recorded", exc_info=True)
            return None

    async def _upsert_questions(
        self, conn: Any, dataset_id: Any, questions: list[EvalQuestion]
    ) -> dict[str, Any]:
        """Materialise the in-code dataset as rows, keyed by the harness's own id.

        Keyed on the harness id rather than the text so that correcting a
        question's wording keeps its history — a renamed question that started a
        new series would look like a fixed bug and a new failure at once.
        """
        ids: dict[str, Any] = {}
        for q in questions:
            ids[q.id] = (
                await conn.execute(
                    text("""
                    INSERT INTO eval_questions
                        (dataset_id, question, expected_state, tags)
                    SELECT :d, :text, CAST(:state AS answer_state), CAST(:tags AS text[])
                    WHERE NOT EXISTS (
                        SELECT 1 FROM eval_questions
                        WHERE dataset_id = :d AND :harness_id = ANY(tags)
                    )
                    RETURNING id
                    """),
                    {
                        "d": dataset_id,
                        "text": q.question,
                        "state": q.expected_state,
                        # The harness id travels as a tag because
                        # `eval_questions` has no external-key column and
                        # inventing one would be a migration for a lookup.
                        "tags": [q.id, *q.tags],
                        "harness_id": q.id,
                    },
                )
            ).scalar_one_or_none()
            if ids[q.id] is None:
                ids[q.id] = (
                    await conn.execute(
                        text("""
                        SELECT id FROM eval_questions
                        WHERE dataset_id = :d AND :harness_id = ANY(tags) LIMIT 1
                        """),
                        {"d": dataset_id, "harness_id": q.id},
                    )
                ).scalar_one()
        return ids


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


def _is_uuid(value: str) -> bool:
    """`retrieved_family_ids` is a uuid array and the harness's family
    identifiers are not always uuids — the security dataset names them. Filtered
    rather than cast, because a failed cast would abort a whole run's recording
    over a cosmetic column."""
    from uuid import UUID

    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
