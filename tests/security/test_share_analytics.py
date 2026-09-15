"""`shareAnalytics` — what it gates, and what it provably cannot (§5.3).

The obvious reading of the client's copy — *"Help improve AskAU by sharing
anonymised usage data"* — is unbuildable. `audit_events` is non-optional under
BR-008 and `model_invocations` backs the §7.4 resource reporting the AUC
requires; a user cannot decline either, and a toggle that claimed to switch
them off would be a lie told by the settings screen.

So the setting means one enforceable thing: may this person's questions be
sampled into `eval_questions` for quality measurement.

Two halves, and the second is the unusual one. Most consent tests assert that
switching something off stops it. This suite also asserts that switching it off
*changes nothing else* — because the design's whole claim is that the toggle
cannot reach the records the AUC is required to keep. A future change that
quietly made audit conditional on it would satisfy every other test in the
codebase.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from askau.db.eval_sampling import DATASET_NAME
from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]

#: Chosen because the corpus genuinely cannot answer it, so it reaches
#: `insufficient_evidence` — one of the states worth sampling. A question the
#: corpus answers well is never sampled, which is the point of the filter and
#: would make this suite test nothing.
_UNANSWERABLE = "What is the procedure for terminating a consultancy contract early?"
_ANSWERABLE = "How many days of annual leave do staff accrue?"


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _set_sharing(client: httpx.AsyncClient, tok: str, *, on: bool) -> None:
    resp = await client.patch(
        "/api/v1/users/me/preferences", headers=auth(tok), json={"shareAnalytics": on}
    )
    assert resp.status_code == 200
    assert resp.json()["shareAnalytics"] is on


async def _samples(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("""
                    SELECT count(*) FROM eval_questions q
                    JOIN eval_datasets d ON d.id = q.dataset_id
                    WHERE d.name = :name
                    """),
                    {"name": DATASET_NAME},
                )
            ).scalar_one()
        )


async def _ask(client: httpx.AsyncClient, tok: str, question: str) -> httpx.Response:
    return await client.post("/api/v1/ask", headers=auth(tok), json={"content": question})


class TestConsentGatesSampling:
    async def test_with_sharing_off_no_question_is_sampled(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        tok = token("staff.finance")
        await _set_sharing(client, tok, on=False)
        try:
            before = await _samples(admin_engine)
            resp = await _ask(client, tok, _UNANSWERABLE)
            assert resp.status_code == 200
            assert await _samples(admin_engine) == before
        finally:
            await _set_sharing(client, tok, on=True)

    async def test_with_sharing_on_a_weak_answer_is_sampled(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Guards against the inverse failure of the test above.

        A sampler that never wrote anything — a broken query, a wrong table,
        an exception swallowed by the defensive `except` — would pass
        "sharing off writes nothing" perfectly. That actually happened during
        development: a CHECK violation on the dataset category made a broken
        sampler indistinguishable from a working one. This is the test that
        tells them apart.
        """
        tok = token("staff.finance")
        await _set_sharing(client, tok, on=True)
        unique = f"{_UNANSWERABLE} Reference {id(self)}."
        before = await _samples(admin_engine)
        assert (await _ask(client, tok, unique)).status_code == 200
        assert await _samples(admin_engine) == before + 1

    async def test_a_well_answered_question_is_never_sampled(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """Consent is necessary, not sufficient.

        A grounded answer teaches the evaluation corpus nothing it does not
        already know, and sampling every question would fill the table with
        thousands of near-identical rows — which is what an unfiltered sample
        of production traffic becomes.
        """
        tok = token("staff.finance")
        await _set_sharing(client, tok, on=True)
        before = await _samples(admin_engine)
        resp = await _ask(client, tok, _ANSWERABLE)
        state = resp.json()["answerState"]
        # `partially_grounded` *is* worth sampling, so this assertion only means
        # something for a fully grounded answer. Skipping rather than passing
        # silently: a corpus change that made this question thin would otherwise
        # turn the test into a no-op nobody notices.
        if state != "grounded":
            pytest.skip(f"question answered as {state!r}, which is a sampled state")
        assert await _samples(admin_engine) == before

    async def test_the_same_question_is_not_sampled_twice(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        tok = token("staff.finance")
        await _set_sharing(client, tok, on=True)
        repeated = f"{_UNANSWERABLE} Duplicate check {id(self)}."
        await _ask(client, tok, repeated)
        after_first = await _samples(admin_engine)
        await _ask(client, tok, repeated)
        assert await _samples(admin_engine) == after_first


class TestWhatIsSampledIsNotIdentifying:
    async def test_no_user_or_principal_is_recorded_with_the_question(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """`as_principal_id` stays NULL deliberately.

        A question plus the exact access footprint of the person who asked it
        is a small enough set to name someone, which would make "anonymised"
        false. An evaluation run supplies its own principal, so nothing is lost.
        """
        tok = token("staff.finance")
        await _set_sharing(client, tok, on=True)
        unique = f"{_UNANSWERABLE} Identity check {id(self)}."
        await _ask(client, tok, unique)

        async with admin_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("""
                    SELECT q.as_principal_id, q.expected_answer
                    FROM eval_questions q
                    JOIN eval_datasets d ON d.id = q.dataset_id
                    WHERE d.name = :name AND q.question = :q
                    """),
                    {"name": DATASET_NAME, "q": unique},
                )
            ).first()
        assert row is not None, "the question was not sampled, so this asserts nothing"
        assert row[0] is None, "the asker's principal was stored — the row is re-identifying"
        # The answer is not kept either. What makes a sampled question useful is
        # the question and which documents it reached; storing the generated
        # answer as though it were a target would enshrine the current model's
        # output as the expected one.
        assert row[1] is None


class TestTheToggleCannotReachWhatIsMandatory:
    """The half of the design most likely to be broken by a well-meaning change.

    BR-008 and §7.4 are not preferences. If a future edit made either
    conditional on this flag, every other test would still pass.
    """

    async def test_turning_sharing_off_changes_nothing_in_audit(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        tok = token("staff.finance")

        async def audited() -> int:
            deadline = asyncio.get_running_loop().time() + 5.0
            async with admin_engine.connect() as conn:
                sql = text("SELECT count(*) FROM audit_events WHERE event_category = 'query'")
                seen = int((await conn.execute(sql)).scalar_one())
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                async with admin_engine.connect() as conn:
                    now = int((await conn.execute(sql)).scalar_one())
                if now == seen:
                    return now
                seen = now
            return seen

        await _set_sharing(client, tok, on=False)
        try:
            before = await audited()
            await _ask(client, tok, f"{_UNANSWERABLE} Audit check {id(self)}.")
            assert await audited() == before + 1, (
                "BR-008: the audit record is not something a preference can switch off"
            )
        finally:
            await _set_sharing(client, tok, on=True)

    async def test_turning_sharing_off_changes_nothing_in_model_invocations(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, token
    ) -> None:
        """§7.4 resource reporting. The AUC requires it; a user cannot decline it."""
        tok = token("staff.finance")

        async def invocations() -> int:
            async with admin_engine.connect() as conn:
                return int(
                    (
                        await conn.execute(text("SELECT count(*) FROM model_invocations"))
                    ).scalar_one()
                )

        await _set_sharing(client, tok, on=False)
        try:
            before = await invocations()
            await _ask(client, tok, _ANSWERABLE)
            assert await invocations() > before
        finally:
            await _set_sharing(client, tok, on=True)
