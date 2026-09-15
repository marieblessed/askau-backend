"""Every route is guarded — enumerated from the app, not from a list (§6).

A hand-written RBAC matrix tests the routes someone remembered to add to it.
The routes that leak are the ones nobody remembered, so this suite asks the
application which routes exist and checks all of them. A new admin endpoint
added without a role guard fails here on the day it is written, with no one
having to maintain a list.

Two properties, both stated as "nothing succeeds":

1. No route returns 2xx without a token, unless it is deliberately public.
2. No administrative route returns 2xx for an ordinary member of staff.

Both are deliberately weak assertions — 401, 403 and 422 all pass. The thing
worth failing on is a *success*, and a stricter assertion would break whenever
FastAPI's validation happened to run before the guard, which is noise rather
than signal.
"""

from __future__ import annotations

import pytest

from askau.main import create_app
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

#: Routes that must work without a token, and why. Anything not on this list is
#: required to reject anonymous callers — so making a route public is a
#: deliberate edit here, visible in review, rather than an omission.
PUBLIC: dict[str, str] = {
    "/health/live": "liveness probe; kubelet has no token",
    "/health/ready": "readiness probe",
    "/health/deep": "versions and timings only, no content",
    "/api/v1/health": (
        "service health for the interface; reports whether dependencies answer, "
        "never anything about content"
    ),
    "/metrics": "Prometheus scrape; cluster-internal, counts only",
    "/api/v1/auth/config": "tells the browser where to authenticate — it cannot know yet",
    "/api/v1/auth/session": "this is where a token is obtained",
    "/api/v1/auth/session/refresh": "carries its own refresh credential",
}

#: Prefixes an ordinary member of staff must never reach. `/api/v1/ask`,
#: `/api/v1/conversations` and `/api/v1/documents` are theirs; nothing below is.
PRIVILEGED = (
    "/api/v1/admin",
    "/api/v1/security",
    "/api/v1/knowledge",
    "/api/v1/evaluation",
    "/api/v1/debug",
)

#: Routes under a privileged prefix that are nonetheless user-facing, guarded by
#: the caller's access list rather than by a role. Listed individually and with
#: a reason, because "it lives under /v1/knowledge so it must be admin-only" is
#: the assumption that would otherwise hide a genuinely missing guard.
#: What role-checking cannot cover is covered per-route instead:
#: `test_knowledge_bases.py::TestCountsAreScopedToTheCaller` for the registry,
#: and the document suites for the preview. `test_acl_guarded_exceptions_still_exist`
#: below only keeps this list from outliving the routes on it.
ACL_GUARDED: dict[str, str] = {
    "/api/v1/knowledge-bases": (
        "§5.4: the registry the settings modal lists. Read-only for everyone, "
        "and every row it returns is already filtered by the caller's access "
        "list — a source they can reach nothing in is absent rather than shown "
        "empty. Note it only matches the privileged `/api/v1/knowledge` prefix "
        "by accident of spelling; it is not under that router."
    ),
    "/api/v1/knowledge/documents/{document_id}/preview": (
        "FR-029: the extracted text behind a citation. Any member of staff may "
        "read it for a document their principals reach — that is the point of a "
        "citation being checkable — so it enforces the access list, not a role."
    ),
}

#: Stand-ins for path parameters. The value never needs to resolve: the guard
#: must reject before anything is looked up, and a 404 for an authorised user
#: would be a pass here for the wrong reason — which is why 404 is not accepted.
_SAMPLES = {
    "conversation_id": "00000000-0000-0000-0000-000000000000",
    "document_id": "00000000-0000-0000-0000-000000000000",
    "message_id": "00000000-0000-0000-0000-000000000000",
    "source_id": "00000000-0000-0000-0000-000000000000",
    "run_id": "00000000-0000-0000-0000-000000000000",
    "feedback_id": "1",
}


def _routes() -> list[tuple[str, str]]:
    """Read the routes off the built application's own OpenAPI document.

    Not by walking `app.routes`: FastAPI wraps an included router in a container
    whose `original_router` still carries *router-relative* paths, so a walk
    returns `/v1/x` for a route actually served at `/api/v1/x` — every request
    then 404s and the suite reports a guard failure that is really a test bug.
    The OpenAPI document is the only place the fully-resolved paths exist.

    `test_the_enumeration_actually_found_routes` below exists because this
    failure mode is silent: an empty list makes every other test in the file
    pass vacuously.
    """
    spec = create_app().openapi()
    out: list[tuple[str, str]] = []
    for path, operations in spec["paths"].items():
        if path.startswith(("/openapi", "/docs", "/redoc")):
            continue
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            out.append((method.upper(), path))
    return sorted(set(out))


ROUTES = _routes()


def _concrete(path: str) -> str:
    for name, value in _SAMPLES.items():
        path = path.replace(f"{{{name}}}", value)
    return path


async def _call(client, method: str, path: str, headers: dict[str, str] | None = None):
    return await client.request(
        method,
        _concrete(path),
        headers=headers or {},
        json={} if method in {"POST", "PATCH", "PUT"} else None,
    )


def test_the_enumeration_actually_found_routes() -> None:
    """Guards the guard: a broken enumerator would make every test below pass
    vacuously, which is worse than having no suite at all."""
    assert len(ROUTES) > 40, f"only {len(ROUTES)} routes found — enumeration is broken"


def test_every_public_route_still_exists() -> None:
    """A path renamed without updating PUBLIC would silently stop being
    asserted on, and the stale entry would hide it."""
    known = {path for _, path in ROUTES}
    missing = sorted(set(PUBLIC) - known)
    assert not missing, f"PUBLIC lists routes that no longer exist: {missing}"


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=lambda v: v if isinstance(v, str) else "")
async def test_anonymous_callers_are_refused(client, method: str, path: str) -> None:
    if path in PUBLIC:
        pytest.skip(f"public by design: {PUBLIC[path]}")
    response = await _call(client, method, path)
    assert response.status_code != 200, (
        f"{method} {path} served an anonymous caller. Either it needs a guard, "
        f"or it belongs in PUBLIC with a stated reason."
    )
    assert response.status_code in {401, 403, 422}, (
        f"{method} {path} answered anonymously with {response.status_code}"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [r for r in ROUTES if r[1].startswith(PRIVILEGED) and r[1] not in ACL_GUARDED],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_staff_cannot_reach_privileged_routes(client, token, method: str, path: str) -> None:
    """FR-002, §6.4. staff.finance is an ordinary end user with no admin role."""
    response = await _call(
        client, method, path, {"Authorization": f"Bearer {token('staff.finance')}"}
    )
    assert response.status_code != 200, (
        f"{method} {path} served an ordinary user. Missing a require_*_admin guard."
    )
    assert response.status_code in {401, 403, 422}, (
        f"{method} {path} answered a non-admin with {response.status_code}"
    )


def test_acl_guarded_exceptions_still_exist() -> None:
    """A renamed route would leave a stale exemption behind, and the stale
    entry would quietly stop a real route from being checked."""
    known = {path for _, path in ROUTES}
    missing = sorted(set(ACL_GUARDED) - known)
    assert not missing, f"ACL_GUARDED lists routes that no longer exist: {missing}"


class TestAclGuardedRoutes:
    """What the role sweep cannot cover, because these routes have no role.

    Excluding them from the sweep is only safe if the access list is doing the
    work instead — so that is asserted directly rather than assumed.
    """

    async def test_preview_is_refused_for_another_department(
        self, client, token, doc_id_of
    ) -> None:
        # Confidential, and its access list is exactly {Finance Officers,
        # Finance}. staff.misd is a different department, so no principal of
        # theirs intersects it.
        document_id = await doc_id_of("conf-budget-reallocation")
        response = await client.get(
            f"/api/v1/knowledge/documents/{document_id}/preview",
            headers={"Authorization": f"Bearer {token('staff.misd')}"},
        )
        assert response.status_code != 200, (
            "preview returned another department's document text — the ACL check "
            "is the only guard on this route"
        )

    async def test_preview_is_allowed_for_the_owning_department(
        self, client, token, doc_id_of
    ) -> None:
        """The negative case alone would pass if the route were simply broken."""
        document_id = await doc_id_of("conf-budget-reallocation")
        response = await client.get(
            f"/api/v1/knowledge/documents/{document_id}/preview",
            headers={"Authorization": f"Bearer {token('staff.finance')}"},
        )
        assert response.status_code == 200, (
            f"finance cannot read its own document ({response.status_code}) — the "
            "refusal test above would then pass for the wrong reason"
        )

    async def test_preview_is_refused_without_a_token(self, client, doc_id_of) -> None:
        document_id = await doc_id_of("conf-budget-reallocation")
        response = await client.get(f"/api/v1/knowledge/documents/{document_id}/preview")
        assert response.status_code == 401
