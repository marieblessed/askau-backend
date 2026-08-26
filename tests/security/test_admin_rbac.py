"""Administration console authorization (NFR-004b, FR-051, BR-008).

Every admin surface is gated server-side. The console hiding a link is a
courtesy; these tests cover the control.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.security]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


#: Who may reach what. Roles are deliberately separate: an administrator who
#: manages ingestion has no need to read who accessed which document, and the
#: reverse holds too.
MATRIX: list[tuple[str, str, int]] = [
    # end users reach nothing administrative
    ("staff.misd", "/v1/admin/overview", 403),
    ("staff.misd", "/v1/admin/documents", 403),
    ("staff.misd", "/v1/admin/ingestion/runs", 403),
    ("staff.misd", "/v1/admin/usage", 403),
    ("staff.misd", "/v1/security/audit-events", 403),
    # knowledge admin: the knowledge base, not usage or audit
    ("admin.knowledge", "/v1/admin/overview", 200),
    ("admin.knowledge", "/v1/admin/documents", 200),
    ("admin.knowledge", "/v1/admin/ingestion/runs", 200),
    ("admin.knowledge", "/v1/admin/usage", 403),
    ("admin.knowledge", "/v1/security/audit-events", 403),
    # system admin: adds usage, still not the audit log
    ("admin.system", "/v1/admin/usage", 200),
    ("admin.system", "/v1/security/audit-events", 403),
    # security admin: the audit log, and *only* the audit log
    ("admin.security", "/v1/security/audit-events", 200),
    ("admin.security", "/v1/security/audit-events/export", 200),
    ("admin.security", "/v1/admin/overview", 403),
    ("admin.security", "/v1/admin/usage", 403),
]


@pytest.mark.parametrize(("user", "path", "expected"), MATRIX)
async def test_role_matrix(
    client: httpx.AsyncClient, token, user: str, path: str, expected: int
) -> None:
    resp = await client.get(path, headers=auth(token(user)))
    assert resp.status_code == expected, f"{user} -> {path}"


async def test_unauthenticated_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/admin/overview")).status_code == 401


class TestAuditIsAppendOnly:
    """BR-008. The log must be trustworthy to a reviewer who does not trust the
    application, so there is no mutation path at any level."""

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    async def test_no_mutation_endpoint_exists(
        self, client: httpx.AsyncClient, token, method: str
    ) -> None:
        resp = await getattr(client, method)(
            "/v1/security/audit-events", headers=auth(token("admin.security"))
        )
        # 405 (no such method) or 404 — never 2xx.
        assert resp.status_code >= 400

    async def test_database_denies_update_even_to_the_app_role(self, engine) -> None:
        """The grant, not the routing table, is what makes this hold."""
        from sqlalchemy import text

        async with engine.connect() as conn:
            privileges = (
                await conn.execute(
                    text("""
                    SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)
                    FROM information_schema.role_table_grants
                    WHERE grantee = 'askau_app' AND table_name = 'audit_events'
                    """)
                )
            ).scalar_one()
        assert privileges == "INSERT,SELECT"


class TestAdminSurfacesExcludeUserContent:
    """§6.5 restricts administrator access to conversations.

    An administrator troubleshooting ingestion has no business reading what
    staff asked. These endpoints return platform state only.
    """

    async def test_overview_returns_no_content_fields(
        self, client: httpx.AsyncClient, token
    ) -> None:
        body = (
            await client.get("/v1/admin/overview", headers=auth(token("admin.knowledge")))
        ).text.lower()
        for leaked in ("question", "answer", "conversation", "message_content"):
            assert leaked not in body

    async def test_audit_rows_carry_no_question_text(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """FR-052: audit records that a query happened and which documents were
        retrieved — never the wording of the question."""
        asker = token("staff.misd")
        await client.post(
            "/v1/ask",
            headers=auth(asker),
            json={"content": "What is the annual leave entitlement for probation?"},
        )
        body = (
            await client.get(
                "/v1/security/audit-events?limit=200",
                headers=auth(token("admin.security")),
            )
        ).text
        assert "annual leave entitlement for probation" not in body
