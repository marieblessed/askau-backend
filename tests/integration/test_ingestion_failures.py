"""The ingestion failure rollup (FR-049, FR-050).

Per-run task lists answer "what went wrong in this run". This endpoint answers
the question asked across runs: what is systematically keeping documents out of
the corpus. `no_text_layer` is singled out because it is a decision input — the
number of documents OCR would recover, which is what makes "is OCR worth
installing" a measurement rather than a guess.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


@pytest_asyncio.fixture
async def failed_tasks(admin_engine: AsyncEngine) -> AsyncIterator[dict[str, int]]:
    """Three scanned documents and one oversized one, then cleaned up.

    Written directly rather than driven through the pipeline: the assertion is
    about aggregation, and running four real ingestions to produce four rows
    would test the pipeline again and this endpoint barely.
    """
    planted = {"no_text_layer": 3, "too_large": 1}
    run_id = uuid.uuid4()

    async with admin_engine.begin() as conn:
        source_id = (
            await conn.execute(text("SELECT id FROM knowledge_sources LIMIT 1"))
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO ingestion_runs (id, source_id, trigger, status, started_at) "
                "VALUES (:id, :src, 'manual', 'failed', now())"
            ),
            {"id": run_id, "src": source_id},
        )
        for code, count in planted.items():
            for n in range(count):
                await conn.execute(
                    text(
                        "INSERT INTO ingestion_tasks "
                        "  (run_id, external_key, stage, attempts, error_code, updated_at) "
                        "VALUES (:run, :key, 'failed', 1, :code, now())"
                    ),
                    {
                        "run": run_id,
                        "key": f"fixture-{code}-{n}",
                        "code": code,
                    },
                )

    yield planted

    async with admin_engine.begin() as conn:
        await conn.execute(text("DELETE FROM ingestion_tasks WHERE run_id = :run"), {"run": run_id})
        await conn.execute(text("DELETE FROM ingestion_runs WHERE id = :run"), {"run": run_id})


class TestFailureRollup:
    async def test_groups_by_cause_and_counts(
        self, client: httpx.AsyncClient, token, failed_tasks: dict[str, int]
    ) -> None:
        body = (
            await client.get(
                "/v1/admin/ingestion/failures",
                headers=auth(token("admin.knowledge")),
            )
        ).json()

        counts = {f["error_code"]: f["documents"] for f in body["failures"]}
        for code, expected in failed_tasks.items():
            assert counts.get(code) == expected, f"{code}: expected {expected}, got {counts}"

    async def test_surfaces_what_ocr_would_recover(
        self, client: httpx.AsyncClient, token, failed_tasks: dict[str, int]
    ) -> None:
        """The count that decides whether OCR is worth installing.

        It must be the scanned documents alone — an administrator reading this
        as "documents OCR would recover" and getting the total failure count
        would install a system dependency to fix problems it cannot touch.
        """
        body = (
            await client.get(
                "/v1/admin/ingestion/failures",
                headers=auth(token("admin.knowledge")),
            )
        ).json()

        assert body["ocr_would_recover"] == failed_tasks["no_text_layer"]
        assert body["ocr_would_recover"] < body["total_failed"], (
            "too_large was planted precisely so this cannot pass by returning the total"
        )

    async def test_every_cause_carries_an_action(
        self, client: httpx.AsyncClient, token, failed_tasks: dict[str, int]
    ) -> None:
        """A console that names a failure code and stops has moved the work,
        not done it."""
        body = (
            await client.get(
                "/v1/admin/ingestion/failures",
                headers=auth(token("admin.knowledge")),
            )
        ).json()

        for failure in body["failures"]:
            assert failure["remedy"].strip()
            assert "No remedy recorded" not in failure["remedy"], (
                f"{failure['error_code']} has no remedy — add one to _REMEDIES"
            )

    async def test_end_user_is_refused(self, client: httpx.AsyncClient, token) -> None:
        resp = await client.get(
            "/v1/admin/ingestion/failures", headers=auth(token("staff.finance"))
        )
        assert resp.status_code == 403
