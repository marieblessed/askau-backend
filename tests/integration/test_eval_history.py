"""Evaluation runs are recorded, so a score has something to be read against.

`eval_runs` and `eval_results` have existed since the first migration and
nothing had ever written to them. The harness could fail a build on today's
score and could not answer the question anybody actually asks after a change:
**did this make retrieval worse?**

What these tests hold is not "a row was written" but the two properties that
make the row worth having:

* **Comparability.** `git_sha`, `model`, `prompt_hash` and `retriever` decide
  whether two runs can be compared at all. A drop in mean reciprocal rank means
  something if those match and nothing if they do not — comparing a `bge-m3` run
  against a hash-embedding run yields a confident, meaningless regression.
* **A stable series.** Re-running a dataset must extend the history, not
  multiply the questions. A question that got a new row each run would break
  every per-question trend and grow the table without bound.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.evaluation.history import git_sha, prompt_hash
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


class TestRunsArePersisted:
    async def test_a_run_writes_itself_and_a_row_per_question(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        before_runs = await _count(admin_engine, "SELECT count(*) FROM eval_runs")
        resp = await client.post(
            "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.system"))
        )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
        assert run_id, "the run was not recorded; the endpoint returns None when it cannot save"

        assert await _count(admin_engine, "SELECT count(*) FROM eval_runs") == before_runs + 1
        results = await _count(
            admin_engine,
            "SELECT count(*) FROM eval_results WHERE run_id = CAST(:r AS uuid)",
            r=run_id,
        )
        assert results == resp.json()["metrics"]["questions"]

    async def test_the_run_records_what_makes_it_comparable(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """The four fields without which the row is a number, not a measurement.

        `prompt_hash` is the one that earns its place least obviously: the
        system prompt is a file that ships with the wheel and can change with no
        commit that looks related to retrieval. Recording it turns "scores moved
        and nobody knows why" into a one-line diff.
        """
        run_id = (
            await client.post(
                "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.system"))
            )
        ).json()["run_id"]

        async with admin_engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("""
                    SELECT git_sha, model, prompt_hash, retriever, finished_at, passed
                    FROM eval_runs WHERE id = CAST(:r AS uuid)
                    """),
                        {"r": run_id},
                    )
                )
                .mappings()
                .one()
            )

        assert row["git_sha"] == git_sha()
        assert row["prompt_hash"] == prompt_hash()
        # Provider *and* model. "gpt-4" alone does not distinguish a run against
        # Azure from one against a local Ollama, and those are not comparable.
        assert ":" in row["model"]
        assert row["retriever"]
        assert row["finished_at"] is not None
        assert row["passed"] is not None

    async def test_a_second_run_extends_the_series_without_duplicating_questions(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Re-running a dataset is the normal case, not the exception.

        Questions are upserted on the harness's own id rather than inserted, so
        correcting a question's wording keeps its history. A renamed question
        starting a fresh series would read as a fixed bug and a new failure at
        the same time.
        """
        tok = token("admin.system")
        await client.post("/api/v1/evaluation/runs?dataset=security", headers=auth(tok))
        questions = await _count(
            admin_engine,
            """
            SELECT count(*) FROM eval_questions q JOIN eval_datasets d ON d.id = q.dataset_id
            WHERE d.name = 'harness-security'
            """,
        )
        runs = await _count(admin_engine, "SELECT count(*) FROM eval_runs")

        await client.post("/api/v1/evaluation/runs?dataset=security", headers=auth(tok))
        assert (
            await _count(
                admin_engine,
                """
                SELECT count(*) FROM eval_questions q JOIN eval_datasets d ON d.id = q.dataset_id
                WHERE d.name = 'harness-security'
                """,
            )
            == questions
        )
        assert await _count(admin_engine, "SELECT count(*) FROM eval_runs") == runs + 1

    async def test_per_question_scores_are_kept_not_only_the_aggregate(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """An aggregate says quality moved; the per-question rows say which
        question moved, which is the only form of that news anyone can act on."""
        run_id = (
            await client.post(
                "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.system"))
            )
        ).json()["run_id"]

        async with admin_engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                    SELECT scores, passed, actual_state FROM eval_results
                    WHERE run_id = CAST(:r AS uuid)
                    """),
                        {"r": run_id},
                    )
                )
                .mappings()
                .all()
            )

        assert rows
        for row in rows:
            assert "latency_ms" in row["scores"]
            assert isinstance(row["passed"], bool)


class TestWhatIsDeliberatelyNotStored:
    async def test_generated_answers_are_not_kept(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """`actual_answer` stays NULL for curated questions.

        The answer is reproducible by re-running the question, and storing
        generated text for every question of every run would grow the table
        steadily without recording a fact the scores do not already carry.
        """
        run_id = (
            await client.post(
                "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.system"))
            )
        ).json()["run_id"]
        stored = await _count(
            admin_engine,
            "SELECT count(*) FROM eval_results "
            "WHERE run_id = CAST(:r AS uuid) AND actual_answer IS NOT NULL",
            r=run_id,
        )
        assert stored == 0


class TestRecordingIsNeverFatal:
    async def test_a_failure_to_record_does_not_fail_the_run(
        self, client: httpx.AsyncClient, token, monkeypatch
    ) -> None:
        """A measurement that cannot be saved is still a measurement.

        Turning a full disk into a red build that looks like a retrieval
        regression would waste exactly the time this table exists to save. The
        endpoint says so honestly by returning a null `run_id` rather than
        pretending the run was stored.
        """
        from askau.evaluation import history as history_mod

        async def _boom(self, *a, **kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated storage failure")

        # Patched *inside* `record`, not over it. Replacing `record` itself
        # would test the monkeypatch rather than the guard — the guarantee is
        # that a failure during the write is swallowed, so the failure has to
        # happen during the write.
        monkeypatch.setattr(history_mod.EvalHistory, "_upsert_questions", _boom, raising=True)
        resp = await client.post(
            "/api/v1/evaluation/runs?dataset=security", headers=auth(token("admin.system"))
        )
        assert resp.status_code == 200, "a storage failure must not fail the evaluation"
        # Null rather than absent, and rather than a fabricated id: an operator
        # comparing runs needs to know this one is missing from the series.
        assert resp.json()["run_id"] is None
        assert resp.json()["metrics"]["questions"] > 0


class TestHistoryEndpoint:
    async def test_history_returns_runs_newest_first_with_their_provenance(
        self, client: httpx.AsyncClient, token
    ) -> None:
        tok = token("admin.system")
        await client.post("/api/v1/evaluation/runs?dataset=security", headers=auth(tok))
        resp = await client.get("/api/v1/evaluation/runs?limit=5", headers=auth(tok))
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items

        first = items[0]
        for field in ("gitSha", "model", "promptHash", "retriever", "metrics"):
            assert field in first, f"{field} missing; the run cannot be compared without it"
        assert first["questionCount"] >= first["failureCount"]

        stamps = [i["startedAt"] for i in items]
        assert stamps == sorted(stamps, reverse=True)

    async def test_history_is_restricted_to_a_system_administrator(
        self, client: httpx.AsyncClient, token
    ) -> None:
        assert (
            await client.get("/api/v1/evaluation/runs", headers=auth(token("staff.finance")))
        ).status_code == 403
