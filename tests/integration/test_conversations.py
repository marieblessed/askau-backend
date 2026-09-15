"""Conversations, follow-ups, and feedback over HTTP (FR-004 … FR-006, FR-043/044)."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _new(client: httpx.AsyncClient, tok: str) -> str:
    resp = await client.post("/api/v1/conversations", headers=auth(tok), json={})
    assert resp.status_code == 201
    return str(resp.json()["id"])


async def _ask(client: httpx.AsyncClient, tok: str, cid: str, q: str) -> dict:
    resp = await client.post(
        f"/api/v1/conversations/{cid}/messages", headers=auth(tok), json={"content": q}
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


class TestPersistence:
    async def test_a_turn_stores_both_sides(self, client: httpx.AsyncClient, token) -> None:
        tok = token("staff.misd")
        cid = await _new(client, tok)
        await _ask(client, tok, cid, "What is the annual leave entitlement?")

        messages = (await client.get(f"/api/v1/conversations/{cid}", headers=auth(tok))).json()[
            "messages"
        ]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["state"] == "grounded"
        # `sources`, not `citations`: a stored turn is returned in the shape the
        # interface renders, and its richer per-source metadata is what makes a
        # reopened answer checkable rather than just readable.
        assert messages[1]["sources"]
        assert messages[1]["groundingCount"] == len(messages[1]["sources"])

    async def test_the_first_question_titles_the_conversation(
        self, client: httpx.AsyncClient, token
    ) -> None:
        tok = token("staff.misd")
        cid = await _new(client, tok)
        await _ask(client, tok, cid, "What is the annual leave entitlement?")
        listed = (await client.get("/api/v1/conversations", headers=auth(tok))).json()
        mine = next(c for c in listed["items"] if c["id"] == cid)
        assert mine["title"].startswith("What is the annual leave")

    async def test_citations_resolve_to_real_documents(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """FR-030 is referential: a citation that resolves to nothing cannot be
        inserted, so anything read back is real."""
        tok = token("staff.misd")
        cid = await _new(client, tok)
        body = await _ask(client, tok, cid, "What is the annual leave entitlement?")
        for c in body["citations"]:
            resp = await client.get(f"/api/v1/documents/{c['documentId']}", headers=auth(tok))
            assert resp.status_code == 200


class TestFollowUps:
    async def test_a_follow_up_inherits_the_subject(self, client: httpx.AsyncClient, token) -> None:
        """FR-004. "Does this apply to staff on probation?" is meaningless alone."""
        tok = token("staff.misd")
        cid = await _new(client, tok)
        await _ask(client, tok, cid, "What is the annual leave entitlement?")
        follow = await _ask(client, tok, cid, "Does this apply to staff on probation?")

        assert follow["answerState"] == "grounded"
        titles = {c["documentTitle"] for c in follow["citations"]}
        assert "Annual Leave Policy" in titles

    async def test_the_same_follow_up_alone_cannot_be_answered(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The control: without history the question has no subject, so a
        grounded answer above proves the history did the work."""
        tok = token("staff.misd")
        cid = await _new(client, tok)
        alone = await _ask(client, tok, cid, "Does this apply to staff on probation?")
        assert alone["answerState"] != "grounded"


class TestOwnershipIsolation:
    """FR-006. Another user's conversation is indistinguishable from one that
    does not exist."""

    async def test_another_user_cannot_read_it(self, client: httpx.AsyncClient, token) -> None:
        owner = token("staff.misd")
        cid = await _new(client, owner)
        await _ask(client, owner, cid, "What is the annual leave entitlement?")

        resp = await client.get(
            f"/api/v1/conversations/{cid}", headers=auth(token("staff.finance"))
        )
        assert resp.status_code == 404

    async def test_another_user_cannot_post_into_it(self, client: httpx.AsyncClient, token) -> None:
        cid = await _new(client, token("staff.misd"))
        resp = await client.post(
            f"/api/v1/conversations/{cid}/messages",
            headers=auth(token("staff.finance")),
            json={"content": "What is the annual leave entitlement?"},
        )
        assert resp.status_code == 404

    async def test_another_user_cannot_delete_it(self, client: httpx.AsyncClient, token) -> None:
        cid = await _new(client, token("staff.misd"))
        resp = await client.delete(
            f"/api/v1/conversations/{cid}", headers=auth(token("staff.finance"))
        )
        assert resp.status_code == 404

    async def test_listing_shows_only_your_own(self, client: httpx.AsyncClient, token) -> None:
        mine = await _new(client, token("staff.misd"))
        listed = (
            await client.get("/api/v1/conversations", headers=auth(token("staff.finance")))
        ).json()
        assert mine not in {c["id"] for c in listed["items"]}


class TestFeedback:
    async def test_helpful_needs_no_reason(self, client: httpx.AsyncClient, token) -> None:
        tok = token("staff.misd")
        cid = await _new(client, tok)
        body = await _ask(client, tok, cid, "What is the annual leave entitlement?")
        resp = await client.post(
            f"/api/v1/messages/{body['messageId']}/feedback",
            headers=auth(tok),
            json={"rating": "helpful"},
        )
        assert resp.status_code == 204

    async def test_not_helpful_requires_a_reason(self, client: httpx.AsyncClient, token) -> None:
        """ "Not helpful" with no reason cannot be triaged — an administrator
        staring at a count cannot tell wrong from outdated."""
        tok = token("staff.misd")
        cid = await _new(client, tok)
        body = await _ask(client, tok, cid, "What is the annual leave entitlement?")
        resp = await client.post(
            f"/api/v1/messages/{body['messageId']}/feedback",
            headers=auth(tok),
            json={"rating": "not_helpful"},
        )
        # 422, not 400: their client only reads `message` and `fieldErrors` off a
        # 422, and turns every other 4xx into an opaque "unexpected error" in
        # production — so a 400 would hide the very explanation we wrote.
        assert resp.status_code == 422

    async def test_feedback_reads_back_on_the_message(
        self, client: httpx.AsyncClient, token
    ) -> None:
        tok = token("staff.misd")
        cid = await _new(client, tok)
        body = await _ask(client, tok, cid, "What is the annual leave entitlement?")
        await client.post(
            f"/api/v1/messages/{body['messageId']}/feedback",
            headers=auth(tok),
            json={"rating": "not_helpful", "reason": "outdated_information"},
        )
        messages = (await client.get(f"/api/v1/conversations/{cid}", headers=auth(tok))).json()[
            "messages"
        ]
        assert messages[1]["feedback"] == "not_helpful"

    async def test_cannot_rate_someone_elses_answer(self, client: httpx.AsyncClient, token) -> None:
        tok = token("staff.misd")
        cid = await _new(client, tok)
        body = await _ask(client, tok, cid, "What is the annual leave entitlement?")
        resp = await client.post(
            f"/api/v1/messages/{body['messageId']}/feedback",
            headers=auth(token("staff.finance")),
            json={"rating": "helpful"},
        )
        assert resp.status_code == 404


class TestConversationTitle:
    """The detail response names itself (FR-006).

    Added so a permalink can render a heading and a document title without
    fetching the whole conversation list. The scoping is the part worth testing:
    a title is a summary of what someone asked, and §6.5 treats that as their
    business alone — so the title query carries the same `user_id` predicate as
    every other read, and this proves it rather than assuming it.
    """

    async def test_detail_carries_the_title(self, client, token) -> None:
        headers = {"Authorization": f"Bearer {token('staff.finance')}"}
        created = await client.post("/api/v1/conversations", json={}, headers=headers)
        conversation_id = created.json()["id"]
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What is the annual leave entitlement?"},
            headers=headers,
        )

        body = (
            await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
        ).json()
        assert body["title"], "the detail response cannot name itself"
        assert "annual leave" in body["title"].lower()

    async def test_another_users_title_is_not_disclosed(self, client, token) -> None:
        owner = {"Authorization": f"Bearer {token('staff.finance')}"}
        other = {"Authorization": f"Bearer {token('staff.misd')}"}

        created = await client.post("/api/v1/conversations", json={}, headers=owner)
        conversation_id = created.json()["id"]
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "What is the annual leave entitlement?"},
            headers=owner,
        )

        response = await client.get(f"/api/v1/conversations/{conversation_id}", headers=other)
        # 404, not 403: confirming that an id exists but belongs to someone else
        # discloses that they asked something.
        assert response.status_code == 404, response.status_code
        assert "annual leave" not in response.text.lower()
