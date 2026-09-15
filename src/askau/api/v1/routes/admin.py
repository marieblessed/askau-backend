"""Administration console API (FR-046 … FR-053, §7.4).

Read surfaces for knowledge and system administrators. Every endpoint is
role-gated server-side — the interface hiding a control is a courtesy, not a
control.

These are administrative views over platform state, not over user content:
nothing here returns a question, an answer, or a conversation. An administrator
troubleshooting ingestion has no business reading what staff asked, and §6.5
restricts administrator access to conversations specifically.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.api.schemas.wire import WireModel
from askau.core.errors import NotFoundError
from askau.core.rbac import require_knowledge_admin, require_system_admin
from askau.settings import Settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])

#: Every SQL fragment this module can interpolate, by key. Composition selects
#: from here and nowhere else — a caller cannot introduce a fragment, so the
#: only values reaching the database are bound parameters. Ruff's S608 warning
#: is about string-built SQL in general; the suppression below is safe only
#: because the vocabulary is closed, and it stops being safe the moment someone
#: interpolates a variable into this dict.
_PREDICATES: dict[str, str] = {
    "lifecycle": "d.lifecycle = CAST(:lifecycle AS lifecycle_status)",
    "ingest_status": "d.ingest_status = CAST(:ingest_status AS ingest_status)",
    "classification": "d.classification = CAST(:classification AS classification)",
    "title": "d.title ILIKE :q",
    "needs_attention": (
        "(d.review_required OR d.ingest_status = 'failed' "
        "OR d.lifecycle = 'review_required' "
        "OR (d.lifecycle = 'expired' AND d.ingest_status = 'indexed'))"
    ),
    "expired_indexed": "(d.lifecycle = 'expired' AND d.ingest_status = 'indexed')",
    "never_synced": "d.last_synced_at IS NULL",
    "high_risk": "d.injection_risk >= 50",
}


async def _rows(request: Request, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    async with request.app.state.engine.connect() as conn:
        return list((await conn.execute(text(sql), params or {})).mappings().all())


async def _one(request: Request, sql: str, params: dict[str, Any] | None = None) -> Any:
    async with request.app.state.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).mappings().first()


# ── overview (FR-048) ───────────────────────────────────────────────────────

_OVERVIEW = """
SELECT
  (SELECT count(*) FROM documents)                                    AS documents,
  (SELECT count(*) FROM documents WHERE ingest_status = 'indexed')    AS indexed,
  (SELECT count(*) FROM documents WHERE ingest_status = 'failed')     AS failed,
  (SELECT count(*) FROM documents WHERE review_required)              AS review_required,
  (SELECT count(*) FROM documents
     WHERE lifecycle = 'expired' AND ingest_status = 'indexed')       AS expired_indexed,
  (SELECT coalesce(sum(chunk_count), 0) FROM documents)               AS chunks,
  (SELECT count(*) FROM knowledge_sources)                            AS sources,
  (SELECT count(*) FROM knowledge_sources WHERE status = 'active')    AS sources_active,
  (SELECT count(*) FROM knowledge_sources
     WHERE status = 'active' AND approved_by IS NULL)                 AS sources_unapproved,
  (SELECT count(*) FROM ingestion_runs WHERE status = 'running')      AS runs_active,
  (SELECT count(*) FROM ingestion_tasks WHERE stage = 'failed')       AS tasks_failed,
  (SELECT count(*) FROM audit_events
     WHERE occurred_at > now() - interval '24 hours')                 AS events_24h,
  (SELECT count(*) FROM audit_events
     WHERE event_category = 'security' AND outcome = 'denied'
       AND occurred_at > now() - interval '24 hours')                 AS denials_24h,
  (SELECT count(*) FROM audit_events
     WHERE event_category = 'ai_safety'
       AND occurred_at > now() - interval '24 hours')                 AS ai_safety_24h,
  (SELECT max(indexed_at) FROM documents)                             AS acl_synced_at,
  (SELECT min(indexed_at) FROM documents WHERE indexed_at IS NOT NULL) AS acl_oldest
"""

# Note on `chunks`: this is summed from `documents.chunk_count`, not counted
# from `chunks` directly. Row-level security correctly hides chunk rows from the
# application role when no principals are set, so a direct count returns zero —
# the right answer to the wrong question. Reaching for an elevated connection to
# get a dashboard number would be the wrong fix; the figure already exists on a
# table the administrator may read.


@router.get("/overview")
async def overview(authz: AuthzDep, request: Request) -> dict[str, Any]:
    require_knowledge_admin(authz)
    row = await _one(request, _OVERVIEW)
    settings = request.app.state.settings

    lag_seconds = None
    if row["acl_oldest"] is not None and row["acl_synced_at"] is not None:
        lag_seconds = int((row["acl_synced_at"] - row["acl_oldest"]).total_seconds())

    return {
        "knowledge": {
            "documents": row["documents"],
            "indexed": row["indexed"],
            "failed": row["failed"],
            "review_required": row["review_required"],
            "expired_still_indexed": row["expired_indexed"],
            "chunks": row["chunks"],
        },
        "sources": {
            "total": row["sources"],
            "active": row["sources_active"],
            "active_without_approval": row["sources_unapproved"],
        },
        "ingestion": {
            "runs_in_progress": row["runs_active"],
            "documents_failed": row["tasks_failed"],
        },
        "security": {
            # Any access denial is worth an administrator's attention: the
            # acceptance criterion for unauthorized retrieval is zero, so a
            # non-zero count here is a signal, not a statistic.
            "access_denials_24h": row["denials_24h"],
            "ai_safety_events_24h": row["ai_safety_24h"],
            "audit_events_24h": row["events_24h"],
            # FR-025: how stale the materialized access lists are.
            "acl_sync_lag_seconds": lag_seconds,
            "acl_sync_interval_seconds": settings.acl_sync_interval_seconds,
        },
        "configuration": {
            "environment": settings.env,
            "auth_mode": settings.auth_mode,
            "retriever": settings.retriever,
            "reranker": settings.reranker,
            "llm": f"{settings.llm_provider}:{settings.llm_model}",
            "embedder": f"{settings.embedding_provider}:{settings.embedding_model}",
            "ocr": _ocr_status(settings),
        },
    }


# ── documents (FR-046, FR-047) ──────────────────────────────────────────────


@router.get("/documents")
async def documents(
    authz: AuthzDep,
    request: Request,
    lifecycle: Annotated[str | None, Query()] = None,
    ingest_status: Annotated[str | None, Query()] = None,
    classification: Annotated[str | None, Query()] = None,
    #: The lifecycle questions administrators actually ask, as one parameter.
    #: "Show me what needs attention" is the real query; assembling it from
    #: four filters every time is work the interface should absorb.
    view: Annotated[
        Literal["all", "needs_attention", "expired_indexed", "never_synced", "high_risk"],
        Query(),
    ] = "all",
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    require_knowledge_admin(authz)

    keys = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}

    if lifecycle:
        keys.append(_PREDICATES["lifecycle"])
        params["lifecycle"] = lifecycle
    if ingest_status:
        keys.append(_PREDICATES["ingest_status"])
        params["ingest_status"] = ingest_status
    if classification:
        keys.append(_PREDICATES["classification"])
        params["classification"] = classification
    if q:
        keys.append(_PREDICATES["title"])
        params["q"] = f"%{q}%"
    if view != "all":
        keys.append(_PREDICATES[view])

    # Composed from _PREDICATES only; every value is a bound parameter.
    sql = f"""
        SELECT d.id::text, d.title, d.doc_type, d.language,
               d.classification::text AS classification,
               d.lifecycle::text      AS lifecycle,
               d.ingest_status::text  AS ingest_status,
               d.version_label, d.version_seq, d.chunk_count, d.injection_risk,
               d.review_required, d.effective_from, d.effective_to,
               d.indexed_at, d.last_synced_at, d.updated_at,
               d.ingest_error, ks.name AS source_name,
               u.display_name AS owner
        FROM documents d
        JOIN knowledge_sources ks ON ks.id = d.source_id
        LEFT JOIN users u ON u.id = d.owner_user_id
        WHERE {" AND ".join(keys)}
        ORDER BY d.review_required DESC, d.updated_at DESC
        LIMIT :limit
    """  # noqa: S608

    rows = await _rows(request, sql, params)
    return {"count": len(rows), "view": view, "documents": [dict(r) for r in rows]}


# ── sources (FR-011, FR-045, FR-047) ────────────────────────────────────────


@router.get("/sources")
async def sources(authz: AuthzDep, request: Request) -> dict[str, Any]:
    require_knowledge_admin(authz)
    rows = await _rows(
        request,
        """
        SELECT ks.id::text, ks.name, ks.source_type::text AS source_type,
               ks.department, ks.status::text AS status,
               ks.default_classification::text AS default_classification,
               ks.sync_cron, ks.last_sync_at, ks.next_sync_at,
               ks.approved_by IS NOT NULL AS approved,
               u.display_name AS business_owner,
               (SELECT count(*) FROM documents d WHERE d.source_id = ks.id) AS documents,
               (SELECT count(*) FROM documents d
                  WHERE d.source_id = ks.id AND d.ingest_status = 'failed') AS failed
        FROM knowledge_sources ks
        JOIN users u ON u.id = ks.business_owner_id
        ORDER BY ks.name
        """,
    )
    return {"count": len(rows), "sources": [dict(r) for r in rows]}


# ── ingestion (FR-049, FR-050) ──────────────────────────────────────────────


@router.get("/ingestion/runs")
async def ingestion_runs(
    authz: AuthzDep, request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> dict[str, Any]:
    require_knowledge_admin(authz)
    rows = await _rows(
        request,
        """
        SELECT r.id::text, r.trigger, r.status, r.docs_discovered, r.docs_processed,
               r.docs_skipped, r.docs_failed, r.chunks_written, r.embedding_tokens,
               r.started_at, r.finished_at, ks.name AS source_name,
               EXTRACT(EPOCH FROM (coalesce(r.finished_at, now()) - r.started_at))::int
                   AS duration_seconds
        FROM ingestion_runs r
        JOIN knowledge_sources ks ON ks.id = r.source_id
        ORDER BY r.started_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return {"count": len(rows), "runs": [dict(r) for r in rows]}


@router.get("/ingestion/runs/{run_id}")
async def ingestion_run(run_id: str, authz: AuthzDep, request: Request) -> dict[str, Any]:
    require_knowledge_admin(authz)
    tasks = await _rows(
        request,
        """
        SELECT t.id, t.external_key, t.stage::text AS stage, t.attempts,
               t.error_code, t.error_detail, t.stage_timings, t.updated_at,
               d.title
        FROM ingestion_tasks t
        LEFT JOIN documents d ON d.id = t.document_id
        WHERE t.run_id = CAST(:run_id AS uuid)
        -- Failures first: a run detail page is opened to diagnose, not to browse.
        ORDER BY (t.stage = 'failed') DESC, t.updated_at DESC
        """,
        {"run_id": run_id},
    )
    return {"run_id": run_id, "count": len(tasks), "tasks": [dict(t) for t in tasks]}


#: Why a document did not make it into the corpus, grouped so the shape of the
#: problem is visible rather than spread across per-run detail pages.
#:
#: `no_text_layer` is the one worth calling out: it is the count that decides
#: whether OCR is worth installing at all. Scans are currently rejected, so this
#: number is the size of the gap that decision would close — and it is far more
#: useful measured against the real corpus than estimated in advance.
_FAILURES_BY_CODE = """
SELECT t.error_code,
       count(*)                       AS documents,
       max(t.updated_at)              AS last_seen,
       min(t.updated_at)              AS first_seen
FROM ingestion_tasks t
WHERE t.stage = 'failed' AND t.error_code IS NOT NULL
GROUP BY t.error_code
ORDER BY count(*) DESC
"""

#: Remedies keyed by error code. The console should say what to *do*, not name a
#: failure and leave an administrator to infer the action from the identifier.
_REMEDIES: dict[str, str] = {
    "no_text_layer": (
        "Scanned documents with no extractable text. Ask the source owner for the "
        "original, or configure OCR — it runs as a Tika container, never as a host "
        "install. Set ASKAU_OCR_PROVIDER and ASKAU_OCR_URL."
    ),
    "insufficient_text": (
        "Extracted almost nothing. Usually a cover sheet, a placeholder, or a "
        "failed export — confirm with the source owner that the file has content."
    ),
    "unsupported_format": "Outside the formats in FR-012. Ask for PDF, DOCX, XLSX, PPTX or HTML.",
    "too_large": "Above the size ceiling. Split it, or raise the limit for this source.",
    "extraction_failed": "The file parsed as its format but could not be read — likely corrupt.",
    "encrypted": "Password-protected. Ask the owner for an unprotected copy.",
    # ── Codes the connector ingestion path emits ────────────────────────────
    #
    # These were all missing, and the reason is instructive: nothing could run
    # connector ingestion at all until `make ingest` existed, so no run had ever
    # produced one of them and the console had never been asked. The first real
    # sync against a live library produced `fetch_error` and the console
    # answered "No remedy recorded for this code."
    #
    # `extraction_error` is not a duplicate of `extraction_failed` above — it is
    # the code the pipeline actually writes. The near-miss is exactly why
    # `test_every_emitted_code_has_a_remedy` now compares the two sets rather
    # than trusting that a plausible-looking key covers it.
    "empty_file": "The file is zero bytes. Ask the source owner to re-upload it.",
    "extraction_error": (
        "Extraction raised on a file of a supported type — usually a truncated "
        "download or a malformed document. Reprocess it; if it fails again, ask "
        "the source owner for a fresh copy."
    ),
    "fetch_error": (
        "The content could not be downloaded. Usually transient — a timeout or a "
        "dropped connection — so reprocess first. If it persists, confirm the item "
        "still exists and that the application's credentials still reach it."
    ),
    "access_unavailable": (
        "The item's permissions could not be read, so its audience is unknown and it "
        "was not ingested — deliberately, since guessing would risk disclosure. Grant "
        "the application permission to read this item's sharing settings, or remove "
        "the anonymous sharing link, then reprocess it."
    ),
    "invalid_classification": (
        "The document declares a classification the schema does not have. Correct its "
        "classification metadata at the source — the accepted values are public, internal, "
        "confidential and highly_restricted — then reprocess it."
    ),
    "no_chunks": (
        "Text was extracted but produced no chunks — typically a document of headings "
        "with no body. Confirm the file has readable content."
    ),
    "ocr_unavailable": (
        "The document needs OCR and no OCR service is configured. Set "
        "ASKAU_OCR_PROVIDER and ASKAU_OCR_URL to a Tika container, then reprocess."
    ),
}


@router.get("/ingestion/failures")
async def ingestion_failures(authz: AuthzDep, request: Request) -> dict[str, Any]:
    """Ingestion failures grouped by cause (FR-049, FR-050).

    Per-run task lists answer "what went wrong in this run". This answers the
    question an administrator actually has across runs: "what is systematically
    keeping documents out of the corpus, and what would fix the most of it".
    """
    require_knowledge_admin(authz)
    rows = await _rows(request, _FAILURES_BY_CODE)
    failures = [
        {
            **dict(r),
            "remedy": _REMEDIES.get(r["error_code"], "No remedy recorded for this code."),
        }
        for r in rows
    ]
    total = sum(int(f["documents"]) for f in failures)
    scanned = next((int(f["documents"]) for f in failures if f["error_code"] == "no_text_layer"), 0)

    return {
        "total_failed": total,
        "failures": failures,
        # Surfaced separately because it is a decision input, not just a count:
        # it is the number of documents OCR would recover, and nothing else in
        # the console answers "is OCR worth turning on".
        "ocr_would_recover": scanned,
    }


# ── usage (FR-053, §7.4) ────────────────────────────────────────────────────


@router.get("/usage")
async def usage(
    authz: AuthzDep, request: Request, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, Any]:
    require_system_admin(authz)
    by_operation = await _rows(
        request,
        """
        SELECT operation, model, provider,
               count(*)                                    AS calls,
               count(*) FILTER (WHERE cached)              AS cached,
               count(*) FILTER (WHERE outcome <> 'success') AS errors,
               sum(input_tokens)                           AS input_tokens,
               sum(output_tokens)                          AS output_tokens,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
        FROM model_invocations
        WHERE occurred_at > now() - make_interval(days => :days)
        GROUP BY operation, model, provider
        ORDER BY calls DESC
        """,
        {"days": days},
    )
    by_department = await _rows(
        request,
        """
        SELECT coalesce(department, 'unattributed') AS department,
               count(*) AS calls,
               sum(input_tokens + output_tokens) AS tokens
        FROM model_invocations
        WHERE occurred_at > now() - make_interval(days => :days)
        GROUP BY 1 ORDER BY tokens DESC NULLS LAST
        """,
        {"days": days},
    )
    return {
        "window_days": days,
        "by_operation": [dict(r) for r in by_operation],
        "by_department": [dict(r) for r in by_department],
    }


# ── quality (FR-054) ────────────────────────────────────────────────────────

_QUALITY = """
WITH answers AS (
    SELECT answer_state, groundedness, ttft_ms, total_ms, created_at
    FROM messages
    WHERE role = 'assistant'
      AND created_at > now() - make_interval(days => CAST(:days AS int))
)
SELECT
  (SELECT count(*) FROM answers)                                        AS answers,
  (SELECT avg(groundedness) FROM answers WHERE groundedness IS NOT NULL) AS groundedness,
  (SELECT percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms)
     FROM answers WHERE ttft_ms IS NOT NULL)                            AS ttft_p95,
  (SELECT percentile_disc(0.95) WITHIN GROUP (ORDER BY total_ms)
     FROM answers WHERE total_ms IS NOT NULL)                           AS total_p95,
  (SELECT count(*) FROM citations c
     JOIN messages m ON m.id = c.message_id AND m.created_at = c.message_created_at
    WHERE m.created_at > now() - make_interval(days => CAST(:days AS int)))  AS citations,
  (SELECT count(*) FROM citations c
     JOIN messages m ON m.id = c.message_id AND m.created_at = c.message_created_at
    WHERE c.verified
      AND m.created_at > now() - make_interval(days => CAST(:days AS int)))  AS citations_verified
"""


@router.get("/quality")
async def quality(
    authz: AuthzDep, request: Request, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, Any]:
    """Answer quality against the §6.8 acceptance thresholds.

    Refusal rate is reported without a target, deliberately. Driving it down
    invites fabrication and driving it up invites uselessness — a sudden *move*
    in either direction is the signal, and it is usually the earliest warning of
    a retrieval regression.
    """
    require_knowledge_admin(authz)
    row = await _one(request, _QUALITY, {"days": days})
    states = await _rows(
        request,
        """
        SELECT answer_state::text AS state, count(*) AS n
        FROM messages
        WHERE role = 'assistant' AND answer_state IS NOT NULL
          AND created_at > now() - make_interval(days => CAST(:days AS int))
        GROUP BY 1 ORDER BY n DESC
        """,
        {"days": days},
    )
    feedback = await _rows(
        request,
        """
        SELECT f.rating::text AS rating, coalesce(f.reason_code, 'none') AS reason,
               count(*) AS n
        FROM message_feedback f
        WHERE f.created_at > now() - make_interval(days => CAST(:days AS int))
        GROUP BY 1, 2 ORDER BY n DESC
        """,
        {"days": days},
    )

    answers = row["answers"] or 0
    refusals = sum(
        r["n"]
        for r in states
        if r["state"] in {"insufficient_evidence", "out_of_scope", "refused_safety"}
    )
    cites = row["citations"] or 0

    return {
        "window_days": days,
        "answers": answers,
        "metrics": {
            # None rather than 0 when there is nothing to measure: a zero here
            # reads as failure, and "no data yet" is a different thing.
            "groundedness": float(row["groundedness"]) if row["groundedness"] else None,
            "citation_accuracy": (row["citations_verified"] / cites) if cites else None,
            "refusal_rate": (refusals / answers) if answers else None,
            "ttft_p95_ms": row["ttft_p95"],
            "total_p95_ms": row["total_p95"],
        },
        "thresholds": {
            "groundedness": 0.90,
            "citation_accuracy": 0.95,
            "ttft_p95_ms": 3000,
            "total_p95_ms": 10000,
        },
        "by_state": [dict(r) for r in states],
        "feedback": [dict(r) for r in feedback],
    }


@router.get("/feedback")
async def feedback_queue(
    authz: AuthzDep, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> dict[str, Any]:
    """Negative-feedback triage (FR-044).

    Returns the reason, the answer state, and the documents that were cited —
    everything needed to decide whether the corpus or the pipeline is at fault.
    The question and answer text are NOT returned: §6.5 restricts administrator
    access to conversations, and a triage queue is not an exemption.
    """
    require_knowledge_admin(authz)
    rows = await _rows(
        request,
        """
        SELECT f.id, f.created_at, f.rating::text AS rating,
               f.reason_code, f.comment, f.triaged_at, f.resolution,
               m.answer_state::text AS answer_state, m.groundedness,
               m.correlation_id::text AS correlation_id,
               coalesce(
                 (SELECT array_agg(DISTINCT d.title)
                    FROM citations c JOIN documents d ON d.id = c.document_id
                   WHERE c.message_id = m.id AND c.message_created_at = m.created_at),
                 ARRAY[]::text[]
               ) AS cited_documents
        FROM message_feedback f
        JOIN messages m ON m.id = f.message_id AND m.created_at = f.message_created_at
        WHERE f.rating = 'not_helpful'
        ORDER BY f.triaged_at IS NOT NULL, f.created_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return {"count": len(rows), "items": [dict(r) for r in rows]}


# ── operational metrics (FR-053) ────────────────────────────────────────────


@router.get("/metrics")
async def metrics(
    authz: AuthzDep, request: Request, days: Annotated[int, Query(ge=1, le=30)] = 1
) -> dict[str, Any]:
    """Availability, latency, error rate, and retrieval health in one view.

    Separate from /usage (what was consumed) and /quality (whether answers were
    good). This is the operational picture: is the platform healthy right now.
    """
    require_system_admin(authz)
    row = await _one(
        request,
        """
        SELECT
          count(*) FILTER (WHERE role = 'assistant')                         AS answers,
          count(*) FILTER (WHERE answer_state = 'error')                     AS errors,
          percentile_disc(0.50) WITHIN GROUP (ORDER BY ttft_ms)              AS ttft_p50,
          percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms)              AS ttft_p95,
          percentile_disc(0.50) WITHIN GROUP (ORDER BY total_ms)             AS total_p50,
          percentile_disc(0.95) WITHIN GROUP (ORDER BY total_ms)             AS total_p95,
          count(*) FILTER (WHERE cache_hit)                                  AS cache_hits
        FROM messages
        WHERE created_at > now() - make_interval(days => CAST(:days AS int))
        """,
        {"days": days},
    )
    retrieval = await _one(
        request,
        """
        SELECT percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
               count(*) FILTER (WHERE outcome <> 'success')             AS failures,
               count(*)                                                 AS calls
        FROM model_invocations
        WHERE operation IN ('embed_query', 'rerank')
          AND occurred_at > now() - make_interval(days => CAST(:days AS int))
        """,
        {"days": days},
    )
    answers = row["answers"] or 0
    return {
        "window_days": days,
        "availability": {
            "answers": answers,
            "errors": row["errors"] or 0,
            "error_rate": ((row["errors"] or 0) / answers) if answers else None,
        },
        "latency_ms": {
            "ttft_p50": row["ttft_p50"],
            "ttft_p95": row["ttft_p95"],
            "total_p50": row["total_p50"],
            "total_p95": row["total_p95"],
            "retrieval_p95": retrieval["p95"],
        },
        "retrieval": {
            "calls": retrieval["calls"] or 0,
            "failures": retrieval["failures"] or 0,
        },
        "cache": {"hits": row["cache_hits"] or 0},
        # The requirement names these targets; showing them beside the numbers
        # is what makes the page answerable at a glance rather than a data dump.
        "targets": {"ttft_p95_ms": 3000, "total_p95_ms": 10000, "error_rate": 0.01},
    }


class TriageIn(WireModel):
    resolution: str = Field(min_length=3, max_length=1000)


@router.patch("/feedback/{feedback_id}")
async def triage_feedback(
    feedback_id: int, body: TriageIn, authz: AuthzDep, request: Request
) -> dict[str, str]:
    """FR-044. Records who closed the item and what they did about it.

    Attribution is the point: a triage queue where nobody's name is attached is
    a queue nobody owns.
    """
    require_knowledge_admin(authz)
    async with request.app.state.engine.begin() as conn:
        result = await conn.execute(
            text("""
            UPDATE message_feedback
            SET triaged_at = now(), triaged_by = CAST(:uid AS uuid), resolution = :resolution
            WHERE id = :fid
            """),
            {"fid": feedback_id, "uid": str(authz.user_id), "resolution": body.resolution},
        )
        if not result.rowcount:
            raise NotFoundError("Feedback item not found")
    return {"id": str(feedback_id), "triaged": "ok"}


@router.post("/ingestion/runs/{run_id}/cancel")
async def cancel_run(run_id: str, authz: AuthzDep, request: Request) -> dict[str, str]:
    require_knowledge_admin(authz)
    async with request.app.state.engine.begin() as conn:
        result = await conn.execute(
            text("""
            UPDATE ingestion_runs SET status = 'cancelled', finished_at = now()
            WHERE id = CAST(:rid AS uuid) AND status = 'running'
            """),
            {"rid": run_id},
        )
    if not result.rowcount:
        raise NotFoundError("No running ingestion with that id")
    return {"run_id": run_id, "status": "cancelled"}


@router.get("/config")
async def read_config(authz: AuthzDep, request: Request) -> dict[str, Any]:
    """Effective non-secret configuration.

    Explicitly allow-listed rather than dumped: a settings object contains
    connection strings and signing keys, and "expose everything except what I
    remembered to exclude" is the wrong default for a governance product.
    """
    require_system_admin(authz)
    s = request.app.state.settings
    return {
        "environment": s.env,
        "auth_mode": s.auth_mode,
        "retriever": s.retriever,
        "reranker": s.reranker,
        "llm": {"provider": s.llm_provider, "model": s.llm_model},
        "embedder": {
            "provider": s.embedding_provider,
            "model": s.embedding_model,
            "dimensions": s.embedding_dim,
        },
        "retrieval": {
            "candidate_k": s.retrieval_candidate_k,
            "top_k": s.retrieval_top_k,
            "rrf_k": s.rrf_k,
            "hnsw_iterative_scan": s.hnsw_iterative_scan,
        },
        "thresholds": {
            "min_evidence_score": s.min_evidence_score,
            "min_evidence_chunks": s.min_evidence_chunks,
            "groundedness_floor": s.groundedness_floor,
            "context_token_budget": s.context_token_budget,
        },
        "limits": {
            "rate_per_minute": s.rate_limit_per_minute,
            "rate_per_hour": s.rate_limit_per_hour,
            "acl_sync_interval_seconds": s.acl_sync_interval_seconds,
        },
    }


def _ocr_status(settings: Settings) -> str:
    """One line an administrator can read without knowing what Tika is.

    Deliberately not a live probe: the overview is loaded often and a network
    call to a possibly-hung service would make the whole page hang with it.
    /health/deep does the probing; this reports intent, and points there.
    """
    if not settings.ocr_enabled:
        return "off — scanned documents are rejected, not indexed"

    from askau.ingestion.extractors.ocr import requested_languages

    languages = requested_languages(settings.ocr_languages)
    return (
        f"{settings.ocr_provider} service ({', '.join(languages)}) "
        "— see /health/deep for reachability"
    )
