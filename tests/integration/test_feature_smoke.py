"""Every read surface answers correctly for the role that owns it.

The guard suite proves routes refuse the wrong caller. That is only half the
claim: a route that refuses *everyone* — because its SQL is broken, or its role
constant is wrong — passes every guard test in the file next door. This one
asserts the other half, that each feature actually works for the person it was
built for.

Reads only. Sync, reindex, cancel and approve have real side effects on the
corpus, and a smoke test that fires them would be rewriting the fixture it runs
against; they are listed at the bottom as explicitly uncovered rather than
quietly skipped.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


#: (path, role that owns it). Roles are not interchangeable — §6.4 separates
#: knowledge, system and security administration precisely so that no single
#: account can both change the corpus and erase the record of having done so.
READS: list[tuple[str, str]] = [
    ("/v1/admin/overview", "admin.knowledge"),
    ("/v1/admin/documents", "admin.knowledge"),
    ("/v1/admin/sources", "admin.knowledge"),
    ("/v1/admin/ingestion/runs", "admin.knowledge"),
    ("/v1/admin/ingestion/failures", "admin.knowledge"),
    ("/v1/admin/quality", "admin.knowledge"),
    ("/v1/admin/feedback", "admin.knowledge"),
    ("/v1/admin/config", "admin.system"),
    ("/v1/admin/usage", "admin.system"),
    ("/v1/admin/metrics", "admin.system"),
    ("/v1/knowledge/sources", "admin.knowledge"),
    ("/v1/security/audit-events", "admin.security"),
    ("/v1/security/access-denials", "admin.security"),
    ("/v1/security/ai-safety-events", "admin.security"),
    ("/v1/security/retention", "admin.security"),
    ("/v1/security/audit-events/export", "admin.security"),
    ("/v1/evaluation/datasets", "admin.system"),
    ("/v1/conversations", "staff.finance"),
    ("/v1/debug/retrieve?q=travel", "admin.system"),
]


@pytest.mark.parametrize(("path", "user"), READS, ids=[p for p, _ in READS])
async def test_read_surface_answers_its_owner(
    client: httpx.AsyncClient, token, path: str, user: str
) -> None:
    response = await client.get(path, headers=auth(token(user)))
    assert response.status_code == 200, (
        f"{path} refused {user}, its own role: {response.status_code} {response.text[:200]}"
    )


@pytest.mark.parametrize(
    ("path", "user"),
    [(p, u) for p, u in READS if not p.endswith("export")],
    ids=[p for p, _ in READS if not p.endswith("export")],
)
async def test_read_surface_returns_structured_data(
    client: httpx.AsyncClient, token, path: str, user: str
) -> None:
    """A 200 carrying an empty body would satisfy the test above.

    Every one of these backs a screen; an endpoint that answers `null` or `[]`
    where the console expects an object renders as a blank panel with no error.
    """
    body = (await client.get(path, headers=auth(token(user)))).json()
    assert isinstance(body, dict | list), f"{path} returned {type(body).__name__}"
    assert body != {} and body != [], f"{path} returned an empty payload"


class TestDocumentDetail:
    """Path-parameter routes, which the list endpoints above cannot reach."""

    async def test_admin_document_search(self, client, token) -> None:
        response = await client.get(
            "/v1/admin/documents?q=travel", headers=auth(token("admin.knowledge"))
        )
        assert response.status_code == 200
        assert response.json()["documents"], "no documents matched a known-present title"

    async def test_document_versions(self, client, token, doc_id_of) -> None:
        """FR-030. int-travel-v1 is superseded by v2 in the seed, so this
        document genuinely has a version chain to return."""
        document_id = await doc_id_of("int-travel-v2")
        response = await client.get(
            f"/v1/knowledge/documents/{document_id}/versions",
            headers=auth(token("admin.knowledge")),
        )
        assert response.status_code == 200

    async def test_user_document_metadata(self, client, token, doc_id_of) -> None:
        document_id = await doc_id_of("int-travel-v2")
        response = await client.get(
            f"/v1/documents/{document_id}", headers=auth(token("staff.finance"))
        )
        assert response.status_code == 200


class TestClearance:
    """`/auth/me.max_classification` is derived, not declared.

    It was hardcoded to `internal` — plausible enough to survive review, wrong
    for anyone cleared above or below it. The interface uses this value to
    explain why an answer came back thin, so under-reporting turns "you are not
    cleared for that document" into an apparent hole in the corpus, and staff
    chase a librarian instead of an administrator.
    """

    async def test_reflects_what_the_user_can_actually_reach(self, client, token) -> None:
        finance = (await client.get("/auth/me", headers=auth(token("staff.finance")))).json()
        misd = (await client.get("/auth/me", headers=auth(token("staff.misd")))).json()

        # Finance is on the access list of a confidential document in the seed;
        # MISD is not on any. If these ever match, the value is hardcoded again.
        assert finance["max_classification"] == "confidential", finance
        assert misd["max_classification"] == "internal", misd
        assert finance["max_classification"] != misd["max_classification"], (
            "clearance is identical for two deliberately different identities"
        )

    async def test_never_exposes_the_principal_set(self, client, token) -> None:
        body = (await client.get("/auth/me", headers=auth(token("staff.finance")))).json()
        assert "principals" not in body
        assert isinstance(body["principal_count"], int)


class TestConversationLifecycle:
    """FR-039…FR-045: the multi-turn path, end to end over HTTP."""

    async def test_create_ask_read_rename_delete(self, client, token) -> None:
        headers = auth(token("staff.finance"))

        created = await client.post("/v1/conversations", json={}, headers=headers)
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]

        answered = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            # Deliberately *not* a per-diem or travel question: the seed carries
            # a conflicting policy pair on that subject, so those return
            # `conflict` by design. Asserting `grounded` there would be testing
            # the fixture, not the lifecycle. Conflict has its own test below.
            json={"content": "What is the annual leave entitlement for staff?"},
            headers=headers,
        )
        assert answered.status_code == 200, answered.text
        payload = answered.json()
        assert payload["answer_state"] == "grounded", payload["answer_state"]
        assert payload["citations"], "a grounded answer with no citations is not grounded"
        # Persisted, not just returned — the feedback and history features both
        # depend on this id existing afterwards.
        assert payload["message_id"], "message was not persisted"

        fetched = await client.get(f"/v1/conversations/{conversation_id}", headers=headers)
        assert fetched.status_code == 200
        assert len(fetched.json()["messages"]) >= 2, "question and answer should both persist"

        renamed = await client.patch(
            f"/v1/conversations/{conversation_id}",
            json={"title": "Per diem enquiry"},
            headers=headers,
        )
        assert renamed.status_code in {200, 204}

        removed = await client.delete(f"/v1/conversations/{conversation_id}", headers=headers)
        assert removed.status_code in {200, 204}

        gone = await client.get(f"/v1/conversations/{conversation_id}", headers=headers)
        assert gone.status_code == 404, "deleted conversation is still readable"


class TestConflictDetection:
    """FR-021. Two policies disagreeing must not be averaged into one answer.

    The seed contains a conflicting pair on purpose. Presenting either figure as
    settled fact would be worse than saying nothing: staff would act on a rate
    the Commission has not actually agreed.
    """

    async def test_conflicting_policies_surface_as_conflict(self, client, token) -> None:
        headers = auth(token("staff.finance"))
        conversation_id = (await client.post("/v1/conversations", json={}, headers=headers)).json()[
            "id"
        ]
        answered = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            json={"content": "What is the daily subsistence allowance for continental travel?"},
            headers=headers,
        )
        payload = answered.json()
        assert payload["answer_state"] == "conflict", (
            f"expected the seeded contradiction to be detected, got {payload['answer_state']}"
        )
        assert len(payload["citations"]) >= 2, (
            "a conflict must cite both sides — one citation cannot show a disagreement"
        )


class TestFeedback:
    """FR-043, FR-044."""

    async def test_rate_then_withdraw(self, client, token) -> None:
        headers = auth(token("staff.finance"))
        created = await client.post("/v1/conversations", json={}, headers=headers)
        conversation_id = created.json()["id"]
        answered = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            json={"content": "What is the annual leave entitlement?"},
            headers=headers,
        )
        message_id = answered.json()["message_id"]

        rated = await client.post(
            f"/v1/messages/{message_id}/feedback",
            json={"rating": "not_helpful", "reason": "missing_information"},
            headers=headers,
        )
        assert rated.status_code in {200, 201, 204}, rated.text

        withdrawn = await client.delete(f"/v1/messages/{message_id}/feedback", headers=headers)
        assert withdrawn.status_code in {200, 204}, withdrawn.text


class TestStreaming:
    """FR-041. The path the interface actually uses."""

    async def test_stream_emits_done_with_an_answer(self, client, token) -> None:
        headers = auth(token("staff.finance"))
        conversation_id = (await client.post("/v1/conversations", json={}, headers=headers)).json()[
            "id"
        ]

        async with client.stream(
            "POST",
            f"/v1/conversations/{conversation_id}/messages/stream",
            json={"content": "What is the daily subsistence allowance?"},
            headers=headers,
        ) as response:
            assert response.status_code == 200
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert "event: done" in body, f"stream never completed: {body[:300]}"
        assert "event: sources" in body, "no sources event — citations would never render"


#: Not covered here, deliberately. Each mutates the corpus or the audit record,
#: and a smoke test that fired them would corrupt the fixture every other test
#: in the suite reads from. They need dedicated tests with their own teardown.
UNCOVERED = [
    "POST /v1/knowledge/sources",
    "PATCH /v1/knowledge/sources/{id}",
    "POST /v1/knowledge/sources/{id}/approve",
    "POST /v1/knowledge/sources/{id}/sync",
    "POST /v1/knowledge/sources/{id}/reindex",
    "POST /v1/knowledge/sources/{id}/test-connection",
    "PATCH /v1/knowledge/documents/{id}",
    "POST /v1/knowledge/documents/{id}/reprocess",
    "POST /v1/admin/ingestion/runs/{id}/cancel",
    "PATCH /v1/admin/feedback/{id}",
    "POST /v1/evaluation/runs",
    "POST /v1/messages/{id}/stop",
]


def test_uncovered_list_is_honest() -> None:
    """Keeps the gap visible instead of letting it read as full coverage."""
    assert UNCOVERED, "if this list is empty, say so in the module docstring too"
