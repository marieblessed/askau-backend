"""Authentication and identity."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.api.schemas.wire import MeOut
from askau.domain.enums import Classification

router = APIRouter(prefix="/auth", tags=["auth"])


#: The strictest document the caller's principals actually reach. Ordered by
#: the enum's own declaration order, which is ascending sensitivity — so the
#: last row is the answer. `ORDER BY … DESC LIMIT 1` rather than `max()`
#: because it reads as the question being asked and does not depend on an
#: aggregate being defined for the enum type.
_MAX_CLASSIFICATION = """
SELECT d.classification::text
FROM documents d
JOIN document_acl a ON a.document_id = d.id
WHERE a.principal_id = ANY(CAST(:principals AS bigint[]))
ORDER BY d.classification DESC
LIMIT 1
"""


@router.get("/me", response_model=MeOut)
async def me(authz: AuthzDep, request: Request) -> MeOut:
    """Who the caller is.

    Returns a *summary* of authorization, never the principal set. The client
    has no use for the raw set, and shipping it would put the access-control
    model on the wire.

    `max_classification` is derived, not declared. It was previously hardcoded
    to `internal`, which was wrong for anyone cleared above that and wrong in
    the direction that matters: the interface uses this to explain *why* an
    answer came back thin, and under-reporting turns "you cannot see that
    document" into an apparent gap in the corpus.
    """
    async with request.app.state.engine.connect() as conn:
        reached = (
            await conn.execute(text(_MAX_CLASSIFICATION), {"principals": authz.principal_array()})
        ).scalar_one_or_none()

    return MeOut(
        user_id=str(authz.user_id),
        email=authz.email,
        department=authz.department,
        roles=sorted(r.value for r in authz.roles),
        # A caller who reaches nothing is cleared to nothing beyond public —
        # not to `internal`, which is what the placeholder used to claim.
        max_classification=reached or Classification.PUBLIC.value,
        principal_count=len(authz.principals),
    )
