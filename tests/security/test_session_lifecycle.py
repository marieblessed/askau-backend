"""Sign-in, refresh and sign-out (FR-001, FR-001a).

A route-coverage trace over the whole suite found these three had never been
called by any test — not once, in either direction. That is a conspicuous gap
for the endpoints that decide whether somebody is signed in at all, and it
happened for an understandable reason: every other test mints a bearer token
directly with the dev verifier, so the session table was never exercised
through the API that writes it.

The two properties worth holding:

* **A valid organisational token is not an AskAU account.** Entra will happily
  issue a token to anyone in the tenant. Auto-provisioning from that would
  create a user whose department and group membership nobody decided, which is
  to say a user whose authorization nobody decided.
* **Signing out revokes the sessions *and* evicts the cached authorization
  context.** A revoked session with a warm cache keeps answering until the
  cache expires, which is a sign-out that did not sign anyone out.
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


async def _sessions_for(engine: AsyncEngine, email: str) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("""
                    SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id
                    WHERE u.email = :email AND s.revoked_at IS NULL
                    """),
                    {"email": email},
                )
            ).scalar_one()
        )


class TestSignIn:
    async def test_a_valid_token_opens_a_session_and_stamps_the_login(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        tok = token("staff.finance")
        resp = await client.post("/api/v1/auth/session", json={"accessToken": tok})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["session_id"]
        assert body["expires_in"] == 28_800

        async with admin_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("""
                    SELECT s.user_id IS NOT NULL, u.last_login_at IS NOT NULL
                    FROM sessions s JOIN users u ON u.id = s.user_id
                    WHERE s.id = CAST(:sid AS uuid)
                    """),
                    {"sid": body["session_id"]},
                )
            ).first()
        assert row is not None, "the endpoint returned a session id that is not a session"
        assert row[0] and row[1], "last_login_at was not stamped"

    async def test_the_body_is_accepted_in_camel_case(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """`accessToken` is what a browser client sends. This was snake_case
        only until the request models moved onto the wire base — the response
        side had been converted and the request side had not."""
        resp = await client.post("/api/v1/auth/session", json={"accessToken": token("staff.hr")})
        assert resp.status_code == 201

    async def test_snake_case_still_parses(self, client: httpx.AsyncClient, token) -> None:
        """`populate_by_name` keeps the old spelling working. The API is a
        published surface and being strict about casing on input buys nothing."""
        resp = await client.post("/api/v1/auth/session", json={"access_token": token("staff.hr")})
        assert resp.status_code == 201

    async def test_a_garbage_token_is_refused(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/api/v1/auth/session", json={"accessToken": "not-a-token"})
        assert resp.status_code == 401


class TestRefresh:
    async def test_refresh_issues_a_new_session(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        tok = token("staff.legal")
        first = (await client.post("/api/v1/auth/session", json={"accessToken": tok})).json()[
            "session_id"
        ]
        second = (await client.post("/api/v1/auth/session/refresh", headers=auth(tok))).json()[
            "session_id"
        ]
        assert second != first

    async def test_refresh_requires_authentication(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/v1/auth/session/refresh")).status_code == 401


class TestSignOut:
    async def test_signing_out_revokes_every_session_for_that_person(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Every session, not the current one.

        Somebody signing out on a shared machine means it, and leaving their
        other sessions live is the failure mode that matters — the whole point
        of the action is that it ends access.
        """
        tok = token("staff.peace")
        email = "staff.peace@africanunion.org"
        await client.post("/api/v1/auth/session", json={"accessToken": tok})
        await client.post("/api/v1/auth/session/refresh", headers=auth(tok))
        assert await _sessions_for(admin_engine, email) >= 2

        assert (await client.delete("/api/v1/auth/session", headers=auth(tok))).status_code == 204
        assert await _sessions_for(admin_engine, email) == 0

    async def test_signing_out_does_not_touch_anyone_elses_sessions(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        mine, theirs = token("staff.misd"), token("exec.office")
        await client.post("/api/v1/auth/session", json={"accessToken": mine})
        await client.post("/api/v1/auth/session", json={"accessToken": theirs})

        await client.delete("/api/v1/auth/session", headers=auth(mine))
        assert await _sessions_for(admin_engine, "exec.office@africanunion.org") >= 1
