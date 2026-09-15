"""Create an AskAU account for a real directory identity.

There is deliberately no provisioning *endpoint* (see `askau.db.directory`): a
valid organisational token is not an AskAU account, and inventing a department
and an access posture from a token would produce a user whose authorization
nobody decided. `POST /auth/session` refuses an unknown identity, and that is
the intended behaviour.

But "decided by a person" is not the same as "typed as SQL at three in the
morning", and until now hand-written SQL was the only option — which is how
`docs/entra-test-tenant.md` came to carry a twenty-line `INSERT` with a mistake
in it. This is the deliberate act, made repeatable.

## What it does not do

**It does not grant group memberships.** Those arrive from the access token at
sign-in and are reconciled by `DirectorySync`, which owns every row it wrote
(`granted_via = 'entra_sync'`) and removes the ones the directory no longer
claims. A group granted here would either be swept away at the next sign-in or,
worse, escape the sweep and become an access grant no directory can revoke.

The self principal is written `granted_via = 'manual'` so that it sits outside
the sync's scope. Measured, that turns out to be the *second* of two guards —
`DirectorySync` also filters by `principals.kind`, and the self principal is
`user`, so either one alone saves it. Both are kept: they fail independently,
and the one that survives is the one that has to hold.

## The one detail worth reading twice

The self principal's `external_id` is **the Entra object id**, not a friendly
`usr-alice`. It has to be: a source connector writes Entra object ids into
`document_acl` for grants made to an individual, and a corpus that names people
by GUID while accounts name them by nickname produces two principals for one
person and an individual grant that silently matches nobody.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from askau.audit.events import EventType
from askau.audit.writer import AuditEvent, AuditWriter
from askau.domain.enums import AppRole
from askau.settings import get_settings

#: Kinds a *person* principal may already exist under. Only one today, but
#: resolved as a set for the same reason `DirectorySync` does: uniqueness is on
#: `(kind, external_id)`, so assuming a kind is how you mint a duplicate.
_PERSON_KINDS = ("user",)

#: Not `entra_sync`. `DirectorySync` deletes every `entra_sync` row the token
#: does not re-assert, and the token never asserts a person's own principal.
_GRANT_SOURCE = "manual"


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="provision-user",
        description="Create or update an AskAU account for a directory identity.",
    )
    p.add_argument("--oid", required=True, help="Entra object id (Users -> the user -> Object ID)")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True, help="Display name")
    p.add_argument("--department")
    p.add_argument("--job-title", dest="job_title")
    p.add_argument(
        "--role",
        dest="roles",
        action="append",
        choices=[r.value for r in AppRole],
        help="AskAU application role; repeatable. Defaults to end_user.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and write nothing.",
    )
    return p.parse_args(argv)


async def _resolve_principal(conn: AsyncConnection, oid: str, name: str) -> tuple[int, bool]:
    """The person's own principal, created only if the corpus lacks one.

    Ingestion can get here first: a blob granted to this individual
    mints their principal long before anyone provisions an account for them.
    Inserting blindly would make a second one and split their grants across
    both.
    """
    existing = (
        await conn.execute(
            text("""
            SELECT id FROM principals
            WHERE external_id = :ext AND kind = ANY(CAST(:kinds AS principal_kind[]))
            ORDER BY id LIMIT 1
            """),
            {"ext": oid, "kinds": list(_PERSON_KINDS)},
        )
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing), False

    minted = (
        await conn.execute(
            text("""
            INSERT INTO principals (kind, external_id, display_name)
            VALUES ('user', :ext, :name)
            RETURNING id
            """),
            {"ext": oid, "name": name},
        )
    ).scalar_one()
    return int(minted), True


async def provision(argv: list[str]) -> int:
    """Parse and apply. Async so tests can drive it inside their own loop."""
    args = _parse(argv)
    roles = sorted(set(args.roles or [AppRole.END_USER.value]))
    settings = get_settings()
    # The migration role, as with the reconciler: row-level security filters
    # `askau_app` by session principals, and provisioning sets none.
    engine = create_async_engine(settings.migration_url)

    audit = AuditWriter(engine)
    try:
        async with engine.begin() as conn:
            # Email is unique independently of the object id. Without this check
            # the collision surfaces as a constraint violation naming
            # `users_email_key`, which reads like a bug in the script rather
            # than two identities claiming one mailbox.
            clash = (
                await conn.execute(
                    text("SELECT entra_oid FROM users WHERE email = :e AND entra_oid <> :oid"),
                    {"e": args.email, "oid": args.oid},
                )
            ).scalar_one_or_none()
            if clash is not None:
                print(
                    f"  refused: {args.email} already belongs to object id {clash}.\n"
                    "  One mailbox, one account. Correct the address or remove the "
                    "existing user first.",
                    file=sys.stderr,
                )
                return 2

            prior = (
                await conn.execute(
                    text("""
                    SELECT id, principal_id, display_name, department
                    FROM users WHERE entra_oid = :oid
                    """),
                    {"oid": args.oid},
                )
            ).first()

            if args.dry_run:
                verb = "update" if prior else "create"
                print(f"  would {verb} {args.email} ({args.name})")
                print(f"  roles: {', '.join(roles)}")
                print(f"  self principal external_id: {args.oid}")
                print("  group memberships: none — they arrive at sign-in from the token")
                return 0

            principal_id, minted = await _resolve_principal(conn, args.oid, args.name)

            if prior is None:
                user_id = (
                    await conn.execute(
                        text("""
                        INSERT INTO users
                          (principal_id, entra_oid, email, display_name, department, job_title)
                        VALUES (:pid, :oid, :email, :name, :dept, :title)
                        RETURNING id
                        """),
                        {
                            "pid": principal_id,
                            "oid": args.oid,
                            "email": args.email,
                            "name": args.name,
                            "dept": args.department,
                            "title": args.job_title,
                        },
                    )
                ).scalar_one()
            else:
                user_id = prior[0]
                # `principal_id` is intentionally not rewritten. It is UNIQUE and
                # already referenced by `user_principals`; repointing it would
                # orphan whatever the corpus granted to the old one.
                await conn.execute(
                    text("""
                    UPDATE users
                    SET email = :email,
                        display_name = :name,
                        department = COALESCE(:dept, department),
                        job_title = COALESCE(:title, job_title)
                    WHERE id = :uid
                    """),
                    {
                        "uid": user_id,
                        "email": args.email,
                        "name": args.name,
                        "dept": args.department,
                        "title": args.job_title,
                    },
                )

            await conn.execute(
                text("""
                INSERT INTO user_principals (user_id, principal_id, granted_via)
                VALUES (:uid, :pid, :source)
                ON CONFLICT (user_id, principal_id) DO NOTHING
                """),
                {"uid": user_id, "pid": principal_id, "source": _GRANT_SOURCE},
            )

            granted = []
            for role in roles:
                added = (
                    await conn.execute(
                        text("""
                        INSERT INTO app_role_assignments (user_id, role)
                        VALUES (:uid, :role)
                        ON CONFLICT DO NOTHING
                        RETURNING role
                        """),
                        {"uid": user_id, "role": role},
                    )
                ).scalar_one_or_none()
                if added is not None:
                    granted.append(role)

            # Existing roles are never removed here. Revoking authority is a
            # separate, deliberate act and should not be a side effect of
            # re-running a provisioning command with a shorter --role list.

        await audit.start()
        audit.record(
            AuditEvent(
                event_type=EventType.USER_PROVISIONED,
                resource_type="user",
                resource_id=str(user_id),
                detail={
                    # No actor id: no organisational identity performed this.
                    # Naming the operating-system account is the honest amount
                    # of attribution a shell command can offer.
                    "granted_by": "cli",
                    "operator": _operator(),
                    "created": prior is None,
                    "roles_granted": granted,
                    "principal_minted": minted,
                },
            )
        )
        await audit.stop()

        print(f"  {'created' if prior is None else 'updated'} {args.email}")
        print(f"  user id      {user_id}")
        print(f"  principal    {principal_id} ({'new' if minted else 'existing'}) -> {args.oid}")
        print(f"  roles        {', '.join(roles)}" + ("" if granted else "  (all already held)"))
        print("  groups       none yet; DirectorySync fills them in at first sign-in")
        return 0
    finally:
        await engine.dispose()


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser can fail with no passwd entry
        return "unknown"


def main(argv: list[str]) -> int:
    return asyncio.run(provision(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
