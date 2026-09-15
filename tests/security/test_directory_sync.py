"""Entra group membership → `user_principals` (FR-002, FR-025).

This is the piece that was missing entirely: the token's `groups` claim was
parsed and consumed by nothing, so `user_principals` had exactly one writer —
the seed script. Against a real tenant nobody would have had any principals, and
an empty principal set raises by design, so live Entra could not have worked.

Two of the tests below are the ones that matter, and both fail in the direction
of a silent, total loss of access rather than a visible error:

* **A missing `groups` claim must change nothing.** Entra omits the claim once a
  user is in roughly 200+ groups. Reading absence as "no groups" strips every
  membership from the most heavily-permissioned people in the organisation, at
  sign-in, with no error anywhere.
* **A user's own principal is not a group and must survive.** It is what
  identifies them; removing it as part of a group reconcile would leave an
  account that can read nothing and looks correctly configured.

Everything here writes and reads the database directly. The question is what
`user_principals` holds afterwards, and the API is the one path that was never
in doubt.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.db.directory import DirectorySync
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

_PREFIX = "askau-dirtest-"


@pytest.fixture
async def person(admin_engine: AsyncEngine):  # type: ignore[no-untyped-def]
    """A user with their own `user`-kind principal and no group memberships.

    Built rather than borrowed from the seed, because these tests remove
    memberships and a shared fixture would leave the rest of the suite looking
    at an account whose access silently changed.
    """
    async with admin_engine.begin() as conn:
        principal_id = (
            await conn.execute(
                text("""
                INSERT INTO principals (kind, external_id, display_name)
                VALUES ('user', :ext, 'Directory test person')
                ON CONFLICT (kind, external_id) DO UPDATE SET display_name = excluded.display_name
                RETURNING id
                """),
                {"ext": f"{_PREFIX}self"},
            )
        ).scalar_one()
        user_id = str(
            (
                await conn.execute(
                    text("""
                    INSERT INTO users (entra_oid, email, display_name, department, principal_id)
                    VALUES (:oid, :email, 'Directory test person', 'MISD', :pid)
                    RETURNING id
                    """),
                    {
                        "oid": f"{_PREFIX}oid",
                        "email": f"{_PREFIX}person@africanunion.org",
                        "pid": principal_id,
                    },
                )
            ).scalar_one()
        )
        await conn.execute(
            text("""
            INSERT INTO user_principals (user_id, principal_id, granted_via)
            VALUES (CAST(:uid AS uuid), :pid, 'entra_sync')
            """),
            {"uid": user_id, "pid": principal_id},
        )

    yield user_id

    async with admin_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE entra_oid = :oid"), {"oid": f"{_PREFIX}oid"}
        )
        await conn.execute(
            text("DELETE FROM principals WHERE external_id LIKE :p"), {"p": f"{_PREFIX}%"}
        )


async def _memberships(engine: AsyncEngine, user_id: str) -> set[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("""
                SELECT p.external_id FROM user_principals up
                JOIN principals p ON p.id = up.principal_id
                WHERE up.user_id = CAST(:uid AS uuid)
                """),
                {"uid": user_id},
            )
        ).all()
    return {r[0] for r in rows}


async def _acl_version(engine: AsyncEngine, user_id: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT acl_version FROM users WHERE id = CAST(:uid AS uuid)"),
                    {"uid": user_id},
                )
            ).scalar_one()
        )


class TestTheClaimBecomesMembership:
    async def test_groups_in_the_token_become_principals(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        change = await DirectorySync(admin_engine).reconcile(
            person, (f"{_PREFIX}g1", f"{_PREFIX}g2")
        )
        assert set(change.added) == {f"{_PREFIX}g1", f"{_PREFIX}g2"}
        assert await _memberships(admin_engine, person) == {
            f"{_PREFIX}self",
            f"{_PREFIX}g1",
            f"{_PREFIX}g2",
        }

    async def test_a_group_never_seen_before_is_created(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """A group we have no row for is a normal state — one was created, or
        this is the first person in it. Dropping the membership because the row
        is missing would make access depend on which system synced first."""
        await DirectorySync(admin_engine).reconcile(person, (f"{_PREFIX}brand-new",))
        async with admin_engine.connect() as conn:
            kind = (
                await conn.execute(
                    text("SELECT kind::text FROM principals WHERE external_id = :e"),
                    {"e": f"{_PREFIX}brand-new"},
                )
            ).scalar_one()
        assert kind == "group"

    async def test_reconciling_twice_changes_nothing_the_second_time(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """Sign-in happens constantly. A sync that reported a change every time
        would bump `acl_version` on every sign-in and evict a cache that was
        correct."""
        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}g1",))
        version = await _acl_version(admin_engine, person)

        second = await sync.reconcile(person, (f"{_PREFIX}g1",))
        assert not second.changed
        assert await _acl_version(admin_engine, person) == version


class TestRemoval:
    async def test_a_group_left_in_entra_is_removed_here(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """The half an additive-only sync gets wrong.

        Somebody taken out of a group must lose what it could read. A sync that
        only ever adds never revokes, and nothing about the result looks wrong.
        """
        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}g1", f"{_PREFIX}g2"))

        change = await sync.reconcile(person, (f"{_PREFIX}g1",))
        assert change.removed == (f"{_PREFIX}g2",)
        assert f"{_PREFIX}g2" not in await _memberships(admin_engine, person)

    async def test_removal_bumps_the_cache_counter(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """`acl_version` keys the cached principal set in Redis.

        Rewriting the rows without bumping it leaves the old set being served
        until the entry expires — which for a removal is exactly the window this
        sync exists to close.
        """
        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}g1",))
        before = await _acl_version(admin_engine, person)

        await sync.reconcile(person, ())
        assert await _acl_version(admin_engine, person) > before

    async def test_the_users_own_principal_survives_every_reconcile(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """It is not a group and no group grants it.

        Removing it would leave an account that can read nothing while looking
        correctly configured — the failure would present as "retrieval is
        broken", nowhere near the cause.
        """
        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}g1",))
        await sync.reconcile(person, ())
        assert await _memberships(admin_engine, person) == {f"{_PREFIX}self"}


class TestAMissingClaimIsNotAnEmptyClaim:
    """The most dangerous case, and the least obvious.

    Entra omits `groups` entirely once a user belongs to more than roughly 200
    groups, sending `_claim_names` pointing at Graph instead. Treating that as
    "no groups" strips every membership from the most heavily-permissioned
    people in the organisation, silently, at sign-in.
    """

    async def test_absent_claim_leaves_membership_untouched(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}g1", f"{_PREFIX}g2"))
        before = await _memberships(admin_engine, person)

        change = await sync.reconcile(person, None)
        assert change.skipped_no_claim is True
        assert not change.changed
        assert await _memberships(admin_engine, person) == before

    async def test_an_empty_claim_does_remove(self, admin_engine: AsyncEngine, person: str) -> None:
        """The other side of the distinction. A token that says "in no groups"
        is telling us something, and it has to be acted on — otherwise the
        overflow guard becomes a way to never revoke anything."""
        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}g1",))

        change = await sync.reconcile(person, ())
        assert change.removed == (f"{_PREFIX}g1",)

    async def test_the_verifier_reports_absence_as_none(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Asserted at the parsing layer too, because this is where the
        distinction is created and where a well-meaning `claims.get("groups", ())`
        would erase it."""
        from askau.core.identity import _groups_claim

        assert _groups_claim({"groups": ["a", "b"]}) == ("a", "b")
        assert _groups_claim({"groups": []}) == ()
        assert _groups_claim({"sub": "x"}) is None
        # Entra's overflow marker: the real list is behind a Graph call.
        assert _groups_claim({"sub": "x", "_claim_names": {"groups": "src1"}}) is None


class TestScope:
    async def test_memberships_granted_another_way_are_left_alone(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """Only what this sync owns is reconciled.

        A future manual or role-based grant must not be silently removed because
        the directory does not mention it — `granted_via` is what keeps the two
        apart.
        """
        async with admin_engine.begin() as conn:
            pid = (
                await conn.execute(
                    text("""
                    INSERT INTO principals (kind, external_id, display_name)
                    VALUES ('group', :e, 'Granted by hand')
                    ON CONFLICT (kind, external_id) DO UPDATE
                      SET display_name = excluded.display_name
                    RETURNING id
                    """),
                    {"e": f"{_PREFIX}manual"},
                )
            ).scalar_one()
            await conn.execute(
                text("""
                INSERT INTO user_principals (user_id, principal_id, granted_via)
                VALUES (CAST(:uid AS uuid), :pid, 'manual')
                ON CONFLICT DO NOTHING
                """),
                {"uid": person, "pid": pid},
            )

        await DirectorySync(admin_engine).reconcile(person, (f"{_PREFIX}g1",))
        assert f"{_PREFIX}manual" in await _memberships(admin_engine, person)


class TestGroupsAreNotDuplicatedAcrossKinds:
    """A group the corpus already knows as a `department` must not be minted again.

    The uniqueness constraint is on `(kind, external_id)`, not on `external_id`
    alone, so an insert that always uses `kind='group'` creates a *second*
    principal for a group already recorded as a department. This is not
    hypothetical: the first version of this sync did exactly that, and `grp-hr`,
    `grp-finance` and `grp-legal` each ended up with two rows and the user a
    member of both.

    No access changed — the predicate intersects on ids and the person held both
    — which is what makes it the kind of defect that survives. The data is wrong,
    the principal counts are inflated, and the next reconcile has two rows
    competing for one external id.
    """

    async def test_an_existing_department_is_reused_not_duplicated(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO principals (kind, external_id, display_name)
                VALUES ('department', :e, 'Existing department')
                ON CONFLICT (kind, external_id) DO NOTHING
                """),
                {"e": f"{_PREFIX}dept"},
            )

        await DirectorySync(admin_engine).reconcile(person, (f"{_PREFIX}dept",))

        async with admin_engine.connect() as conn:
            kinds = [
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT kind::text FROM principals WHERE external_id = :e"),
                        {"e": f"{_PREFIX}dept"},
                    )
                ).all()
            ]
        assert kinds == ["department"], f"a second principal was created: {kinds}"
        assert f"{_PREFIX}dept" in await _memberships(admin_engine, person)

    async def test_the_membership_is_stable_across_repeated_sign_ins(
        self, admin_engine: AsyncEngine, person: str
    ) -> None:
        """The consequence of the above, stated as what a person would notice.

        With two rows for one external id the second reconcile compared a
        dictionary keyed by external id against principal ids, and one of the
        pair would be dropped — so the same sign-in, repeated, changed the
        access list.
        """
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO principals (kind, external_id, display_name)
                VALUES ('department', :e, 'Existing department')
                ON CONFLICT (kind, external_id) DO NOTHING
                """),
                {"e": f"{_PREFIX}dept"},
            )

        sync = DirectorySync(admin_engine)
        await sync.reconcile(person, (f"{_PREFIX}dept", f"{_PREFIX}g1"))
        first = await _memberships(admin_engine, person)

        second_change = await sync.reconcile(person, (f"{_PREFIX}dept", f"{_PREFIX}g1"))
        assert not second_change.changed
        assert await _memberships(admin_engine, person) == first
