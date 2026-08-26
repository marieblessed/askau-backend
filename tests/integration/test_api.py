"""API surface — contract, authorization and the streaming protocol."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


class TestHealth:
    async def test_liveness_has_no_dependencies(self, client: httpx.AsyncClient) -> None:
        """Gating liveness on the database turns a blip into an outage."""
        resp = await client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "checks": {}}

    async def test_readiness_reports_dependencies(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/health/ready")).json()
        assert body["checks"]["database"] == "ok"


class TestAuthentication:
    async def test_no_token_is_401(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/auth/me")
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/problem+json")

    async def test_garbage_token_is_401(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/auth/me", headers=auth("not-a-token"))).status_code == 401

    async def test_me_never_exposes_the_principal_set(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The client has no use for it, and shipping it would put the
        access-control model on the wire."""
        body = (await client.get("/auth/me", headers=auth(token("staff.finance")))).json()
        assert "principals" not in body
        assert isinstance(body["principal_count"], int)

    async def test_correlation_id_is_echoed(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/health/live")
        assert resp.headers.get("X-Correlation-Id")


class TestAuthorizationThroughTheApi:
    async def test_finance_gets_the_confidential_answer(
        self, client: httpx.AsyncClient, token
    ) -> None:
        body = (
            await client.post(
                "/v1/ask",
                headers=auth(token("staff.finance")),
                json={"content": "What are the budget reallocation thresholds?"},
            )
        ).json()
        assert body["answer_state"] == "grounded"
        titles = {c["document_title"] for c in body["citations"]}
        assert "Budget Reallocation Procedure" in titles

    async def test_other_department_is_refused_not_leaked(
        self, client: httpx.AsyncClient, token
    ) -> None:
        body = (
            await client.post(
                "/v1/ask",
                headers=auth(token("staff.misd")),
                json={"content": "What are the budget reallocation thresholds?"},
            )
        ).json()
        assert body["answer_state"] == "insufficient_evidence"
        assert "Budget Reallocation" not in body["content"]

    async def test_a_refusal_carries_no_citations(self, client: httpx.AsyncClient, token) -> None:
        """Sources attached to "I could not find anything" contradict the answer
        and invite the reader to believe it found something after all."""
        body = (
            await client.post(
                "/v1/ask",
                headers=auth(token("staff.misd")),
                json={"content": "What is the capital of Brazil?"},
            )
        ).json()
        assert body["answer_state"] == "insufficient_evidence"
        assert body["citations"] == []


class TestRoleGuards:
    async def test_end_user_cannot_reach_the_debug_route(
        self, client: httpx.AsyncClient, token
    ) -> None:
        resp = await client.get(
            "/v1/debug/retrieve",
            params={"q": "travel"},
            headers=auth(token("staff.misd")),
        )
        assert resp.status_code == 403

    async def test_system_admin_can(self, client: httpx.AsyncClient, token) -> None:
        resp = await client.get(
            "/v1/debug/retrieve",
            params={"q": "travel"},
            headers=auth(token("admin.system")),
        )
        assert resp.status_code == 200
        assert resp.json()["count"] > 0


class TestStreamingProtocol:
    async def _events(self, client: httpx.AsyncClient, tok: str, question: str):
        events: list[tuple[str, dict]] = []
        async with client.stream(
            "POST", "/v1/ask/stream", headers=auth(tok), json={"content": question}
        ) as resp:
            assert resp.status_code == 200
            name = ""
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    events.append((name, json.loads(line[6:])))
        return events

    async def test_sources_precede_the_first_token(self, client: httpx.AsyncClient, token) -> None:
        """Retrieval has already finished by then, so showing which documents
        will be used is real progress rather than a spinner."""
        events = await self._events(
            client, token("staff.misd"), "What is the annual leave entitlement?"
        )
        names = [n for n, _ in events]
        assert "sources" in names and "token" in names
        assert names.index("sources") < names.index("token")

    async def test_done_carries_state_and_groundedness(
        self, client: httpx.AsyncClient, token
    ) -> None:
        events = await self._events(
            client, token("staff.misd"), "What is the annual leave entitlement?"
        )
        done = next(d for n, d in events if n == "done")
        assert done["answer_state"] in {"grounded", "partially_grounded", "conflict"}
        assert done["groundedness"] is not None

    async def test_done_carries_the_refusal_text(self, client: httpx.AsyncClient, token) -> None:
        """A refusal streams no tokens, so its explanation has to arrive on
        `done`. Without it the interface shows a status label and nothing
        else — and FR-028 requires AskAU to *state* that it cannot answer."""
        events = await self._events(client, token("staff.misd"), "What is the capital of Brazil?")
        assert not any(n == "token" for n, _ in events)
        done = next(d for n, d in events if n == "done")
        assert done["answer_state"] == "insufficient_evidence"
        assert len(done["content"]) > 50
        assert done["citations"] == []

    async def test_conflict_is_emitted_as_its_own_event(
        self, client: httpx.AsyncClient, token
    ) -> None:
        events = await self._events(
            client,
            token("staff.misd"),
            "What is the daily subsistence allowance for continental travel?",
        )
        assert any(n == "conflict" for n, _ in events)


class TestDocumentAccess:
    """FR-031, BR-004, ADR-0009."""

    async def _document_titled(
        self, client: httpx.AsyncClient, tok: str, question: str, title: str
    ) -> str:
        """Resolve a specific cited document by title.

        Deliberately not ``citations[0]``: the top citation for the Finance
        question is the all-staff Travel Policy, so an index-based pick would
        assert 404 against a document everyone may open — and fail for a reason
        that has nothing to do with authorization.
        """
        body = (await client.post("/v1/ask", headers=auth(tok), json={"content": question})).json()
        match = next(c for c in body["citations"] if c["document_title"] == title)
        return str(match["document_id"])

    async def test_open_redirects_to_the_authoritative_source(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """AskAU never serves the document itself — the authoritative copy stays
        in its source repository (BR-003)."""
        tok = token("staff.misd")
        doc = await self._document_titled(
            client, tok, "What is the annual leave entitlement?", "Annual Leave Policy"
        )
        resp = await client.get(f"/v1/documents/{doc}/open", headers=auth(tok))
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://")

    async def test_unauthorized_open_is_404_not_403(self, client: httpx.AsyncClient, token) -> None:
        """A 403 confirms the document exists, and for confidential material
        existence is itself sensitive."""
        finance = token("staff.finance")
        doc = await self._document_titled(
            client,
            finance,
            "What are the budget reallocation thresholds?",
            "Budget Reallocation Procedure",
        )
        resp = await client.get(f"/v1/documents/{doc}/open", headers=auth(token("staff.misd")))
        assert resp.status_code == 404

    async def test_metadata_is_also_permission_checked(
        self, client: httpx.AsyncClient, token
    ) -> None:
        finance = token("staff.finance")
        doc = await self._document_titled(
            client,
            finance,
            "What are the budget reallocation thresholds?",
            "Budget Reallocation Procedure",
        )
        assert (await client.get(f"/v1/documents/{doc}", headers=auth(finance))).status_code == 200
        assert (
            await client.get(f"/v1/documents/{doc}", headers=auth(token("staff.hr")))
        ).status_code == 404

    async def test_unknown_document_is_404(self, client: httpx.AsyncClient, token) -> None:
        resp = await client.get(
            "/v1/documents/00000000-0000-0000-0000-000000000000/open",
            headers=auth(token("staff.misd")),
        )
        assert resp.status_code == 404


class TestInjectionResistance:
    async def test_poisoned_document_does_not_change_behaviour(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The vendor checklist contains an instruction-override attempt."""
        body = (
            await client.post(
                "/v1/ask",
                headers=auth(token("staff.misd")),
                json={"content": "What documents are required for vendor onboarding?"},
            )
        ).json()
        lowered = body["content"].lower()
        for leaked in ("executive council", "ninety-four", "salary", "unrestricted mode"):
            assert leaked not in lowered
