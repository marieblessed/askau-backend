"""Authentication and identity."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from askau.api.deps import AuthzDep
from askau.api.schemas.wire import DeletionOut, MeOut, PreferencesIn, PreferencesOut, UserOut
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.errors import NotFoundError
from askau.domain.authz import AuthorizationContext
from askau.domain.enums import AppRole, AuditOutcome, Classification

router = APIRouter(prefix="/v1/auth", tags=["auth"])

#: A second router so the path is `/api/v1/users/me` — the client asks for
#: `/users/me` against its own base URL, and that is a resource path rather than
#: an authentication one.
users_router = APIRouter(prefix="/v1/users", tags=["users"])


#: Our four roles collapsed into the client's three-tier vocabulary.
#:
#: This is display only. §6.4 separates knowledge, system and security
#: administration so that no single account can both change the corpus and erase
#: the record of having done so, and that separation stays server-side and keeps
#: being enforced by `require_*_admin`. What the interface needs is narrower:
#: whether to show an administrative affordance at all. Its own code annotates
#: every role check as UX-only with the backend authoritative, so collapsing
#: here costs nothing and inventing a fourth tier it does not understand would.
_ROLE_DISPLAY: dict[AppRole, str] = {
    AppRole.END_USER: "user",
    AppRole.KNOWLEDGE_ADMIN: "admin",
    AppRole.SYSTEM_ADMIN: "admin",
    AppRole.SECURITY_ADMIN: "admin",
}


def _display_roles(authz: AuthorizationContext) -> list[str]:
    """Ordered least- to most-privileged, because the client compares by index.

    `askau-frontend/lib/auth/permissions.ts` treats roles as an ordered
    hierarchy (`ROLE_HIERARCHY.indexOf(role) >= requiredIndex`), not as set
    membership, so an unrecognised string would silently rank below `user`.
    Only values it knows are emitted.
    """
    display = {_ROLE_DISPLAY[r] for r in authz.roles if r in _ROLE_DISPLAY}
    # Everyone who can reach this endpoint is at least a user; the client's
    # `hasRole([], "user")` returns false, so an empty list would lock them out
    # of their own interface.
    display.add("user")
    return [r for r in ("user", "admin", "super_admin") if r in display]


_PROFILE = """
SELECT u.id::text        AS id,
       u.email,
       u.display_name,
       u.entra_oid,
       u.department,
       u.job_title,
       u.preferred_language,
       u.created_at,
       u.last_login_at
FROM users u
WHERE u.id = CAST(:uid AS uuid)
"""


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


@users_router.get("/me", response_model=UserOut)
async def current_user(authz: AuthzDep, request: Request) -> UserOut:
    """The caller's profile, in the shape `askau-frontend/types/auth.ts` declares.

    This endpoint closes a live bug in that repo rather than merely feeding it:
    `lib/auth/config.ts` never populates `roles` on the NextAuth session, so
    `app/[locale]/admin/page.tsx` reads `?? ["user"]` and redirects *everyone* —
    real administrators included — to `/unauthorized`. Roles have to come from
    somewhere, and a backend that already resolves them is the right somewhere.
    """
    async with request.app.state.engine.connect() as conn:
        row = (await conn.execute(text(_PROFILE), {"uid": str(authz.user_id)})).mappings().first()
        reached = (
            await conn.execute(text(_MAX_CLASSIFICATION), {"principals": authz.principal_array()})
        ).scalar_one_or_none()

    if row is None:
        # The token verified but the user is gone — deactivated mid-session, or
        # a directory sync removed them. Not a 404: the caller is authenticated,
        # they simply no longer have a profile to return.
        raise NotFoundError("Profile not found")

    name = row["display_name"] or (row["email"] or "").split("@")[0]
    return UserOut(
        id=row["id"],
        email=row["email"] or "",
        name=name,
        display_name=name,
        entra_id=row["entra_oid"],
        roles=_display_roles(authz),
        department=row["department"],
        job_title=row["job_title"],
        language_preference=row["preferred_language"],
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
        last_login_at=row["last_login_at"].isoformat() if row["last_login_at"] else None,
        max_classification=reached or Classification.PUBLIC.value,
    )


def _conversations(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.conversations


@users_router.get("/me/preferences", response_model=PreferencesOut)
async def read_preferences(authz: AuthzDep, request: Request) -> PreferencesOut:
    p = await _conversations(request).preferences(str(authz.user_id))
    return PreferencesOut(
        save_history=p.save_history,
        share_analytics=p.share_analytics,
        higher_intelligence=p.higher_intelligence,
    )


@users_router.patch("/me/preferences", response_model=PreferencesOut)
async def update_preferences(
    body: PreferencesIn, authz: AuthzDep, request: Request
) -> PreferencesOut:
    """Change one or both. Omitted fields keep their value.

    Audited: turning off history is a privacy decision, and a record that it was
    made — by whom and when — is the thing that lets somebody answer "why does
    this account have no conversations" a year later without guessing.
    """
    repo = _conversations(request)
    await repo.set_preferences(
        str(authz.user_id),
        save_history=body.save_history,
        share_analytics=body.share_analytics,
        higher_intelligence=body.higher_intelligence,
    )
    p = await repo.preferences(str(authz.user_id))

    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.CONFIG_CHANGED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="user_preferences",
            resource_id=str(authz.user_id),
            detail={
                "save_history": p.save_history,
                "share_analytics": p.share_analytics,
                "higher_intelligence": p.higher_intelligence,
            },
        )
    )
    return PreferencesOut(
        save_history=p.save_history,
        share_analytics=p.share_analytics,
        higher_intelligence=p.higher_intelligence,
    )


@users_router.delete("/me/history", response_model=DeletionOut)
async def delete_history(authz: AuthzDep, request: Request) -> DeletionOut:
    """Delete every conversation this person owns (their "Delete all history").

    Scoped to the caller by the same ownership predicate as every other read —
    "all" means all of *mine*. Messages, citations and feedback go with the
    conversations by cascade.

    The deletion is audited even though its subject is being deleted. That is
    not a contradiction: the audit record says an account erased its history, not
    what the history said, and BR-008 makes it append-only precisely so that
    "the record is gone" cannot itself be made to disappear.
    """
    deleted = await _conversations(request).delete_all_for(str(authz.user_id))
    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.CONVERSATION_DELETED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="conversation",
            resource_id="*",
            detail={"deleted": deleted},
        )
    )
    return DeletionOut(deleted=deleted)
