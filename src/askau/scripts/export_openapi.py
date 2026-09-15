"""Export the OpenAPI contract to the shared `contracts/` component.

The contract is the interface between two independently deployed components, so
it lives above both of them rather than inside the one that happens to generate
it. `web` consumes this file; `api` produces it; neither owns
it.

Committed rather than generated on demand, for one reason: a generated artefact
that nobody can see cannot be reviewed. When the contract is in the tree, a
change to a response shape shows up in the diff of the merge request that caused
it, and a reviewer who knows the frontend can object before it ships.

`--check` is the CI half of that. It regenerates in memory and compares; a
mismatch means someone changed the API and did not commit the contract, which is
exactly the drift the whole arrangement exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: Repository root, resolved by walking up from this file rather than from the
#: working directory, so `make contract` behaves the same wherever it is run.
#:
#: `parents[3]`, not `[4]`: the contract used to live one level above the `api`
#: component, because a sibling `web/` component consumed it. The client now has
#: its own repository and this one is the backend, so `[4]` resolved *outside*
#: the repository — `make contract-check`, and therefore `make check`, would
#: have failed on a fresh clone.
_REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = _REPO_ROOT / "contracts" / "openapi.json"


def _spec() -> dict[str, Any]:
    # Imported here rather than at module scope: building the app reads settings
    # and would make `--help` fail on a machine with no environment configured.
    from askau.main import create_app

    return create_app().openapi()


def _serialize(spec: dict[str, Any]) -> str:
    # sort_keys so the diff reflects real interface changes rather than
    # dictionary ordering, and a trailing newline so the file is POSIX-clean and
    # does not show as modified by editors that add one.
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed contract differs from the current API.",
    )
    args = parser.parse_args(argv)

    current = _serialize(_spec())

    if args.check:
        if not CONTRACT_PATH.exists():
            print(
                f"error: {CONTRACT_PATH} does not exist. Run `make contract` and commit it.",
                file=sys.stderr,
            )
            return 1
        if CONTRACT_PATH.read_text(encoding="utf-8") != current:
            print(
                "error: the committed OpenAPI contract is out of date.\n"
                "       The API changed but contracts/openapi.json was not updated.\n"
                "       Run `make contract` and commit the result — the frontend\n"
                "       generates its types from this file and will drift silently\n"
                "       otherwise.",
                file=sys.stderr,
            )
            return 1
        print(f"contract up to date ({CONTRACT_PATH.relative_to(_REPO_ROOT)})")
        return 0

    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(current, encoding="utf-8")
    # Reported so a surprising number is visible immediately: a contract that
    # suddenly has three paths means the app failed to load its routers, and a
    # silent success would commit that.
    print(f"wrote {CONTRACT_PATH.relative_to(_REPO_ROOT)} ({len(_spec()['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
