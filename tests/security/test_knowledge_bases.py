"""The knowledge-base registry (§5.4) — read-only, and scoped to the caller.

Two properties, and the second is the one that makes this a security test
rather than a feature test.

* **Read-only.** Switching a source on is BR-001's approval act. Their UI
  renders status pills rather than toggles, which is correct, and the API must
  make that the only possibility rather than a convention the client follows.
* **The counts are the caller's counts.** The size of a repository somebody
  cannot read is information about that repository. An unscoped `count(*)`
  would leak it while also telling the reader something untrue about what they
  can be answered from.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


class TestCountsAreScopedToTheCaller:
    async def test_nobody_is_shown_the_unscoped_total(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """The failure this catches is a `count(*)` that forgot the ACL join.

        It would pass every functional check — the endpoint returns, the shape
        is right, the number is plausible — and quietly report the whole corpus
        to everyone. Comparing against the true total is what makes it visible.
        """
        async with admin_engine.connect() as conn:
            corpus = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM documents WHERE lifecycle <> 'draft'")
                    )
                ).scalar_one()
            )

        for username in ("staff.finance", "staff.legal", "staff.new"):
            body = (
                await client.get("/api/v1/knowledge-bases", headers=auth(token(username)))
            ).json()
            reachable = sum(i["documentCount"] for i in body["items"])
            assert 0 < reachable < corpus, (
                f"{username} was shown {reachable} of {corpus} documents — "
                "the count is not filtered by the ACL"
            )

    async def test_identities_with_different_access_see_different_counts(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """Mutation-proof for the assertion above.

        A hardcoded number, or a count filtered by something that happens to
        correlate with access, would satisfy "less than the corpus" for
        everyone. Two identities disagreeing is what shows the filter is
        reading *their* principals.
        """

        counts = {}
        for username in ("staff.dual", "staff.new"):
            body = (
                await client.get("/api/v1/knowledge-bases", headers=auth(token(username)))
            ).json()
            counts[username] = sum(i["documentCount"] for i in body["items"])
        assert counts["staff.dual"] != counts["staff.new"], (
            "an identity in two departments should reach more than a new joiner"
        )


class TestTheRegistryIsReadOnly:
    @pytest.mark.parametrize("method", ["post", "patch", "put", "delete"])
    async def test_no_write_verb_is_routed(
        self, client: httpx.AsyncClient, token, method: str
    ) -> None:
        """405, because the route does not exist rather than because a guard
        refused it. Per-source activation lives on the admin surface, where the
        RBAC checks and the approval audit already are."""
        # `request` rather than the per-verb helpers: httpx's `delete()` takes
        # no body, and the point is the verb, not the payload.
        resp = await client.request(
            method.upper(), "/api/v1/knowledge-bases", headers=auth(token("staff.finance"))
        )
        assert resp.status_code == 405


class TestShapeMatchesWhatExists:
    async def test_no_version_is_invented(self, client: httpx.AsyncClient, token) -> None:
        """Their UI renders `"v2.6"` beside each name and `knowledge_sources`
        has no version column — a revision concept was never built.

        Returning a plausible-looking number would be worse than the gap: it
        would be displayed as provenance, which is the one thing this product
        cannot be casual about. `lastSyncedAt` goes back instead, and the
        mismatch goes to their team as a question rather than a guess.
        """
        body = (await client.get("/api/v1/knowledge-bases", headers=auth(token("staff.hr")))).json()
        assert body["items"]
        for item in body["items"]:
            assert "version" not in item
            assert "lastSyncedAt" in item

    async def test_requires_authentication(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/v1/knowledge-bases")).status_code == 401
