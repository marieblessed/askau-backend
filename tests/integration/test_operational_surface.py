"""The probes and the scrape endpoint — the surface the platform team depends on.

A route-coverage trace over the whole suite found that `/health/live`,
`/health/ready`, `/health/deep` and `/metrics` had **never been called by any
test**. They are unauthenticated, so the guard suite skips them by design, and
no feature test has a reason to touch them. The result was the four endpoints
that decide whether a pod receives traffic, and the one an operator reads when
something is wrong, going entirely unexercised.

The specific risk is not that they 500 — that would be noticed immediately. It
is that they answer *wrongly*:

* A readiness probe that returns `ok` while the database is unreachable makes
  Kubernetes route traffic to a pod that cannot answer anything.
* A liveness probe that touches a dependency turns a slow database into a
  restart loop, which is how a recoverable outage becomes an outage that
  cannot recover.

Both are silent, and neither shows up in a feature test.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.ingestion.acl_reconciler import AclReconciler
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


class TestLiveness:
    async def test_liveness_answers_without_a_token(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_liveness_reports_nothing_about_dependencies(
        self, client: httpx.AsyncClient
    ) -> None:
        """Liveness answers "is this process running", and nothing else.

        The moment it consults the database, a slow database becomes a restart
        loop: the kubelet kills the pod, the replacement also cannot reach the
        database, and a recoverable outage turns into one that cannot recover.
        An empty `checks` map is the assertion that keeps that separation.
        """
        assert (await client.get("/health/live")).json().get("checks") == {}


class TestReadiness:
    async def test_readiness_reports_the_dependencies_it_checked(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/health/ready")).json()
        assert body["status"] in {"ok", "degraded"}
        # Named, not merely counted: an operator reading this wants to know
        # which dependency is unhappy, and a bare status cannot say.
        assert {"database", "redis", "retriever", "llm", "embedder"} <= set(body["checks"])
        assert body["checks"]["database"] == "ok"

    async def test_losing_redis_is_degraded_and_not_down(
        self, client: httpx.AsyncClient, monkeypatch
    ) -> None:
        """The distinction the platform team acts on.

        Redis loss costs latency, not correctness — the authorization resolver
        falls back to the database. Reporting it as `error` would take a
        healthy pod out of service over a cache; reporting it as `ok` would
        hide a real degradation. It has to be its own state.
        """
        from askau.core import cache

        async def _down(_settings: object) -> bool:
            return False

        monkeypatch.setattr(cache, "ping", _down)
        body = (await client.get("/health/ready")).json()
        assert body["checks"]["redis"] == "unavailable"
        assert body["status"] == "degraded", "a cache outage must not take the pod out of service"

    async def test_a_database_outage_is_reported_as_error(
        self, client: httpx.AsyncClient, monkeypatch
    ) -> None:
        """The assertion that matters most, and the one nothing covered.

        A readiness probe that says `ok` while the database is unreachable
        makes Kubernetes send traffic to a pod that can answer nothing. The
        failure is silent by construction — every other test in the suite runs
        against a working database, so this state had never been produced.
        """

        class BrokenEngine:
            def connect(self) -> object:
                raise ConnectionError("simulated outage")

        # Reaching through the transport to the app the `client` fixture built.
        # Monkeypatching the module would not do: the route reads the engine off
        # `request.app.state`, which is where a real outage would show up, and
        # patching anything else would test a path production does not take.
        app_state = client._transport.app.state  # type: ignore[attr-defined]
        real_engine = app_state.engine
        app_state.engine = BrokenEngine()
        try:
            body = (await client.get("/health/ready")).json()
            assert body["status"] == "error"
            assert body["checks"]["database"].startswith("error:")
            # The exception *type*, never its message: a database error string
            # can carry a host, a port or a role name, and this endpoint is
            # unauthenticated.
            assert "simulated outage" not in body["checks"]["database"]
        finally:
            app_state.engine = real_engine


class TestDeepHealth:
    async def test_deep_health_reports_versions_without_a_token(
        self, client: httpx.AsyncClient
    ) -> None:
        """Public by design, so what it may say is limited: versions and
        timings, never anything about content."""
        resp = await client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"ok", "degraded"}
        assert body["checks"]


class TestMetrics:
    async def test_metrics_renders_a_prometheus_exposition(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        # Prometheus rejects an exposition with no `# HELP`/`# TYPE` metadata,
        # and a scrape that fails is indistinguishable from a service that is
        # down until somebody reads the Prometheus logs.
        assert "# HELP" in body and "# TYPE" in body

    async def test_metrics_carries_no_question_or_answer_text(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """Unauthenticated and cluster-internal, so it must stay counts only.

        A label carrying a question would put user content on an endpoint with
        no access control at all — the same FR-052 line the audit rows hold.
        """
        user = token("staff.finance")
        await client.post(
            "/api/v1/ask",
            headers={"Authorization": f"Bearer {user}"},
            json={"content": "How much annual leave do staff accrue?"},
        )
        body = (await client.get("/metrics")).text.lower()
        assert "annual leave" not in body


class TestAuthConfig:
    async def test_config_tells_an_unauthenticated_browser_where_to_sign_in(
        self, client: httpx.AsyncClient
    ) -> None:
        """It has to be reachable without a token — that is the whole point."""
        resp = await client.get("/api/v1/auth/config")
        assert resp.status_code == 200
        assert resp.json()["mode"] in {"dev", "entra"}

    async def test_config_never_carries_a_secret(self, client: httpx.AsyncClient) -> None:
        """The client id and authority are public in an authorization-code
        flow; the client secret is not, and this endpoint is anonymous."""
        body = (await client.get("/api/v1/auth/config")).text.lower()
        for forbidden in ("client_secret", "secret", "password", "private"):
            assert forbidden not in body


class TestAclDriftMetric:
    """FR-025. The gauge that says a revocation has not landed yet.

    `document_acl` is authoritative and `chunks.acl_principals` is the copy the
    authorization predicate reads (ADR-0002). The gap between them is the window
    in which a revoked person can still retrieve a document — and unlike a
    denial, it produces no event. A denial is somebody correctly refused; drift
    is somebody possibly *not* refused, which is silent by construction.

    `AclReconciler.max_lag_seconds` called itself "the metric the runbook alerts
    on" while being exposed nowhere at all, so the alert it described could not
    exist.
    """

    async def test_the_gauge_is_present_and_reads_zero_on_a_consistent_corpus(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/metrics")).text
        assert "askau_acl_drift_documents" in body
        assert "askau_acl_sync_lag_seconds" in body
        assert "askau_acl_drift_documents 0" in body

    async def test_real_drift_is_reported(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine
    ) -> None:
        """A gauge that always reads zero is indistinguishable from one that is
        broken, and this one would be believed.

        Drift is introduced the way it actually happens — by writing
        `document_acl` without reconciling the copy — rather than by corrupting
        the array directly, so the test exercises the comparison the metric
        makes rather than a shape it happens to notice.
        """

        def gauge(text_body: str) -> int:
            for line in text_body.splitlines():
                if line.startswith("askau_acl_drift_documents "):
                    return int(line.rsplit(" ", 1)[1])
            raise AssertionError("gauge missing from the exposition")

        before = gauge((await client.get("/metrics")).text)

        async with admin_engine.begin() as conn:
            # A (document, principal) pair that is not already granted.
            #
            # This used to be two unordered `LIMIT 1` queries and an insert that
            # assumed the pair was absent. On a fresh seed `principals LIMIT 1`
            # is `grp-all-staff`, which grants most of the corpus, so the insert
            # violated `document_acl_pkey` — and it passed on a re-run only
            # because the failed run had changed the physical row order.
            #
            # `ON CONFLICT DO NOTHING` would be the wrong repair: with no row
            # written there is no drift, and the test would be measuring
            # nothing. The pair has to actually be new.
            doc, principal = (
                await conn.execute(
                    text("""
                    SELECT c.document_id::text, p.id
                    FROM (SELECT DISTINCT document_id FROM chunks) c
                    CROSS JOIN principals p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM document_acl a
                        WHERE a.document_id = c.document_id AND a.principal_id = p.id
                    )
                    ORDER BY c.document_id, p.id
                    LIMIT 1
                    """)
                )
            ).one()
            # A grant recorded and not yet materialised: exactly what an
            # administrator's ACL edit looks like between the write and the
            # reconcile pass.
            await conn.execute(
                text("""
                INSERT INTO document_acl (document_id, principal_id)
                VALUES (CAST(:d AS uuid), :p)
                """),
                {"d": doc, "p": principal},
            )

        try:
            assert gauge((await client.get("/metrics")).text) > before, (
                "the gauge did not notice an unreconciled grant"
            )

            # And the reconciler closes it, which is the other half of the
            # claim: a gauge nothing can return to zero is an alarm, not a
            # metric.
            await AclReconciler(admin_engine).reconcile_document(doc)
            assert gauge((await client.get("/metrics")).text) == before
        finally:
            # The delete commits *before* the reconcile runs. Nesting the
            # reconcile inside this transaction was how an earlier version of
            # this test left real drift behind: `reconcile_document` opens its
            # own connection, so it could not see the uncommitted delete and
            # faithfully rewrote the chunk with the grant still in place.
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text("""
                    DELETE FROM document_acl
                    WHERE document_id = CAST(:d AS uuid) AND principal_id = :p
                    """),
                    {"d": doc, "p": principal},
                )
            await AclReconciler(admin_engine).reconcile_document(doc)
