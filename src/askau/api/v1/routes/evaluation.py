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

from askau.api.deps import AuthzDep, OrchestratorDep
from askau.core.rbac import require_system_admin
from askau.evaluation.datasets import ALL, CORE, SECURITY, THRESHOLDS
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

    runner = EvaluationRunner(orch, request.app.state.engine)
    report = await runner.run(questions)
    passed, breaches = report.gate(THRESHOLDS)

    return {
        "dataset": dataset,
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
