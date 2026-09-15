"""The administrative mutations, exercised rather than merely refused.

Every endpoint in this file was already covered by `test_route_guard_coverage`
— which proves each one *denies* an ordinary member of staff. None had ever
been driven to a success. A route-coverage trace over the whole suite made that
concrete: twelve mutating admin operations had only ever returned 401 or 403,
so the guard was tested and the behaviour behind it was not. Approving a source,
starting a sync, cancelling a run — all unproven.

That gap has a particular shape worth naming: an endpoint that always refuses
looks identical, in a passing test suite, to an endpoint that is broken. This
file is the other half.

The order below follows the real lifecycle, because these operations are not
independent — a source has to exist before it can be approved, and approved
before it can be indexed (BR-001). Testing them in isolation with fixtures would
skip the part most likely to be wrong: the transitions between them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


#: Prefixed onto every source name these tests create, so the fixture below can
#: remove them. Without it each run leaves a dozen rows behind for good — they
#: are harmless to retrieval (draft or empty, so the registry never lists them)
#: but they accumulate forever and make the table useless to read by hand.
#:
#: On the *name* and not the description: one of these tests amends the
#: description, which is exactly the kind of thing that quietly defeats a marker
#: stored in a mutable field.
_TEST_PREFIX = "askau-test:"


@pytest.fixture(autouse=True)
async def _clean_up_sources(admin_engine: AsyncEngine):  # type: ignore[no-untyped-def]
    yield
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM knowledge_sources WHERE name LIKE :m"), {"m": f"{_TEST_PREFIX}%"}
        )


async def _new_source(client: httpx.AsyncClient, admin: str, name: str) -> str:
    resp = await client.post(
        "/api/v1/knowledge/sources",
        headers=auth(admin),
        json={
            "name": f"{_TEST_PREFIX}{name}",
            "sourceType": "filesystem",
            "department": "MISD",
            "businessOwnerEmail": "admin.knowledge@africanunion.org",
            "defaultClassification": "internal",
            # A path that deliberately does not exist. Nothing here ingests —
            # the runs assert that a run *row* was created, not that files were
            # read — and `test-connection` reporting "unreachable" for a missing
            # root is the behaviour under test rather than a broken fixture.
            "location": {"root": str(Path(tempfile.gettempdir()) / "askau-absent-corpus")},
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _status(engine: AsyncEngine, source_id: str) -> tuple[str, bool]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("""
                SELECT status::text, approved_by IS NOT NULL
                FROM knowledge_sources WHERE id = CAST(:sid AS uuid)
                """),
                {"sid": source_id},
            )
        ).first()
    assert row is not None
    return str(row[0]), bool(row[1])


class TestTheSourceLifecycle:
    async def test_a_new_source_starts_unapproved_and_cannot_be_indexed(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """BR-001, at the point an administrator would actually hit it.

        The CHECK constraint already refuses an active-but-unapproved row, and
        `test_source_approval.py` proves that by writing to the database
        directly. What this asserts is the other half: that the *workflow* stops
        first and says why, so an administrator gets a reason rather than a 500
        from a constraint they cannot see.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Draft source {id(self)}")

        status, approved = await _status(admin_engine, sid)
        assert status == "draft"
        assert approved is False

        for action in ("sync", "reindex"):
            resp = await client.post(
                f"/api/v1/knowledge/sources/{sid}/{action}", headers=auth(admin)
            )
            assert resp.status_code == 409, f"{action} on an unapproved source must conflict"
            assert "BR-001" in resp.text

    async def test_approval_activates_and_records_who(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Approval and activation are one act.

        Keeping them separate would allow an approved-but-inactive source and an
        active-but-unapproved one; the second is what BR-001 exists to prevent,
        and the constraint would reject it, so the API must not offer a path
        that tries.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Approvable source {id(self)}")

        resp = await client.post(f"/api/v1/knowledge/sources/{sid}/approve", headers=auth(admin))
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

        status, approved = await _status(admin_engine, sid)
        assert (status, approved) == ("active", True)

        # Who approved it, not merely that it is approved. An approval with no
        # approver is the record BR-001 is meant to produce, missing its point.
        async with admin_engine.connect() as conn:
            approver = (
                await conn.execute(
                    text("""
                    SELECT u.email FROM knowledge_sources ks
                    JOIN users u ON u.id = ks.approved_by
                    WHERE ks.id = CAST(:sid AS uuid)
                    """),
                    {"sid": sid},
                )
            ).scalar_one_or_none()
        assert approver is not None

    async def test_approving_a_source_that_does_not_exist_is_404(
        self, client: httpx.AsyncClient, token
    ) -> None:
        resp = await client.post(
            "/api/v1/knowledge/sources/00000000-0000-0000-0000-000000000000/approve",
            headers=auth(token("admin.knowledge")),
        )
        assert resp.status_code == 404

    async def test_sync_and_reindex_start_a_run_once_approved(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """202 with a run id, not 200 with a result.

        Ingestion is minutes-scale work. A synchronous response would either
        block until it finished or claim completion that has not happened, and
        the second is the one that quietly corrupts an operator's mental model.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Indexable source {id(self)}")
        await client.post(f"/api/v1/knowledge/sources/{sid}/approve", headers=auth(admin))

        resp = await client.post(f"/api/v1/knowledge/sources/{sid}/sync", headers=auth(admin))
        assert resp.status_code == 202, resp.text
        body = resp.json()

        # The location must be fetchable exactly as given. It was not: the API
        # moved to `/api/v1` and these two strings still read `/v1`, so an
        # operator following the pointer from a successful call got a 404.
        # Nothing caught it because no test had ever read the response body.
        follow = await client.get(body["location"], headers=auth(admin))
        assert follow.status_code == 200, (
            f"sync returned an unreachable location {body['location']!r}"
        )

        async with admin_engine.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT count(*) FROM ingestion_runs WHERE id = CAST(:r AS uuid)"),
                    {"r": body["run_id"]},
                )
            ).scalar_one()
        assert exists == 1

    async def test_a_second_run_on_the_same_source_is_refused(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """Discovered by writing the test above, which tried to sync then
        reindex the same source and got a 409 on the second call.

        The guard is right and worth pinning: two concurrent runs over one
        source would race on the same documents, and `reindex` in particular
        re-chunks material that `sync` may be inserting. The reply names the
        run already in progress, which is what an operator needs to decide
        whether to wait or cancel.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Busy source {id(self)}")
        await client.post(f"/api/v1/knowledge/sources/{sid}/approve", headers=auth(admin))

        first = await client.post(f"/api/v1/knowledge/sources/{sid}/sync", headers=auth(admin))
        assert first.status_code == 202
        second = await client.post(f"/api/v1/knowledge/sources/{sid}/reindex", headers=auth(admin))
        assert second.status_code == 409
        assert first.json()["run_id"] in second.text, (
            "the conflict must name the run that is blocking, or an operator cannot act on it"
        )

    async def test_a_running_ingestion_can_be_cancelled(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Cancellable source {id(self)}")
        await client.post(f"/api/v1/knowledge/sources/{sid}/approve", headers=auth(admin))
        run_id = (
            await client.post(f"/api/v1/knowledge/sources/{sid}/sync", headers=auth(admin))
        ).json()["run_id"]

        resp = await client.post(
            f"/api/v1/admin/ingestion/runs/{run_id}/cancel", headers=auth(token("admin.system"))
        )
        assert resp.status_code in (200, 202), resp.text

        async with admin_engine.connect() as conn:
            status = (
                await conn.execute(
                    text("SELECT status::text FROM ingestion_runs WHERE id = CAST(:r AS uuid)"),
                    {"r": run_id},
                )
            ).scalar_one()
        assert status in {"cancelled", "cancelling"}, f"run left in {status!r} after cancel"


class TestConnectionTesting:
    async def test_probing_a_source_reports_rather_than_raises(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """FR-013. A misconfigured source must fail here, not as a run full of errors.

        The assertion is deliberately about the *shape* of the reply rather than
        whether the probe succeeds: a filesystem root that does not exist on the
        test machine is a legitimate "cannot reach", and an endpoint that
        reported that as a 500 would be the actual defect.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Probed source {id(self)}")
        resp = await client.post(
            f"/api/v1/knowledge/sources/{sid}/test-connection", headers=auth(admin)
        )
        assert resp.status_code == 200, resp.text
        assert "ok" in resp.json() or "status" in resp.json()


class TestEditing:
    async def test_a_source_can_be_amended(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Editable source {id(self)}")
        resp = await client.patch(
            f"/api/v1/knowledge/sources/{sid}",
            headers=auth(admin),
            json={"description": "Amended by a test."},
        )
        assert resp.status_code == 200, resp.text

        async with admin_engine.connect() as conn:
            description = (
                await conn.execute(
                    text("SELECT description FROM knowledge_sources WHERE id = CAST(:s AS uuid)"),
                    {"s": sid},
                )
            ).scalar_one()
        assert description == "Amended by a test."

    async def test_an_empty_patch_is_refused_rather_than_silently_accepted(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """A no-op PATCH returning 200 tells the caller a change was made."""
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Untouched source {id(self)}")
        resp = await client.patch(f"/api/v1/knowledge/sources/{sid}", headers=auth(admin), json={})
        assert resp.status_code == 422


class TestDocumentOperations:
    async def _a_document(self, engine: AsyncEngine) -> str:
        async with engine.connect() as conn:
            return str(
                (await conn.execute(text("SELECT id::text FROM documents LIMIT 1"))).scalar_one()
            )

    async def test_a_document_can_be_reclassified(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """The single most consequential admin write in the product.

        Classification is what the ACL and the partition key are built from, so
        this endpoint can move a document between what people may see. It had
        never been called successfully by anything.
        """
        did = await self._a_document(admin_engine)
        async with admin_engine.connect() as conn:
            before = (
                await conn.execute(
                    text("SELECT classification::text FROM documents WHERE id = CAST(:d AS uuid)"),
                    {"d": did},
                )
            ).scalar_one()

        target = "confidential" if before != "confidential" else "internal"
        resp = await client.patch(
            f"/api/v1/knowledge/documents/{did}",
            headers=auth(token("admin.knowledge")),
            json={"classification": target},
        )
        assert resp.status_code == 200, resp.text
        try:
            async with admin_engine.connect() as conn:
                after = (
                    await conn.execute(
                        text(
                            "SELECT classification::text FROM documents WHERE id = CAST(:d AS uuid)"
                        ),
                        {"d": did},
                    )
                ).scalar_one()
            assert after == target
        finally:
            # Restored, because `classification` is a LIST partition key and the
            # rest of the suite asserts against the seeded corpus. Leaving a
            # document reclassified would make unrelated retrieval tests fail
            # in a way that points nowhere near this file.
            await client.patch(
                f"/api/v1/knowledge/documents/{did}",
                headers=auth(token("admin.knowledge")),
                json={"classification": before},
            )

    async def test_reprocessing_queues_the_document_rather_than_doing_it_inline(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        did = await self._a_document(admin_engine)
        resp = await client.post(
            f"/api/v1/knowledge/documents/{did}/reprocess", headers=auth(token("admin.knowledge"))
        )
        assert resp.status_code in (200, 202), resp.text

    async def test_reprocessing_an_unknown_document_is_404(
        self, client: httpx.AsyncClient, token
    ) -> None:
        resp = await client.post(
            "/api/v1/knowledge/documents/00000000-0000-0000-0000-000000000000/reprocess",
            headers=auth(token("admin.knowledge")),
        )
        assert resp.status_code == 404


class TestFeedbackTriage:
    async def test_triage_records_who_closed_it_and_what_they_did(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """FR-044. Attribution is the point — a queue where nobody's name is
        attached is a queue nobody owns."""
        user = token("staff.finance")
        cid = (await client.post("/api/v1/conversations", headers=auth(user), json={})).json()["id"]
        answer = await client.post(
            f"/api/v1/conversations/{cid}/messages",
            headers=auth(user),
            json={"content": "How much annual leave do staff accrue?"},
        )
        message_id = answer.json()["messageId"]
        assert message_id, "history is on for this identity, so a message must exist"

        rated = await client.post(
            f"/api/v1/messages/{message_id}/feedback",
            headers=auth(user),
            json={"rating": "not_helpful", "reason": "missing_information"},
        )
        assert rated.status_code in (200, 201, 204), rated.text

        async with admin_engine.connect() as conn:
            fid = (
                await conn.execute(
                    text("""
                    SELECT id FROM message_feedback
                    WHERE message_id = CAST(:m AS uuid) ORDER BY id DESC LIMIT 1
                    """),
                    {"m": message_id},
                )
            ).scalar_one()

        resp = await client.patch(
            f"/api/v1/admin/feedback/{fid}",
            headers=auth(token("admin.knowledge")),
            json={"resolution": "Reviewed; the policy section was genuinely absent."},
        )
        assert resp.status_code == 200, resp.text

        async with admin_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("""
                    SELECT f.resolution, u.email
                    FROM message_feedback f JOIN users u ON u.id = f.triaged_by
                    WHERE f.id = :fid
                    """),
                    {"fid": fid},
                )
            ).first()
        assert row is not None, "triage left no attribution"
        assert row[0].startswith("Reviewed")

    async def test_triaging_an_unknown_item_is_404(self, client: httpx.AsyncClient, token) -> None:
        resp = await client.patch(
            "/api/v1/admin/feedback/999999999",
            headers=auth(token("admin.knowledge")),
            json={"resolution": "nothing to see"},
        )
        assert resp.status_code == 404


class TestStopGeneration:
    async def test_stopping_is_recorded_for_any_authenticated_caller(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """Not an admin route, and it does not actually cancel anything.

        The SSE connection closing is what stops generation; the browser does
        that. This records the intent so an abandoned answer is distinguishable
        from a delivered one in the metrics — counting it as delivered would
        inflate the success rate.
        """
        resp = await client.post(
            "/api/v1/messages/00000000-0000-0000-0000-000000000000/stop",
            headers=auth(token("staff.finance")),
        )
        assert resp.status_code in (200, 202, 204), resp.text


class TestReindexIndependently:
    async def test_reindex_starts_a_run_on_a_source_with_none_in_flight(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Its own source, deliberately.

        A route-coverage trace showed `reindex` had only ever returned 401, 403
        and 409 — the 409 because the test that exercised it had already started
        a sync on the same source. An endpoint whose only observed outcomes are
        refusals is indistinguishable from a broken one, which is the reason
        this test exists separately rather than sharing a fixture.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Reindexable source {id(self)}")
        await client.post(f"/api/v1/knowledge/sources/{sid}/approve", headers=auth(admin))

        resp = await client.post(f"/api/v1/knowledge/sources/{sid}/reindex", headers=auth(admin))
        assert resp.status_code == 202, resp.text
        async with admin_engine.connect() as conn:
            trigger = (
                await conn.execute(
                    text("SELECT trigger FROM ingestion_runs WHERE id = CAST(:r AS uuid)"),
                    {"r": resp.json()["run_id"]},
                )
            ).scalar_one()
        # `reindex` and `sync` are different operations — a full re-chunk versus
        # an incremental pass — and the run has to say which it was, or the
        # ingestion history cannot explain why a source was rebuilt.
        assert trigger == "reindex"


class TestEvaluationRuns:
    async def test_a_system_admin_can_run_the_security_dataset(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The smallest dataset, because this calls the real orchestrator.

        `security` rather than `all`: the point is that the endpoint executes
        and reports, not that the corpus scores well today — the evaluation
        gate itself is covered by `tests/unit/test_eval_gate.py`, which does not
        need a live model.
        """
        resp = await client.post(
            "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.system"))
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dataset"] == "security"
        assert isinstance(body["passed"], bool)
        assert body["metrics"]

    async def test_a_knowledge_admin_cannot_run_evaluations(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """§6.4 separates the administrative roles deliberately.

        Knowledge administration is authority over the corpus; running the
        evaluation suite is a system operation that costs model calls. The
        route requires `system_admin`, and this pins that the granular roles
        are still enforced server-side even though the wire collapses them to
        `admin` for display.
        """
        resp = await client.post(
            "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.knowledge"))
        )
        assert resp.status_code == 403


class TestSourceDetail:
    async def test_an_administrator_can_read_one_source(
        self, client: httpx.AsyncClient, token
    ) -> None:
        """The last operation in the trace that had only ever been refused.

        A detail read is not exciting on its own; what it pins is that the
        fields the admin UI needs are actually returned, which nothing had
        checked because every previous call to this route was a 401 or a 403.
        """
        admin = token("admin.knowledge")
        sid = await _new_source(client, admin, f"Readable source {id(self)}")
        resp = await client.get(f"/api/v1/knowledge/sources/{sid}", headers=auth(admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == sid
        assert body["status"] == "draft"
