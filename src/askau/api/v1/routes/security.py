"""Audit and security surfaces (FR-051, FR-052, §6.9).

Read and export only. There is no endpoint here — in any role — that mutates an
audit row. BR-008 requires the log be trustworthy to a reviewer who does not
trust the application, and the database enforces the same rule: the application
role holds SELECT and INSERT on ``audit_events`` and nothing else.

Gated to ``security_admin`` specifically, not to administrators generally: who
accessed what is a different sensitivity from how ingestion is going.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.core.rbac import require_security_admin

router = APIRouter(prefix="/v1/security", tags=["security"])

#: A fixed filter block. Nothing is interpolated into it — every value arrives
#: as a bound parameter, and the NULL guards let one statement serve every
#: combination of filters without composing SQL per request.
# Every parameter is cast explicitly. Postgres cannot infer a type for a bare
# parameter in `:x IS NULL`, and the failure is a runtime 500 rather than
# anything a test of the SQL text would catch.
_FILTERS = """
    WHERE (CAST(:category AS text) IS NULL OR event_category = CAST(:category AS text))
      AND (CAST(:event_type AS text) IS NULL OR event_type = CAST(:event_type AS text))
      AND (CAST(:outcome AS text) IS NULL
           OR outcome = CAST(:outcome AS audit_outcome))
      AND (CAST(:actor AS text) IS NULL OR lower(actor_email) = lower(CAST(:actor AS text)))
      AND (CAST(:correlation_id AS text) IS NULL
           OR correlation_id = CAST(:correlation_id AS uuid))
      AND occurred_at > now() - make_interval(days => CAST(:days AS int))
"""


def _params(
    category: str | None,
    event_type: str | None,
    outcome: str | None,
    actor: str | None,
    correlation_id: str | None,
    days: int,
) -> dict[str, Any]:
    return {
        "category": category,
        "event_type": event_type,
        "outcome": outcome,
        "actor": actor,
        "correlation_id": correlation_id,
        "days": days,
    }


@router.get("/audit-events")
async def audit_events(
    authz: AuthzDep,
    request: Request,
    category: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    #: The field an investigation actually starts from: one correlation ID
    #: reconstructs a whole request across auth, retrieval and generation.
    correlation_id: Annotated[str | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=365)] = 7,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    require_security_admin(authz)
    params = _params(category, event_type, outcome, actor, correlation_id, days)
    params["limit"] = limit

    events_sql = f"""
        SELECT id, occurred_at, correlation_id::text, event_type,
               event_category, outcome::text AS outcome, actor_email,
               resource_type, resource_id, detail
        FROM audit_events {_FILTERS}
        ORDER BY occurred_at DESC LIMIT :limit
    """  # noqa: S608
    summary_sql = f"""
        SELECT event_category, outcome::text AS outcome, count(*) AS n
        FROM audit_events {_FILTERS}
        GROUP BY 1, 2 ORDER BY n DESC
    """  # noqa: S608

    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(text(events_sql), params)).mappings().all()
        summary = (await conn.execute(text(summary_sql), params)).mappings().all()

    return {
        "count": len(rows),
        "window_days": days,
        "summary": [dict(r) for r in summary],
        "events": [dict(r) for r in rows],
    }


@router.get("/audit-events/export")
async def export_audit(
    authz: AuthzDep,
    request: Request,
    category: Annotated[str | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> StreamingResponse:
    """NDJSON export.

    §6.9 requires records be producible *intelligibly* on lawful request. An
    audit log only an engineer with database access can read does not satisfy
    that, so a compliance officer can produce a complete record here without an
    engineer mediating it.
    """
    require_security_admin(authz)
    params = _params(category, None, None, None, None, days)

    export_sql = f"""
        SELECT occurred_at, correlation_id::text, event_type, event_category,
               outcome::text AS outcome, actor_email, resource_type,
               resource_id, detail
        FROM audit_events {_FILTERS} ORDER BY occurred_at
    """  # noqa: S608

    async def lines() -> AsyncIterator[str]:
        async with request.app.state.engine.connect() as conn:
            result = await conn.stream(text(export_sql), params)
            async for row in result.mappings():
                record = dict(row)
                record["occurred_at"] = record["occurred_at"].isoformat()
                yield json.dumps(record) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="askau-audit.ndjson"'},
    )


@router.get("/access-denials")
async def access_denials(
    authz: AuthzDep, request: Request, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, Any]:
    """Unauthorized-access attempts (FR-051).

    A dedicated view rather than an audit filter because the acceptance
    criterion for unauthorized retrieval is zero — this is the page someone
    checks on purpose, not a query they have to remember how to construct.
    """
    require_security_admin(authz)
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                SELECT occurred_at, actor_email, resource_type, resource_id,
                       event_type, correlation_id::text
                FROM audit_events
                WHERE outcome = 'denied'
                  AND occurred_at > now() - make_interval(days => CAST(:days AS int))
                ORDER BY occurred_at DESC LIMIT 200
                """),
                    {"days": days},
                )
            )
            .mappings()
            .all()
        )
        repeat = (
            (
                await conn.execute(
                    text("""
                SELECT actor_email, count(*) AS attempts,
                       count(DISTINCT resource_id) AS distinct_resources
                FROM audit_events
                WHERE outcome = 'denied'
                  AND occurred_at > now() - make_interval(days => CAST(:days AS int))
                GROUP BY actor_email HAVING count(*) > 1
                ORDER BY attempts DESC
                """),
                    {"days": days},
                )
            )
            .mappings()
            .all()
        )
    return {
        "count": len(rows),
        "window_days": days,
        # One denial is a mistake; the same person denied repeatedly across
        # different documents is a pattern worth a second look.
        "repeat_actors": [dict(r) for r in repeat],
        "denials": [dict(r) for r in rows],
    }


@router.get("/ai-safety-events")
async def ai_safety_events(
    authz: AuthzDep, request: Request, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, Any]:
    """Injection detections and blocked outputs (FR-036, FR-038)."""
    require_security_admin(authz)
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                SELECT occurred_at, event_type, outcome::text AS outcome,
                       actor_email, resource_id, detail, correlation_id::text
                FROM audit_events
                WHERE event_category = 'ai_safety'
                  AND occurred_at > now() - make_interval(days => CAST(:days AS int))
                ORDER BY occurred_at DESC LIMIT 200
                """),
                    {"days": days},
                )
            )
            .mappings()
            .all()
        )
        flagged = (
            (
                await conn.execute(
                    text("""
                SELECT d.id::text, d.title, d.injection_risk, d.review_required,
                       ks.name AS source_name
                FROM documents d JOIN knowledge_sources ks ON ks.id = d.source_id
                WHERE d.injection_risk >= 50 ORDER BY d.injection_risk DESC LIMIT 50
                """)
                )
            )
            .mappings()
            .all()
        )
    return {
        "count": len(rows),
        "window_days": days,
        "events": [dict(r) for r in rows],
        # Runtime detections and the documents that scored high at ingest are
        # the same investigation, so they belong on the same page.
        "flagged_documents": [dict(r) for r in flagged],
    }


@router.get("/retention")
async def retention(authz: AuthzDep, request: Request) -> dict[str, Any]:
    """Retention posture (§6.5).

    Reports what is actually stored and how old it is, not just the configured
    policy — a policy nobody enforces reads identically to one that works.
    """
    require_security_admin(authz)
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                SELECT 'conversations' AS store, count(*) AS rows,
                       min(created_at) AS oldest,
                       count(*) FILTER (WHERE purge_after IS NOT NULL) AS scheduled
                FROM conversations
                UNION ALL
                SELECT 'messages', count(*), min(created_at), 0 FROM messages
                UNION ALL
                SELECT 'audit_events', count(*), min(occurred_at), 0 FROM audit_events
                UNION ALL
                SELECT 'model_invocations', count(*), min(occurred_at), 0
                FROM model_invocations
                """)
                )
            )
            .mappings()
            .all()
        )
    return {
        "stores": [dict(r) for r in rows],
        "policy": {
            # Open item in the SRS: whether conversation history is a retained
            # record or transient user data is a records-management judgement,
            # not an engineering one (see 10-traceability.md).
            "conversation_retention_days": None,
            "status": "awaiting AUC decision (§6.5, §6.9)",
            "audit_retention": "no automatic purge; partitions archived by ops",
        },
    }
