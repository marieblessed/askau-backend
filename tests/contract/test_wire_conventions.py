"""Conventions the web client's own tests pin, asserted from our side.

`askau-frontend/tests/unit/api-errors.test.ts` maps status codes to error
classes and asserts which fields each one reads. That file is the specification
these tests mirror — if it and this disagree, one of the two repositories has
drifted and the mismatch surfaces here rather than in front of a user.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

#: Their `ApiErrorCode` union. A value outside this set reaches their UI as an
#: opaque "unexpected error", so emitting one is a silent failure.
CLIENT_ERROR_CODES = {
    "UNAUTHORIZED",
    "FORBIDDEN",
    "NOT_FOUND",
    "VALIDATION_ERROR",
    "RATE_LIMITED",
    "TIMEOUT",
    "SERVER_ERROR",
    "NETWORK_ERROR",
    "UNKNOWN_ERROR",
}


#: Endpoints whose bodies are shaped for the client. Admin, security and
#: evaluation are excluded deliberately — the client consumes none of them, and
#: they still return raw dicts, some carrying JSONB whose keys are *data*
#: (`error_detail`, `stage_timings`) and must not be rewritten.
CLIENT_FACING = (
    "/api/v1/health",
    "/api/v1/users/me",
    "/api/v1/conversations",
)


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def _snake_keys(node: object, path: str = "") -> list[str]:
    """Every key in a response that would arrive snake_case.

    Recursive because a leak is most likely to be somewhere nested — a citation
    inside a message inside a list envelope — which is exactly where eyeballing
    a payload stops working.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if "_" in key and not key.startswith("_"):
                found.append(f"{path}.{key}" if path else key)
            found.extend(_snake_keys(value, f"{path}.{key}" if path else key))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(_snake_keys(item, f"{path}[{i}]"))
    return found


class TestNothingLeaksSnakeCase:
    """The client's types are camelCase throughout.

    A single snake_case key is not a cosmetic problem: it arrives as `undefined`
    in TypeScript, renders as blank, and does so without an error anywhere.
    """

    @pytest.mark.parametrize("path", CLIENT_FACING)
    async def test_client_facing_endpoints_are_camel_case(
        self, client: httpx.AsyncClient, token, path: str
    ) -> None:
        response = await client.get(path, headers=auth(token("staff.finance")))
        assert response.status_code == 200, response.text
        leaks = _snake_keys(response.json())
        assert not leaks, f"{path} returned snake_case keys: {leaks}"

    async def test_a_stored_conversation_is_camel_case_all_the_way_down(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The nested case, which is the one that actually breaks.

        A conversation contains messages, which contain sources. Each level was a
        separate hand-built dict before, and each was a separate chance to miss
        one.
        """
        tok = token("staff.finance")
        created = await client.post("/api/v1/conversations", json={}, headers=auth(tok))
        conversation_id = created.json()["id"]
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What is the annual leave entitlement?"},
            headers=auth(tok),
        )

        body = (
            await client.get(f"/api/v1/conversations/{conversation_id}", headers=auth(tok))
        ).json()
        assert body["messages"], "no messages to check"
        assert body["messages"][-1]["sources"], "no sources to check"
        leaks = _snake_keys(body)
        assert not leaks, f"nested snake_case: {leaks}"


class TestErrorContract:
    """Mirrors `askau-frontend/tests/unit/api-errors.test.ts`."""

    async def test_401_carries_a_code_and_correlation_id(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/conversations")
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "UNAUTHORIZED"
        # Their `UnauthorizedError` reads only `correlationId`, and it is what a
        # support request is traced by.
        assert body["correlationId"]

    async def test_404_for_a_conversation_that_is_not_yours(
        self, client: httpx.AsyncClient, token
    ) -> None:
        response = await client.get(
            "/api/v1/conversations/00000000-0000-0000-0000-000000000000",
            headers=auth(token("staff.finance")),
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    async def test_validation_failures_are_422_with_a_readable_message(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """422 specifically.

        Their client reads `message` and `fieldErrors` off a 422 and nothing
        else; every other 4xx becomes `UnknownApiError` with the message
        suppressed in production. A 400 would therefore reach a user as "An
        unexpected error occurred" however clearly we explained the problem.
        """
        tok = token("staff.finance")
        conversation_id = (
            await client.post("/api/v1/conversations", json={}, headers=auth(tok))
        ).json()["id"]
        answered = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What is the annual leave entitlement?"},
            headers=auth(tok),
        )
        message_id = answered.json()["messageId"]

        response = await client.post(
            f"/api/v1/messages/{message_id}/feedback",
            json={"rating": "not_helpful"},  # FR-044 requires a reason
            headers=auth(tok),
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["message"] and body["message"] != "Invalid request", (
            "the message must say what is actually wrong — it is the only "
            "explanation their UI will show"
        )

    async def test_every_error_code_is_one_the_client_understands(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """A code outside their union is an opaque failure on their side."""
        cases = [
            (await client.get("/api/v1/conversations"), 401),
            (
                await client.get("/api/v1/admin/overview", headers=auth(token("staff.finance"))),
                403,
            ),
            (
                await client.get(
                    "/api/v1/conversations/00000000-0000-0000-0000-000000000000",
                    headers=auth(token("staff.finance")),
                ),
                404,
            ),
        ]
        for response, expected in cases:
            assert response.status_code == expected, response.text
            code = response.json().get("code")
            assert code in CLIENT_ERROR_CODES, f"{expected} returned unknown code {code!r}"

    async def test_problem_json_is_still_intact(self, client: httpx.AsyncClient) -> None:
        """The client's fields are *extensions*, not a replacement.

        RFC 9457 remains a documented decision (04-api-endpoints.md), and other
        consumers of this API — the Phase 2 integration layer among them — may
        rely on it.
        """
        response = await client.get("/api/v1/conversations")
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        for member in ("type", "title", "status", "detail"):
            assert member in body, f"problem+json lost its {member!r} member"


class TestSourceCardsAreUsable:
    """The client's `SourceCard` fields carry values, not just keys.

    A shape test passes when every field is present and empty. That is not the
    same as a usable card: `department`, `docType`, `status`, `effectiveDate`
    and `published` were all present and all blank for a while, and their card
    rendered five empty rows without anything failing.

    The distinction matters most for `status`. A reader deciding whether to act
    on a quoted policy needs to know it says "Superseded" — and a field that is
    reliably blank teaches them to stop looking at it, which is worse than the
    field not existing.
    """

    async def _a_source(self, client: httpx.AsyncClient, token) -> dict:  # type: ignore[no-untyped-def]
        tok = token("staff.finance")
        created = await client.post("/api/v1/conversations", json={}, headers=auth(tok))
        cid = created.json()["id"]
        await client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={"content": "What is the annual leave entitlement?"},
            headers=auth(tok),
        )
        body = (await client.get(f"/api/v1/conversations/{cid}/messages", headers=auth(tok))).json()
        sources = body["items"][-1]["sources"]
        assert sources, "no sources on a grounded answer"
        return dict(sources[0])

    async def test_document_governance_fields_are_populated(
        self, client: httpx.AsyncClient, token
    ) -> None:
        source = await self._a_source(client, token)
        for field in ("department", "docType", "status", "effectiveDate", "published"):
            assert source[field], (
                f"{field} is blank — the card renders an empty row. These are joined "
                "from `documents` at read time; check the citation query still selects them."
            )

    async def test_status_is_display_text_not_an_enum_value(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """Their card prints this string directly, so `superseded` would show
        lower-cased mid-sentence."""
        source = await self._a_source(client, token)
        assert source["status"] in {
            "Draft",
            "Active",
            "Under review",
            "Expired",
            "Superseded",
        }, source["status"]

    async def test_doc_type_is_one_the_client_declares(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """Their `DocumentType` union drives an icon and a label; a value outside
        it renders as neither."""
        source = await self._a_source(client, token)
        assert source["docType"] in {
            "policy",
            "procedure",
            "sop",
            "guideline",
            "manual",
            "circular",
            "faq",
            "report",
            "other",
        }, source["docType"]

    async def test_a_superseded_document_says_so(self, client: httpx.AsyncClient, token) -> None:
        """The field exists to carry exactly this, and nothing else proves it.

        Asked with `include_historical`, retrieval admits the superseded version
        of the travel policy — and the card must report it as superseded rather
        than inheriting the current one's status.
        """
        tok = token("staff.finance")
        cid = (await client.post("/api/v1/conversations", json={}, headers=auth(tok))).json()["id"]
        await client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={
                "content": "What is the official travel authorization procedure?",
                "include_historical": True,
            },
            headers=auth(tok),
        )
        body = (await client.get(f"/api/v1/conversations/{cid}/messages", headers=auth(tok))).json()
        statuses = {s["status"] for s in body["items"][-1]["sources"]}
        assert "Superseded" in statuses, (
            f"no source reported as superseded, saw {statuses} — the seed contains a "
            "superseded travel policy and include_historical should surface it"
        )
