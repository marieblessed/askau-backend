"""Knowledge management — the write surface (FR-011 … FR-013, FR-046 … FR-050).

Read views live in `admin.py`; everything that changes state lives here, so the
audit story is legible in one file: every mutation records who did it.

BR-001 is enforced twice on purpose. `approve` is the workflow, and the
`active_requires_approval` CHECK constraint is what holds if a future code path
forgets to call it. A business rule that maps to a constraint belongs in a
constraint.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.errors import ConflictError, InvalidRequestError, NotFoundError
from askau.core.rbac import require_knowledge_admin
from askau.domain.enums import AuditOutcome

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


class SourceIn(BaseModel):
    """FR-011. Governance metadata is required, not optional — an unowned source
    has nobody accountable for its content (BR-002)."""

    name: str = Field(min_length=3, max_length=200)
    source_type: Literal["sharepoint", "dms", "filesystem", "s3", "http", "manual"]
    department: str = Field(min_length=2, max_length=120)
    business_owner_email: str
    default_classification: Literal["public", "internal", "confidential", "highly_restricted"]
    description: str | None = Field(default=None, max_length=2000)
    location: dict[str, Any] = Field(default_factory=dict)
    sync_cron: str | None = None


class SourcePatch(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    sync_cron: str | None = None
    status: Literal["draft", "active", "paused", "archived"] | None = None
    default_classification: (
        Literal["public", "internal", "confidential", "highly_restricted"] | None
    ) = None


class DocumentPatch(BaseModel):
    """FR-013 correction path: metadata fixed by an administrator after ingest."""

    classification: Literal["public", "internal", "confidential", "highly_restricted"] | None = None
    lifecycle: Literal["draft", "active", "review_required", "expired", "superseded"] | None = None
    review_required: bool | None = None
    doc_type: str | None = Field(default=None, max_length=80)


def _audit(
    request: Request, authz: AuthzDep, event: EventType, kind: str, rid: str, **detail: Any
) -> None:
    request.app.state.audit.record(
        AuditEvent(
            event_type=event,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type=kind,
            resource_id=rid,
            detail=detail,
        )
    )


# ── sources ─────────────────────────────────────────────────────────────────


@router.get("/sources")
async def list_sources(authz: AuthzDep, request: Request) -> dict[str, Any]:
    require_knowledge_admin(authz)
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                SELECT ks.id::text, ks.name, ks.source_type::text AS source_type,
                       ks.description, ks.department, ks.status::text AS status,
                       ks.default_classification::text AS default_classification,
                       ks.location, ks.sync_cron, ks.last_sync_at, ks.next_sync_at,
                       ks.approved_by IS NOT NULL AS approved,
                       u.display_name AS business_owner, u.email AS business_owner_email,
                       (SELECT count(*) FROM documents d WHERE d.source_id = ks.id) AS documents
                FROM knowledge_sources ks
                JOIN users u ON u.id = ks.business_owner_id
                ORDER BY ks.name
                """)
                )
            )
            .mappings()
            .all()
        )
    return {"count": len(rows), "sources": [dict(r) for r in rows]}


@router.get("/sources/{source_id}")
async def read_source(source_id: str, authz: AuthzDep, request: Request) -> dict[str, Any]:
    require_knowledge_admin(authz)
    async with request.app.state.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("""
                SELECT ks.id::text, ks.name, ks.source_type::text AS source_type,
                       ks.description, ks.department, ks.status::text AS status,
                       ks.default_classification::text AS default_classification,
                       ks.location, ks.access_rules, ks.sync_cron,
                       ks.approved_at, ks.last_sync_at, ks.last_sync_status,
                       u.email AS business_owner_email
                FROM knowledge_sources ks
                JOIN users u ON u.id = ks.business_owner_id
                WHERE ks.id = CAST(:sid AS uuid)
                """),
                    {"sid": source_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError("Source not found")
        runs = (
            (
                await conn.execute(
                    text("""
                SELECT id::text, status, trigger, docs_processed, docs_failed,
                       chunks_written, started_at, finished_at
                FROM ingestion_runs WHERE source_id = CAST(:sid AS uuid)
                ORDER BY started_at DESC LIMIT 10
                """),
                    {"sid": source_id},
                )
            )
            .mappings()
            .all()
        )
    return {**dict(row), "recent_runs": [dict(r) for r in runs]}


@router.post("/sources", status_code=201)
async def create_source(body: SourceIn, authz: AuthzDep, request: Request) -> dict[str, str]:
    require_knowledge_admin(authz)
    async with request.app.state.engine.begin() as conn:
        owner = (
            await conn.execute(
                text("SELECT id FROM users WHERE lower(email) = lower(:e)"),
                {"e": body.business_owner_email},
            )
        ).scalar_one_or_none()
        if owner is None:
            raise InvalidRequestError(
                f"No AskAU user with email {body.business_owner_email}. "
                "Every source needs an accountable owner (BR-002)."
            )
        exists = (
            await conn.execute(
                text("SELECT 1 FROM knowledge_sources WHERE name = :n"), {"n": body.name}
            )
        ).scalar_one_or_none()
        if exists:
            raise ConflictError(f"A source named {body.name!r} already exists")

        import json

        # Created as `draft`, never `active`. Activation requires approval, and
        # the database rejects the alternative anyway.
        source_id = str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO knowledge_sources
                        (name, source_type, description, business_owner_id, department,
                         default_classification, location, sync_cron, status)
                    VALUES (:name, CAST(:stype AS source_type), :description, :owner,
                            :department, CAST(:cls AS classification),
                            CAST(:location AS jsonb), :cron, 'draft')
                    RETURNING id
                    """),
                    {
                        "name": body.name,
                        "stype": body.source_type,
                        "description": body.description,
                        "owner": owner,
                        "department": body.department,
                        "cls": body.default_classification,
                        "location": json.dumps(body.location),
                        "cron": body.sync_cron,
                    },
                )
            ).scalar_one()
        )
    _audit(request, authz, EventType.SOURCE_CREATED, "knowledge_source", source_id, name=body.name)
    return {"id": source_id, "status": "draft"}


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: str, body: SourcePatch, authz: AuthzDep, request: Request
) -> dict[str, str]:
    require_knowledge_admin(authz)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise InvalidRequestError("No changes supplied")
    if fields.get("status") == "active":
        raise InvalidRequestError(
            "Use POST /sources/{id}/approve to activate a source. Activation "
            "records an approver, and the database refuses an active source without one."
        )

    sets, params = [], {"sid": source_id}
    for key, value in fields.items():
        if key == "status":
            sets.append("status = CAST(:status AS source_status)")
        elif key == "default_classification":
            sets.append("default_classification = CAST(:default_classification AS classification)")
        else:
            sets.append(f"{key} = :{key}")
        params[key] = value
    sets.append("updated_at = now()")

    async with request.app.state.engine.begin() as conn:
        result = await conn.execute(
            text(f"UPDATE knowledge_sources SET {', '.join(sets)} WHERE id = CAST(:sid AS uuid)"),  # noqa: S608
            params,
        )
        if not result.rowcount:
            raise NotFoundError("Source not found")
    _audit(
        request,
        authz,
        EventType.CONFIG_CHANGED,
        "knowledge_source",
        source_id,
        changed=sorted(fields),
    )
    return {"id": source_id, "updated": "ok"}


@router.post("/sources/{source_id}/approve")
async def approve_source(source_id: str, authz: AuthzDep, request: Request) -> dict[str, str]:
    """BR-001. Approval and activation are the same act, recorded together."""
    require_knowledge_admin(authz)
    async with request.app.state.engine.begin() as conn:
        result = await conn.execute(
            text("""
            UPDATE knowledge_sources
            SET approved_by = CAST(:uid AS uuid), approved_at = now(),
                status = 'active', updated_at = now()
            WHERE id = CAST(:sid AS uuid)
            RETURNING name
            """),
            {"sid": source_id, "uid": str(authz.user_id)},
        )
        row = result.first()
        if row is None:
            raise NotFoundError("Source not found")
    _audit(request, authz, EventType.SOURCE_APPROVED, "knowledge_source", source_id, name=row[0])
    return {"id": source_id, "status": "active"}


async def _start_run(request: Request, source_id: str, authz: AuthzDep, trigger: str) -> str:
    async with request.app.state.engine.begin() as conn:
        source = (
            (
                await conn.execute(
                    text("""
                SELECT name, status::text AS status, approved_by IS NOT NULL AS approved
                FROM knowledge_sources WHERE id = CAST(:sid AS uuid)
                """),
                    {"sid": source_id},
                )
            )
            .mappings()
            .first()
        )
        if source is None:
            raise NotFoundError("Source not found")
        if not source["approved"]:
            # BR-001 at the workflow layer. The constraint would also stop this,
            # but an administrator deserves the reason rather than a 500.
            raise ConflictError(
                f"{source['name']!r} has not been approved. Only approved sources "
                "may be indexed (BR-001)."
            )
        running = (
            await conn.execute(
                text("""
                SELECT id FROM ingestion_runs
                WHERE source_id = CAST(:sid AS uuid) AND status = 'running'
                """),
                {"sid": source_id},
            )
        ).scalar_one_or_none()
        if running:
            raise ConflictError(f"A run is already in progress for this source ({running})")

        run_id = str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO ingestion_runs (source_id, trigger, triggered_by, status)
                    VALUES (CAST(:sid AS uuid), :trigger, CAST(:uid AS uuid), 'running')
                    RETURNING id
                    """),
                    {"sid": source_id, "trigger": trigger, "uid": str(authz.user_id)},
                )
            ).scalar_one()
        )
    return run_id


@router.post("/sources/{source_id}/sync", status_code=202)
async def sync_source(source_id: str, authz: AuthzDep, request: Request) -> dict[str, str]:
    """FR-049. Returns 202 with a run id — ingestion is minutes-scale work, and
    a synchronous response would be a lie."""
    require_knowledge_admin(authz)
    run_id = await _start_run(request, source_id, authz, "manual")
    _audit(
        request,
        authz,
        EventType.REINDEX_TRIGGERED,
        "knowledge_source",
        source_id,
        run_id=run_id,
        mode="sync",
    )
    return {"run_id": run_id, "status": "running", "location": f"/v1/admin/ingestion/runs/{run_id}"}


@router.post("/sources/{source_id}/reindex", status_code=202)
async def reindex_source(source_id: str, authz: AuthzDep, request: Request) -> dict[str, str]:
    """FR-049. Full re-chunk and re-embed, not an incremental sync."""
    require_knowledge_admin(authz)
    run_id = await _start_run(request, source_id, authz, "reindex")
    _audit(
        request,
        authz,
        EventType.REINDEX_TRIGGERED,
        "knowledge_source",
        source_id,
        run_id=run_id,
        mode="reindex",
    )
    return {"run_id": run_id, "status": "running", "location": f"/v1/admin/ingestion/runs/{run_id}"}


@router.post("/sources/{source_id}/test-connection")
async def test_connection(source_id: str, authz: AuthzDep, request: Request) -> dict[str, Any]:
    """FR-013. Validate reachability before activating, so a misconfigured
    source fails here rather than as a run full of errors."""
    require_knowledge_admin(authz)
    async with request.app.state.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("""
                SELECT source_type::text AS source_type, location
                FROM knowledge_sources WHERE id = CAST(:sid AS uuid)
                """),
                    {"sid": source_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFoundError("Source not found")

    from askau.ingestion.connectors import probe

    result = await probe(row["source_type"], dict(row["location"] or {}))
    return {"source_id": source_id, **result}


# ── documents ───────────────────────────────────────────────────────────────


@router.get("/documents/{document_id}/versions")
async def document_versions(document_id: str, authz: AuthzDep, request: Request) -> dict[str, Any]:
    """FR-017. The whole version chain, so an administrator can see which
    revision retrieval will actually use."""
    require_knowledge_admin(authz)
    async with request.app.state.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("""
                SELECT d.id::text, d.version_seq, d.version_label,
                       d.lifecycle::text AS lifecycle, d.effective_from, d.effective_to,
                       d.chunk_count, d.indexed_at,
                       (df.current_document_id = d.id) AS is_current
                FROM documents d
                JOIN document_families df ON df.id = d.family_id
                WHERE d.family_id = (
                    SELECT family_id FROM documents WHERE id = CAST(:did AS uuid)
                )
                ORDER BY d.version_seq DESC
                """),
                    {"did": document_id},
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        raise NotFoundError("Document not found")
    return {"count": len(rows), "versions": [dict(r) for r in rows]}


@router.patch("/documents/{document_id}")
async def update_document(
    document_id: str, body: DocumentPatch, authz: AuthzDep, request: Request
) -> dict[str, str]:
    require_knowledge_admin(authz)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise InvalidRequestError("No changes supplied")

    sets, params = [], {"did": document_id}
    for key, value in fields.items():
        if key == "classification":
            sets.append("classification = CAST(:classification AS classification)")
        elif key == "lifecycle":
            sets.append("lifecycle = CAST(:lifecycle AS lifecycle_status)")
        else:
            sets.append(f"{key} = :{key}")
        params[key] = value
    sets.append("updated_at = now()")

    async with request.app.state.engine.begin() as conn:
        result = await conn.execute(
            text(f"UPDATE documents SET {', '.join(sets)} WHERE id = CAST(:did AS uuid)"),  # noqa: S608
            params,
        )
        if not result.rowcount:
            raise NotFoundError("Document not found")

        # Reclassification and lifecycle changes must reach the chunks, or
        # retrieval keeps using the old values — the denormalization's standing
        # obligation (ADR-0002).
        if "classification" in fields or "lifecycle" in fields:
            await conn.execute(
                text("""
                UPDATE chunks SET lifecycle = d.lifecycle, acl_synced_at = now()
                FROM documents d
                WHERE chunks.document_id = d.id AND d.id = CAST(:did AS uuid)
                """),
                {"did": document_id},
            )

    _audit(
        request, authz, EventType.CONFIG_CHANGED, "document", document_id, changed=sorted(fields)
    )
    return {"id": document_id, "updated": "ok"}


@router.post("/documents/{document_id}/reprocess", status_code=202)
async def reprocess_document(document_id: str, authz: AuthzDep, request: Request) -> dict[str, str]:
    """FR-049/050. Retry one document rather than a whole source.

    Idempotent: content hashing means an unchanged document costs one comparison.
    """
    require_knowledge_admin(authz)
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT source_id::text FROM documents WHERE id = CAST(:did AS uuid)"),
                {"did": document_id},
            )
        ).first()
        if row is None:
            raise NotFoundError("Document not found")
        await conn.execute(
            text("""
            UPDATE documents SET ingest_status = 'pending', ingest_error = NULL,
                                 updated_at = now()
            WHERE id = CAST(:did AS uuid)
            """),
            {"did": document_id},
        )
    _audit(request, authz, EventType.REINDEX_TRIGGERED, "document", document_id)
    return {"id": document_id, "status": "pending"}


@router.get("/documents/{document_id}/preview")
async def preview_document(
    document_id: str,
    authz: AuthzDep,
    request: Request,
    chunk_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """FR-029. The extracted text behind a citation.

    Permission-checked like every other document read: the caller's principals
    must intersect the document's access list.
    """
    async with request.app.state.engine.connect() as conn:
        allowed = (
            await conn.execute(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM document_acl
                    WHERE document_id = CAST(:did AS uuid)
                      AND principal_id = ANY(CAST(:principals AS bigint[]))
                )
                """),
                {"did": document_id, "principals": authz.principal_array()},
            )
        ).scalar_one()
        if not allowed:
            raise NotFoundError("Document not found")

        rows = (
            (
                await conn.execute(
                    text("""
                SELECT id, ordinal, content, heading_path, section_ref, page_from, page_to
                FROM chunks
                WHERE document_id = CAST(:did AS uuid)
                  AND (CAST(:chunk AS bigint) IS NULL OR id = CAST(:chunk AS bigint))
                ORDER BY ordinal
                LIMIT 20
                """),
                    {"did": document_id, "chunk": chunk_id},
                )
            )
            .mappings()
            .all()
        )
    return {"document_id": document_id, "chunks": [dict(r) for r in rows]}
