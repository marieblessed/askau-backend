"""The "Save conversation history" toggle, and what it does and does not cover.

The client has shipped this switch since its first commit, wired to nothing. A
switch that says questions are not being kept, while `messages` records every
one, is worse than no switch at all — so these tests exist to hold the promise
rather than to exercise the code path.

Every assertion here reads the database directly. Going through the API would
prove the wrong thing: the API is the one path that was never in doubt, and the
promise is about what is *on disk* afterwards.

The line these tests draw, and it falls somewhere unobvious:

* Nothing about the conversation survives — no `conversations` row, no
  `messages`, no `citations`.
* The audit event survives regardless. BR-008 makes `audit_events` append-only
  and non-optional; that somebody asked something, and which documents their
  question reached, is a security record and not a convenience anyone may
  decline. What that record must never contain is the question or answer text.

Both halves have to hold at once. Only the first is a privacy feature; only the
second keeps it from breaking BR-008. Either alone is a defect.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

_QUESTION = "What is the annual leave entitlement for staff?"


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _set_history(client: httpx.AsyncClient, tok: str, *, on: bool) -> None:
    resp = await client.patch(
        "/api/v1/users/me/preferences", headers=auth(tok), json={"saveHistory": on}
    )
    assert resp.status_code == 200
    assert resp.json()["saveHistory"] is on


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def _audit_reaches(engine: AsyncEngine, sql: str, target: int, **params: object) -> int:
    """Poll until the audit queue has drained, or give up after a deadline.

    `AuditWriter.record` enqueues and returns; the write happens off the request
    path so that auditing can never slow or fail a request. That is the right
    design and it makes a read-immediately-after assertion racy — it passed by
    luck against a running server and failed in-process, which is the more
    honest of the two results.

    Polling rather than sleeping a fixed interval: the wait is then as short as
    the machine allows, and a genuine failure still fails within the deadline
    instead of hanging.
    """
    deadline = asyncio.get_running_loop().time() + 5.0
    count = await _count(engine, sql, **params)
    while count < target and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        count = await _count(engine, sql, **params)
    return count


class TestNothingIsKept:
    async def test_asking_with_history_off_writes_no_conversation_and_no_message(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        tok = token("staff.finance")
        await _set_history(client, tok, on=False)
        try:
            before_c = await _count(admin_engine, "SELECT count(*) FROM conversations")
            before_m = await _count(admin_engine, "SELECT count(*) FROM messages")

            cid = (await client.post("/api/v1/conversations", headers=auth(tok), json={})).json()[
                "id"
            ]
            answer = await client.post(
                f"/api/v1/conversations/{cid}/messages",
                headers=auth(tok),
                json={"content": _QUESTION},
            )

            # The answer is still a real answer. Turning off history is not
            # turning off the product, and a test that passed because the
            # request failed would prove nothing.
            assert answer.status_code == 200
            assert answer.json()["content"].strip()

            assert await _count(admin_engine, "SELECT count(*) FROM conversations") == before_c
            assert await _count(admin_engine, "SELECT count(*) FROM messages") == before_m
            # Specifically not this id, which is the one the client held and the
            # only one it could have written under.
            assert (
                await _count(
                    admin_engine,
                    "SELECT count(*) FROM conversations WHERE id = CAST(:cid AS uuid)",
                    cid=cid,
                )
                == 0
            )
        finally:
            await _set_history(client, tok, on=True)

    async def test_no_message_id_is_returned_so_the_client_hides_feedback(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """`message_feedback` has a foreign key to `messages`.

        With nothing stored there is nothing to rate, so FR-043 cannot apply to
        this turn. A null `messageId` is how that reaches the interface: their
        `AnswerActions` renders the feedback bar only when one is present, so
        the control disappears on its own. The alternative — returning an id
        that references no row — would put a foreign-key error behind a thumbs-up.
        """
        tok = token("staff.finance")
        await _set_history(client, tok, on=False)
        try:
            cid = (await client.post("/api/v1/conversations", headers=auth(tok), json={})).json()[
                "id"
            ]
            body = (
                await client.post(
                    f"/api/v1/conversations/{cid}/messages",
                    headers=auth(tok),
                    json={"content": _QUESTION},
                )
            ).json()
            assert body["messageId"] is None
            # Sources still travel inline, so the answer is still attributable
            # on screen even though nothing was written.
            assert body["citations"]
        finally:
            await _set_history(client, tok, on=True)


class TestTheAuditRecordSurvives:
    async def test_query_is_audited_but_carries_no_question_or_answer_text(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """The assertion most likely to regress silently.

        `core/redaction.py` already blanks a set of known key names — `question`,
        `answer`, `content`, `quote` — at any depth, so the obvious version of
        this mistake is caught before the row is written. Confirmed by mutation:
        adding `"question": body.content` to the detail below stores
        `[REDACTED]` and this test stays green, correctly.

        What the denylist cannot catch is a key nobody thought of. Adding
        `"user_query": body.content` for debugging leaks the question in full,
        breaks the privacy promise, and breaks no other test — the audit event
        is still written and the conversation is still absent. That mutation
        fails here, which is the whole reason this test asserts on *content*
        rather than on field names.
        """
        tok = token("staff.finance")
        await _set_history(client, tok, on=False)
        try:
            cid = (await client.post("/api/v1/conversations", headers=auth(tok), json={})).json()[
                "id"
            ]
            answer = await client.post(
                f"/api/v1/conversations/{cid}/messages",
                headers=auth(tok),
                json={"content": _QUESTION},
            )
            assert answer.status_code == 200

            # Keyed on the conversation id rather than "the most recent query
            # event". The writer drains asynchronously and the suite is writing
            # audit rows the whole time, so "most recent" selects a different
            # request's row often enough to make the assertion meaningless — it
            # passed against a deliberately leaking build once already.
            by_resource = (
                "SELECT count(*) FROM audit_events "
                "WHERE event_category = 'query' AND resource_id = :cid"
            )
            assert await _audit_reaches(admin_engine, by_resource, 1, cid=cid) == 1, (
                "BR-008: the query must be audited regardless"
            )

            async with admin_engine.connect() as conn:
                details = [
                    row[0]
                    for row in (
                        await conn.execute(
                            text(
                                "SELECT detail::text FROM audit_events "
                                "WHERE event_category = 'query' AND resource_id = :cid"
                            ),
                            {"cid": cid},
                        )
                    ).all()
                ]
            assert details

            # Match on content, not on field names: the leak to catch is text
            # appearing under *any* key, including one added in good faith for
            # debugging.
            blob = " ".join(details).lower()
            assert "annual leave" not in blob
            for word in answer.json()["content"].lower().split():
                if len(word) > 7 and word.isalpha():
                    assert word not in blob, f"answer text leaked into audit: {word!r}"
        finally:
            await _set_history(client, tok, on=True)


class TestEphemeralIdsAdmitNothingElse:
    """`create` mints an unwritten uuid when history is off, so the ownership
    check has to accept an id with no row behind it. That is a widening of a
    security predicate, and this is the test that bounds it."""

    async def test_another_users_real_conversation_is_still_refused(
        self, client: httpx.AsyncClient, token
    ) -> None:
        owner, intruder = token("staff.hr"), token("staff.finance")
        victim = (
            await client.post(
                "/api/v1/conversations", headers=auth(owner), json={"title": "HR only"}
            )
        ).json()["id"]

        await _set_history(client, intruder, on=False)
        try:
            posted = await client.post(
                f"/api/v1/conversations/{victim}/messages",
                headers=auth(intruder),
                json={"content": _QUESTION},
            )
            read = await client.get(f"/api/v1/conversations/{victim}", headers=auth(intruder))
            # 404 and not 403: another person's conversation is indistinguishable
            # from one that does not exist, which is the same answer the rest of
            # the ownership checks give.
            assert posted.status_code == 404
            assert read.status_code == 404
        finally:
            await _set_history(client, intruder, on=True)


class TestDeleteAllHistory:
    async def test_deletes_only_the_callers_own(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """ "All" means all of mine.

        The ownership predicate is already tested for reads; this extends it to
        a bulk write, where getting it wrong is unrecoverable rather than merely
        a disclosure.
        """
        mine, theirs = token("staff.finance"), token("staff.hr")
        my_cid = (
            await client.post("/api/v1/conversations", headers=auth(mine), json={"title": "mine"})
        ).json()["id"]
        their_cid = (
            await client.post(
                "/api/v1/conversations", headers=auth(theirs), json={"title": "theirs"}
            )
        ).json()["id"]

        resp = await client.delete("/api/v1/users/me/history", headers=auth(mine))
        assert resp.status_code == 200
        assert resp.json()["deleted"] >= 1

        assert (
            await _count(
                admin_engine,
                "SELECT count(*) FROM conversations WHERE id = CAST(:cid AS uuid)",
                cid=my_cid,
            )
            == 0
        )
        assert (
            await _count(
                admin_engine,
                "SELECT count(*) FROM conversations WHERE id = CAST(:cid AS uuid)",
                cid=their_cid,
            )
            == 1
        ), "another user's history was destroyed by a delete scoped to the caller"

    async def test_deletion_is_itself_audited(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Not a contradiction with erasing the history.

        The record says an account erased its history, never what the history
        said. BR-008 makes `audit_events` append-only precisely so that "the
        record is gone" cannot itself be made to disappear.
        """
        tok = token("staff.finance")
        await client.post("/api/v1/conversations", headers=auth(tok), json={"title": "x"})
        before = await _count(
            admin_engine,
            "SELECT count(*) FROM audit_events WHERE event_type = 'conversation.deleted'",
        )
        assert (
            await client.delete("/api/v1/users/me/history", headers=auth(tok))
        ).status_code == 200
        after = await _audit_reaches(
            admin_engine,
            "SELECT count(*) FROM audit_events WHERE event_type = 'conversation.deleted'",
            before + 1,
        )
        assert after == before + 1


class TestPreferencesEndpoint:
    async def test_patching_one_toggle_leaves_the_other_alone(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """A PATCH that reset the omitted field would be a privacy setting
        turning itself back on when an unrelated one was changed."""
        tok = token("staff.finance")
        await client.patch(
            "/api/v1/users/me/preferences",
            headers=auth(tok),
            json={"saveHistory": False, "shareAnalytics": False},
        )
        try:
            body = (
                await client.patch(
                    "/api/v1/users/me/preferences", headers=auth(tok), json={"saveHistory": True}
                )
            ).json()
            assert body["saveHistory"] is True
            assert body["shareAnalytics"] is False
        finally:
            await client.patch(
                "/api/v1/users/me/preferences",
                headers=auth(tok),
                json={"saveHistory": True, "shareAnalytics": True},
            )

    async def test_higher_intelligence_defaults_off(self, client: httpx.AsyncClient, token) -> None:
        """The two privacy toggles default on; this one defaults off.

        They are opt-outs of useful behaviour. This one authorises extra work
        per question against a shared database, so nobody's questions get more
        expensive because a column appeared.
        """
        body = (
            await client.get("/api/v1/users/me/preferences", headers=auth(token("staff.legal")))
        ).json()
        assert body["higherIntelligence"] is False
        assert body["saveHistory"] is True

    async def test_preferences_are_per_user(self, client: httpx.AsyncClient, token) -> None:
        a, b = token("staff.finance"), token("staff.hr")
        await _set_history(client, a, on=False)
        try:
            other = (await client.get("/api/v1/users/me/preferences", headers=auth(b))).json()
            assert other["saveHistory"] is True
        finally:
            await _set_history(client, a, on=True)


class TestRetrievalWidthIsNotAClientParameter:
    """FR-016. The reader's influence over retrieval width is a consent flag on
    their account, never a number in a request body."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"content": "leave policy?", "topK": 100},
            {"content": "leave policy?", "candidateK": 500},
            {"content": "leave policy?", "retrieval_candidate_k": 500},
            {"content": "leave policy?", "tier": "thorough"},
        ],
    )
    async def test_a_request_carrying_retrieval_parameters_is_rejected(
        self, client: httpx.AsyncClient, token, payload: dict[str, object]
    ) -> None:
        """422, not a silent ignore.

        Dropping the field would leave an integrator believing it worked and
        quietly getting different results than they think — which surfaces
        months later as a bug report about relevance, with nothing in the logs
        to explain it. `tier` is in this list because escalation is the
        system's decision to make: a client that could name the tier would have
        turned consent into a request.
        """
        resp = await client.post("/api/v1/ask", headers=auth(token("staff.finance")), json=payload)
        assert resp.status_code == 422

    async def test_a_plain_question_is_still_accepted(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The guard above forbids unknown fields, so this is the test that
        catches it forbidding a legitimate one — including the camelCase alias
        their client actually sends."""
        resp = await client.post(
            "/api/v1/ask",
            headers=auth(token("staff.finance")),
            json={"content": "How much annual leave do staff get?", "includeHistorical": False},
        )
        assert resp.status_code == 200
