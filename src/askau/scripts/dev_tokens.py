"""Emit every seeded identity's dev token as one JSON document.

`dev_token` mints one token for one person, which is right for a `curl`. The web
client needs the whole set at once: its proxy attaches a bearer server-side, and
which bearer depends on who is signed in, so it reads a map keyed by username
from `.dev-tokens.json`.

That file used to be produced by hand — a shell loop around `dev_token`, run
once, remembered by nobody. A new developer cloning both repositories got an
absent file, `loadDevTokens()` returning `{}`, and every request answering
"Not signed in" from the proxy. The interface looked signed in and behaved as
though it were not.

Written to stdout rather than to a path, because the destination is in *another
repository* and a script here that writes there would make the two repos
depend on each other's layout. The setup guide pipes it.

Dev mode only: `Settings` refuses `AUTH_MODE=dev` in production, so these
cannot be minted against a real tenant.
"""

from __future__ import annotations

import json
import sys

from askau.core.identity import DevTokenVerifier
from askau.scripts.corpus import USERS
from askau.settings import get_settings

#: Matches `dev_token`. Long enough that a file generated on Monday still works
#: on Friday — an overnight expiry surfaces as a 401 on every request with
#: nothing naming the cause, which costs far more than the short TTL protects.
_TTL_SECONDS = 30 * 24 * 3600


def main() -> int:
    settings = get_settings()
    if not settings.is_dev_auth:
        print("refusing: ASKAU_AUTH_MODE is not 'dev'", file=sys.stderr)
        return 1

    verifier = DevTokenVerifier(settings)
    out = {
        u.username: {
            "email": u.email,
            "name": u.name,
            "token": verifier.issue(
                f"oid-{u.username}",
                email=u.email,
                name=u.name,
                groups=u.groups,
                roles=u.roles,
                department=u.department,
                ttl_seconds=_TTL_SECONDS,
            ),
        }
        for u in USERS
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
