"""Entra group membership → `user_principals` (FR-002, FR-025).

The authorization predicate intersects `chunks.acl_principals` with the caller's
principal set, and that set comes from `user_principals`. Until now the only
thing that ever wrote it was the seed script: the access token's `groups` claim
was parsed into `VerifiedIdentity.groups` and then consumed by nothing.

The consequence was not a subtle one. Against a real tenant a signed-in person
either had no AskAU account at all, or had one with no principals — and an empty
principal set raises loudly by design. Live Entra could not work, and no test
tenant would have shown anything else.

## What this does, and the three parts that matter

**Removal, not just addition.** Somebody taken out of a group in Entra must lose
what that group could read, on their next sign-in. An additive-only sync is the
same defect as an access list that only ever grows — it never revokes, and the
symptom is invisible because everything looks present and correct.

**A missing claim is not an empty claim.** Entra omits `groups` entirely when a
user belongs to more than roughly 200 groups, sending `_claim_names` pointing at
Graph instead. Reading that absence as "this person is in no groups" would strip
every membership from exactly the most heavily-permissioned people in the
organisation, silently, at sign-in. So absence is distinguished from emptiness
and absence changes nothing.

**Only directory-sourced group memberships are touched.** Each user also holds a
`user`-kind principal — their own identity, which no group grants and nothing
here may remove. Scoped by `principals.kind`, so the self principal is excluded
by construction rather than by remembering to exclude it.

## What it deliberately does not do

It does not create users. A valid organisational token is not an AskAU account:
onboarding assigns a department and an access posture, and inventing those from
a token would produce a user whose authorization nobody decided. An unknown
identity is still refused at `POST /auth/session`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_log = logging.getLogger(__name__)

#: Principal kinds a directory group can grant. `user` is excluded on purpose —
#: that is the person's own principal and is not a group membership.
_GROUP_KINDS = ("group", "department")

#: Marks memberships this sync owns. Anything granted another way is left alone,
#: so a future manual or role-based grant is not silently reconciled away.
_SOURCE = "entra_sync"


@dataclass(frozen=True, slots=True)
class MembershipChange:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: True when the token carried no `groups` claim at all, so nothing was
    #: reconciled. Surfaced rather than hidden: it is the difference between
    #: "this person is in no groups" and "we were not told", and an operator
    #: seeing repeated skips is seeing the Graph-overage case.
    skipped_no_claim: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


class DirectorySync:
    """Reconciles one user's group memberships from their token's claims."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def reconcile(self, user_id: str, groups: tuple[str, ...] | None) -> MembershipChange:
        """Bring `user_principals` into agreement with the token's groups.

        `groups=None` means the claim was absent and nothing is changed. An
        empty tuple means the claim was present and empty — the person really
        is in no groups — and their group memberships are removed.
        """
        if groups is None:
            _log.info("no groups claim for user %s; membership left unchanged", user_id)
            return MembershipChange(skipped_no_claim=True)

        wanted = {g.strip() for g in groups if g and g.strip()}

        async with self._engine.begin() as conn:
            # Resolve each group named by the token to a principal id, creating
            # one only when no principal already carries that external id.
            #
            # The uniqueness constraint is on `(kind, external_id)`, not on
            # `external_id` alone, so blindly inserting `kind='group'` creates a
            # *second* principal for a group the corpus already knows as a
            # `department`. It happened: `grp-hr`, `grp-finance` and `grp-legal`
            # each ended up with two rows and the user a member of both. No
            # access changed — the predicate intersects on ids and the user held
            # both — but the data was wrong, the counts inflated, and the next
            # reconcile would have had two rows competing for one external id.
            #
            # So: look first across every kind a group can be, and only mint a
            # `group` when the directory really is naming something new.
            resolved: dict[str, int] = {}
            for external_id in sorted(wanted):
                existing = (
                    await conn.execute(
                        text("""
                        SELECT id FROM principals
                        WHERE external_id = :ext AND kind = ANY(CAST(:kinds AS principal_kind[]))
                        ORDER BY id
                        LIMIT 1
                        """),
                        {"ext": external_id, "kinds": list(_GROUP_KINDS)},
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = (
                        await conn.execute(
                            text("""
                            INSERT INTO principals (kind, external_id, display_name)
                            VALUES ('group', :ext, :ext)
                            ON CONFLICT (kind, external_id)
                              DO UPDATE SET synced_at = now()
                            RETURNING id
                            """),
                            {"ext": external_id},
                        )
                    ).scalar_one()
                else:
                    await conn.execute(
                        text("UPDATE principals SET synced_at = now() WHERE id = :pid"),
                        {"pid": existing},
                    )
                resolved[external_id] = int(existing)

            current: dict[str, int] = {
                row[0]: row[1]
                for row in (
                    await conn.execute(
                        text("""
                        SELECT p.external_id, p.id
                        FROM user_principals up
                        JOIN principals p ON p.id = up.principal_id
                        WHERE up.user_id = CAST(:uid AS uuid)
                          AND up.granted_via = :source
                          AND p.kind = ANY(CAST(:kinds AS principal_kind[]))
                        """),
                        {"uid": user_id, "source": _SOURCE, "kinds": list(_GROUP_KINDS)},
                    )
                ).all()
            }

            # Compared by principal id, not by external id. Ids are what
            # `user_principals` holds and what the predicate intersects, and a
            # corpus that already contains two rows for one external id would
            # otherwise have them collapse into a single dictionary key and one
            # of them silently dropped.
            wanted_ids = set(resolved.values())
            current_ids = set(current.values())
            added = sorted(e for e, pid in resolved.items() if pid not in current_ids)
            removed = sorted(e for e, pid in current.items() if pid not in wanted_ids)

            for external_id in added:
                await conn.execute(
                    text("""
                    INSERT INTO user_principals (user_id, principal_id, granted_via)
                    VALUES (CAST(:uid AS uuid), :pid, :source)
                    ON CONFLICT (user_id, principal_id) DO NOTHING
                    """),
                    {"uid": user_id, "pid": resolved[external_id], "source": _SOURCE},
                )

            if removed:
                await conn.execute(
                    text("""
                    DELETE FROM user_principals
                    WHERE user_id = CAST(:uid AS uuid)
                      AND granted_via = :source
                      AND principal_id = ANY(CAST(:ids AS bigint[]))
                    """),
                    {
                        "uid": user_id,
                        "source": _SOURCE,
                        "ids": [current[e] for e in removed],
                    },
                )

            if added or removed:
                # The resolver caches a principal set in Redis under this
                # counter. Rewriting the rows without bumping it would leave the
                # old set being served until the entry expired — which for a
                # removal is precisely the window this sync exists to close.
                await conn.execute(
                    text("""
                    UPDATE users SET acl_version = acl_version + 1
                    WHERE id = CAST(:uid AS uuid)
                    """),
                    {"uid": user_id},
                )

        if added or removed:
            _log.info("membership reconciled for %s: +%d -%d", user_id, len(added), len(removed))
        return MembershipChange(added=tuple(added), removed=tuple(removed))
