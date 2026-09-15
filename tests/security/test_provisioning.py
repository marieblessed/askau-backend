"""Creating an AskAU account (`askau.scripts.provision_user`).

Provisioning writes authorization state — a principal, a membership and an
application role — so it is tested here rather than beside the other scripts.

The two tests that carry weight are the ones about interaction with things that
run *later*, both of which fail as a silent loss of access:

* **The self principal must survive `DirectorySync`.** The sync deletes every
  `entra_sync` membership the token does not re-assert, and the token never
  asserts a person's own principal. Get this wrong and an account works
  perfectly until its owner's first sign-in, then can read nothing that was
  granted to them by name.

  Mutation testing changed what this test claims. Flipping the grant to
  `entra_sync` did *not* break it: `DirectorySync` also filters by
  `principals.kind`, and a `user`-kind principal is out of scope regardless of
  how it was granted. The two guards are genuinely independent — only removing
  both loses the principal, which is what the second assertion below now
  pins.
* **The principal must be keyed on the object id.** The Azure Blob connector
  writes Entra object ids into `document_acl` for grants to an individual. A friendly
  key here would make a second principal for the same person, and every
  individual grant in the corpus would match an identity nobody holds.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.db.directory import DirectorySync
from askau.scripts.provision_user import provision
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

_OID = "askau-prov-11111111-2222-3333-4444-555555555555"
_EMAIL = "askau-prov-tester@africanunion.org"


@pytest.fixture(autouse=True)
async def _clean(admin_engine: AsyncEngine):  # type: ignore[no-untyped-def]
    async def wipe() -> None:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE entra_oid LIKE 'askau-prov-%'"),
            )
            await conn.execute(
                text("DELETE FROM principals WHERE external_id LIKE 'askau-prov-%'"),
            )

    await wipe()
    yield
    await wipe()


async def _row(engine: AsyncEngine) -> tuple[str, int, str | None]:
    async with engine.connect() as conn:
        r = (
            await conn.execute(
                text("""
                SELECT u.id::text, u.principal_id, u.department
                FROM users u WHERE u.entra_oid = :oid
                """),
                {"oid": _OID},
            )
        ).one()
    return str(r[0]), int(r[1]), r[2]


async def _roles(engine: AsyncEngine, user_id: str) -> set[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT role FROM app_role_assignments WHERE user_id = CAST(:u AS uuid)"),
                {"u": user_id},
            )
        ).all()
    return {r[0] for r in rows}


def _args(**over: str) -> list[str]:
    base = {"--oid": _OID, "--email": _EMAIL, "--name": "Provisioning Tester"}
    base.update(over)
    out: list[str] = []
    for k, v in base.items():
        out += [k, v]
    return out


class TestTheAccountItCreates:
    async def test_the_principal_is_keyed_on_the_entra_object_id(
        self, admin_engine: AsyncEngine
    ) -> None:
        assert await provision(_args()) == 0
        _, principal_id, _ = await _row(admin_engine)
        async with admin_engine.connect() as conn:
            kind, external = (
                await conn.execute(
                    text("SELECT kind, external_id FROM principals WHERE id = :p"),
                    {"p": principal_id},
                )
            ).one()
        assert (kind, external) == ("user", _OID)

    async def test_an_existing_principal_is_reused_not_duplicated(
        self, admin_engine: AsyncEngine
    ) -> None:
        """Ingestion can get here first: a blob granted to this person mints
        their principal before anyone provisions an account."""
        async with admin_engine.begin() as conn:
            already = (
                await conn.execute(
                    text("""
                    INSERT INTO principals (kind, external_id, display_name)
                    VALUES ('user', :ext, 'From ingestion') RETURNING id
                    """),
                    {"ext": _OID},
                )
            ).scalar_one()

        assert await provision(_args()) == 0
        _, principal_id, _ = await _row(admin_engine)
        assert principal_id == already

        async with admin_engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM principals WHERE external_id = :e"),
                    {"e": _OID},
                )
            ).scalar_one()
        assert count == 1

    async def test_roles_default_to_end_user_and_extra_roles_are_granted(
        self, admin_engine: AsyncEngine
    ) -> None:
        assert await provision(_args()) == 0
        user_id, _, _ = await _row(admin_engine)
        assert await _roles(admin_engine, user_id) == {"end_user"}

        assert await provision([*_args(), "--role", "knowledge_admin"]) == 0
        assert await _roles(admin_engine, user_id) == {"end_user", "knowledge_admin"}


class TestRerunningIt:
    async def test_a_second_run_does_not_revoke_roles_or_lose_a_department(
        self, admin_engine: AsyncEngine
    ) -> None:
        """Re-running with a shorter `--role` list is a re-run, not a
        revocation. Revoking authority is a deliberate act and must not be a
        side effect of retyping a command."""
        assert await provision([*_args(), "--department", "Finance", "--role", "system_admin"]) == 0
        user_id, _, _ = await _row(admin_engine)
        assert await _roles(admin_engine, user_id) == {"system_admin"}

        assert await provision(_args()) == 0
        _, _, department = await _row(admin_engine)
        assert await _roles(admin_engine, user_id) == {"system_admin", "end_user"}
        assert department == "Finance"

    async def test_a_second_identity_claiming_the_same_mailbox_is_refused(
        self, admin_engine: AsyncEngine
    ) -> None:
        assert await provision(_args()) == 0
        code = await provision(_args(**{"--oid": "askau-prov-someone-else"}))
        assert code == 2

        async with admin_engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM users WHERE email = :e"), {"e": _EMAIL}
                )
            ).scalar_one()
        assert count == 1


class TestWhatHappensAtFirstSignIn:
    async def test_the_self_principal_survives_a_directory_sync(
        self, admin_engine: AsyncEngine
    ) -> None:
        """An empty `groups` claim must not take the person's identity with it.

        This is the new-joiner case exactly: a real user who belongs to no
        groups. Two independent things protect the self principal — the
        `manual` grant and `DirectorySync`'s `kind` filter — and this asserts
        the outcome rather than either mechanism, so it holds whichever one is
        later changed.
        """
        assert await provision(_args()) == 0
        user_id, principal_id, _ = await _row(admin_engine)

        change = await DirectorySync(admin_engine).reconcile(user_id, ())
        assert change.removed == ()

        async with admin_engine.connect() as conn:
            held = (
                (
                    await conn.execute(
                        text("""
                    SELECT principal_id FROM user_principals
                    WHERE user_id = CAST(:u AS uuid)
                    """),
                        {"u": user_id},
                    )
                )
                .scalars()
                .all()
            )
        assert list(held) == [principal_id]

    async def test_group_memberships_are_not_written_here(self, admin_engine: AsyncEngine) -> None:
        """Provisioning grants exactly one membership: the person themselves.
        Anything else would be an access grant no directory can revoke."""
        assert await provision(_args()) == 0
        user_id, _, _ = await _row(admin_engine)

        async with admin_engine.connect() as conn:
            kinds = (
                await conn.execute(
                    text("""
                    SELECT p.kind::text, up.granted_via
                    FROM user_principals up JOIN principals p ON p.id = up.principal_id
                    WHERE up.user_id = CAST(:u AS uuid)
                    """),
                    {"u": user_id},
                )
            ).all()
        assert [(k, g) for k, g in kinds] == [("user", "manual")]
