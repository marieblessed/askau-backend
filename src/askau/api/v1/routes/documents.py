"""Document access — metadata and the authoritative-source redirect.

FR-031 and BR-004. Two properties matter more than the mechanics:

* **Authorization is re-checked at click time.** The ``can_open`` flag the
  client received with the answer may be minutes old, and permissions change
  (FR-025). Trusting it would mean an answer rendered before a revocation stays
  clickable after it.
* **Unauthorized returns 404, never 403.** A 403 confirms the document exists.
  For confidential AUC material, existence is itself sensitive (ADR-0009). The
  audit row records the real outcome as ``denied`` even though the response says
  not-found, so an investigator sees what actually happened.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.errors import NotFoundError
from askau.domain.authz import AuthorizationContext
from askau.domain.enums import AuditOutcome

router = APIRouter(prefix="/v1/documents", tags=["documents"])

#: Authorization is the same intersection retrieval uses — against
#: ``document_acl``, the authoritative table, rather than the materialized
#: array on chunks. A document with no indexed chunks is still openable.
_LOOKUP = text("""
    SELECT d.id::text        AS id,
           d.title,
           d.source_uri,
           d.classification::text AS classification,
           d.version_label,
           d.lifecycle::text AS lifecycle,
           ks.name           AS source_name,
           EXISTS (
               SELECT 1 FROM document_acl acl
               WHERE acl.document_id = d.id
                 AND acl.principal_id = ANY(CAST(:principals AS bigint[]))
           )                 AS authorized
    FROM documents d
    JOIN knowledge_sources ks ON ks.id = d.source_id
    WHERE d.id = CAST(:document_id AS uuid)
""")


async def _load(request: Request, document_id: str, authz: AuthorizationContext):  # type: ignore[no-untyped-def]
    async with request.app.state.engine.connect() as conn:
        return (
            (
                await conn.execute(
                    _LOOKUP,
                    {"document_id": document_id, "principals": authz.principal_array()},
                )
            )
            .mappings()
            .first()
        )


def _deny(request: Request, authz: AuthorizationContext, document_id: str) -> NotFoundError:
    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.ACCESS_DENIED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="document",
            resource_id=document_id,
        )
    )
    return NotFoundError("Document not found")


@router.get("/{document_id}")
async def metadata(document_id: str, authz: AuthzDep, request: Request) -> dict[str, object]:
    row = await _load(request, document_id, authz)
    if row is None or not row["authorized"]:
        raise _deny(request, authz, document_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "classification": row["classification"],
        "version_label": row["version_label"],
        "lifecycle": row["lifecycle"],
        "source_name": row["source_name"],
        "source_uri": row["source_uri"],
    }


@router.get("/{document_id}/open")
async def open_document(document_id: str, authz: AuthzDep, request: Request) -> RedirectResponse:
    """Redirect to the authoritative original (BR-004).

    AskAU never serves the document itself: the authoritative copy stays in its
    source repository, and sending the reader there is what keeps AskAU from
    becoming the system of record (BR-003).
    """
    row = await _load(request, document_id, authz)
    if row is None or not row["authorized"]:
        raise _deny(request, authz, document_id)

    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.DOCUMENT_OPENED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="document",
            resource_id=document_id,
            detail={"classification": row["classification"]},
        )
    )
    # 302, not 301: the target can change when a document is re-synced, and a
    # permanently-cached redirect would outlive the permission check entirely.
    return RedirectResponse(url=str(row["source_uri"]), status_code=302)
