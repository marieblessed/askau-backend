"""Evaluation over HTTP (SRS §7.5).

The CLI is what CI calls; this is what the administration console reads. Both
run the same runner against the same dataset, so a green pipeline and a green
page cannot disagree.

Runs are executed inline rather than queued: the set is twenty questions against
a local pipeline and finishes in under a second. Queuing it would add a state
machine to manage for no benefit — revisit when the set reaches the 300
questions the design calls for.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from askau.api.deps import AuthzDep, OrchestratorDep
from askau.core.rbac import require_system_admin
from askau.evaluation.datasets import ALL, CORE, SECURITY, THRESHOLDS
from askau.evaluation.history import EvalHistory
from askau.evaluation.runner import EvaluationRunner

router = APIRouter(prefix="/v1/evaluation", tags=["evaluation"])


@router.get("/datasets")
async def datasets(authz: AuthzDep) -> dict[str, Any]:
    require_system_admin(authz)

    def describe(name: str, questions: list[Any]) -> dict[str, Any]:
        tags: dict[str, int] = {}
        for q in questions:
            for t in q.tags:
                tags[t] = tags.get(t, 0) + 1
        refusals = sum(
            1
            for q in questions
            if q.expected_state in {"insufficient_evidence", "out_of_scope", "clarification_needed"}
        )
        return {
            "name": name,
            "questions": len(questions),
            # The share the system is expected NOT to answer. A set of only
            # answerable questions cannot detect confident fabrication.
            "expected_refusals": refusals,
            "refusal_share": round(refusals / len(questions), 2) if questions else 0,
            "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
        }

    return {
        "datasets": [
            describe("core", CORE),
            describe("security", SECURITY),
            describe("all", ALL),
        ],
        "thresholds": THRESHOLDS,
    }


@router.post("/runs")
async def run_evaluation(
    authz: AuthzDep,
    orch: OrchestratorDep,
    request: Request,
    dataset: Annotated[Literal["all", "core", "security"], Query()] = "all",
) -> dict[str, Any]:
    require_system_admin(authz)
    questions = {"all": ALL, "core": CORE, "security": SECURITY}[dataset]

    engine = request.app.state.engine
    runner = EvaluationRunner(orch, engine)
    report = await runner.run(questions)
    passed, breaches = report.gate(THRESHOLDS)

    # Recorded so a score can be compared with the last one. A run that is only
    # returned answers "is quality acceptable today"; a run that is stored
    # answers "did that change make it worse", which is the question anybody
    # actually asks.
    settings = request.app.state.settings
    run_id = await EvalHistory(engine).record(
        dataset=dataset,
        questions=questions,
        report=report,
        passed=passed,
        breaches=breaches,
        model=f"{settings.llm_provider}:{settings.llm_model}",
        retriever=settings.retriever,
    )

    return {
        "dataset": dataset,
        # `None` when the run could not be saved. Surfaced rather than hidden:
        # an operator comparing runs needs to know one is missing from the
        # series, and recording is deliberately non-fatal.
        "run_id": run_id,
        "passed": passed,
        "breaches": breaches,
        "metrics": report.metrics(),
        "duration_ms": report.duration_ms,
        "failures": [
            {
                "question_id": r.question_id,
                "question": r.question,
                "expected_vs_actual": r.failure,
                "actual_state": r.actual_state,
            }
            for r in report.failures
        ],
        "known_limitations": [
            {"question_id": r.question_id, "reason": r.known_limitation} for r in report.known
        ],
    }


@router.get("/runs")
async def run_history(
    authz: AuthzDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Past runs, newest first — the series a score is read against.

    Each row carries the four things that decide whether two runs are
    comparable: `gitSha`, `model`, `promptHash` and `retriever`. A drop in mean
    reciprocal rank means something if those match the run before it and nothing
    at all if they do not, so they are returned beside the metrics rather than
    left for somebody to look up.
    """
    require_system_admin(authz)
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                    SELECT r.id::text AS id, d.name AS dataset, r.git_sha, r.model,
                           r.prompt_hash, r.retriever, r.started_at, r.finished_at,
                           r.passed, r.metrics,
                           (SELECT count(*) FROM eval_results er WHERE er.run_id = r.id)
                             AS question_count,
                           (SELECT count(*) FROM eval_results er
                            WHERE er.run_id = r.id AND NOT er.passed) AS failure_count
                    FROM eval_runs r
                    JOIN eval_datasets d ON d.id = r.dataset_id
                    ORDER BY r.started_at DESC
                    LIMIT :limit
                    """),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )

    return {
        "items": [
            {
                "id": r["id"],
                "dataset": r["dataset"],
                "gitSha": r["git_sha"],
                "model": r["model"],
                "promptHash": r["prompt_hash"],
                "retriever": r["retriever"],
                "startedAt": r["started_at"].isoformat() if r["started_at"] else None,
                "finishedAt": r["finished_at"].isoformat() if r["finished_at"] else None,
                "passed": r["passed"],
                "questionCount": r["question_count"],
                "failureCount": r["failure_count"],
                "metrics": r["metrics"],
            }
            for r in rows
        ],
        "total": len(rows),
    }
