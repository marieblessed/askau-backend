"""Mint a development bearer token for a seeded identity.

Dev mode only. ``Settings`` refuses ``AUTH_MODE=dev`` in production, so this
cannot become a way to forge an organizational identity against a real tenant.
"""

from __future__ import annotations

import sys

from askau.core.identity import DevTokenVerifier
from askau.scripts.corpus import USERS
from askau.settings import get_settings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m askau.scripts.dev_token <username>", file=sys.stderr)
        print("\nSeeded identities:", file=sys.stderr)
        for u in USERS:
            print(f"  {u.username:18} {u.department:20} {', '.join(u.roles)}", file=sys.stderr)
        return 2

    username = argv[1]
    user = next((u for u in USERS if u.username == username), None)
    if user is None:
        print(f"unknown identity: {username}", file=sys.stderr)
        return 2

    settings = get_settings()
    if not settings.is_dev_auth:
        print("refusing: ASKAU_AUTH_MODE is not 'dev'", file=sys.stderr)
        return 1

    token = DevTokenVerifier(settings).issue(
        f"oid-{user.username}",
        email=user.email,
        name=user.name,
        groups=user.groups,
        roles=user.roles,
        department=user.department,
        # Thirty days. Twelve hours sounded prudent and was not: the token
        # expires overnight, and the next morning the interface reports "could
        # not complete" on every question with nothing anywhere naming the
        # cause. That failure cost more than a short-lived dev credential ever
        # protected. `ASKAU_AUTH_MODE=dev` is refused outright in production by
        # Settings validation, so a long-lived token here cannot become one
        # there — the boundary is the mode, not the TTL.
        ttl_seconds=30 * 24 * 3600,
    )
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
