"""The knowledge-base registry the settings modal shows (§5.4).

Read-only, and it stays that way. Their UI presents these as status pills
rather than toggles, which is correct: switching a source on is BR-001's
approval act and belongs to a knowledge administrator, not to a reader's
preferences. The admin surface for that already exists at
`/api/v1/knowledge/sources`.

Two things this endpoint does that the obvious version would not.

**The counts are the caller's counts.** An unscoped `count(*)` would tell
someone "247 documents" when three are reachable by them, which is both
misleading and a disclosure: the size of a repository a person cannot read is
information about it. The count runs through the same `document_acl`
intersection retrieval uses.

**A source the caller can reach nothing in is absent, not empty.** Listing
"Executive Council Records — 0 documents" confirms that repository exists and
that they are shut out of it. Absence says neither.

Note what is *not* returned: a version. Their UI renders `"v2.6"` beside each
name and `knowledge_sources` has no such column — a revision concept was never
built. Inventing a number here would be worse than the gap it papers over, so
`lastSyncedAt` is returned instead and the mismatch goes back to their team.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.api.schemas.wire import KnowledgeBaseOut, ListResponse

router = APIRouter(prefix="/v1/knowledge-bases", tags=["knowledge"])

#: Only `active` sources. A draft or suspended source is not something a reader
#: can be answered from, so listing it would describe a capability that is not
#: there.
_LIST = text("""
    SELECT ks.id::text                       AS id,
           ks.name,
           ks.department,
           ks.source_type::text              AS source_type,
           ks.status::text                   AS status,
           ks.last_sync_at,
           ks.last_sync_status,
           count(DISTINCT d.id)              AS document_count
    FROM knowledge_sources ks
    JOIN documents d
      ON d.source_id = ks.id
     AND d.lifecycle <> 'draft'
     AND EXISTS (
             SELECT 1 FROM document_acl acl
             WHERE acl.document_id = d.id
               AND acl.principal_id = ANY(CAST(:principals AS bigint[]))
         )
    WHERE ks.status = 'active'
    GROUP BY ks.id, ks.name, ks.department, ks.source_type, ks.status,
             ks.last_sync_at, ks.last_sync_status
    ORDER BY ks.name
""")


@router.get("", response_model=ListResponse[KnowledgeBaseOut])
async def list_knowledge_bases(authz: AuthzDep, request: Request) -> ListResponse[KnowledgeBaseOut]:
    """What this reader can be answered from.

    An inner join rather than a left join, which is the whole authorization
    story in one word: a source contributes a row only when at least one
    document in it survives the ACL intersection.
    """
    async with request.app.state.engine.connect() as conn:
        rows = (await conn.execute(_LIST, {"principals": authz.principal_array()})).mappings().all()

    items = [
        KnowledgeBaseOut(
            id=r["id"],
            name=r["name"],
            department=r["department"],
            source_type=r["source_type"],
            status=r["status"],
            document_count=r["document_count"],
            last_synced_at=r["last_sync_at"].isoformat() if r["last_sync_at"] else None,
            last_sync_status=r["last_sync_status"],
        )
        for r in rows
    ]
    # Not paginated. The AUC has a handful of repositories, not a corpus of
    # them, and an envelope that pages over four rows is ceremony. The
    # `ListResponse` shape is kept because every other collection uses it and
    # their client has one parser.
    return ListResponse[KnowledgeBaseOut].of(
        items, total=len(items), page=1, page_size=max(len(items), 1)
    )
