"""Sampling real questions into the evaluation corpus — the `shareAnalytics` gate (§5.3).

The client's toggle reads *"Help improve AskAU by sharing anonymised usage
data"*, which describes telemetry. Telemetry is not what it can govern: BR-008
makes `audit_events` non-optional and `model_invocations` backs the §7.4
resource reporting the AUC requires, so a user cannot decline either and a
toggle claiming otherwise would be false.

What *is* genuinely discretionary is whether this person's questions may be used
to improve AskAU. That has a concrete home — the four `eval_*` tables, which
have been schema-only since they were created. Real staff questions are the most
valuable evaluation corpus there is, and a synthetic one written by the team that
built the retriever is the least valuable, because it asks things the way the
retriever expects them to be asked.

So the setting means exactly one thing:

> may this person's questions be sampled into `eval_questions` for quality
> measurement.

Honest, enforceable, and it touches nothing required. Their UI copy needs to
change to match — that goes back with the rest of the notes.

**What is stored, and what is deliberately not.** The question text, the state
the answer reached, and the documents it drew on. Not the answer, not the user
id, not the asker's principals. `as_principal_id` stays NULL: filling it with
the asker's principal would make the row re-identifying — a question plus the
exact access footprint of the person who asked it is a small enough set to name
someone — and an evaluation run supplies its own principal anyway.

**Which questions.** Only the ones that went badly. A grounded answer with good
citations teaches the evaluation corpus nothing it does not already know; a
refusal, a conflict, or a thin answer is a case somebody should look at. That
also keeps the table from filling with thousands of near-identical rows, which
is what an unfiltered sample of production traffic becomes.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_log = logging.getLogger(__name__)

#: The dataset live samples land in. Named and separate so a quality run can
#: choose it deliberately, and so it can be emptied without touching the
#: curated datasets.
DATASET_NAME = "live-samples"

#: States worth keeping. A grounded answer is the case that already works.
_WORTH_SAMPLING = frozenset(
    {"insufficient_evidence", "conflict", "partially_grounded", "clarification_needed"}
)

#: `category` is constrained to retrieval | generation | security | performance.
#: These samples are `retrieval`: every state that qualifies for sampling —
#: insufficient evidence, a conflict, thin support — is a statement about what
#: came back from the corpus, not about how the model wrote it up.
_UPSERT_DATASET = text("""
    INSERT INTO eval_datasets (name, category, description)
    VALUES (:name, 'retrieval', 'Questions sampled from real use, with consent (shareAnalytics).')
    ON CONFLICT (name) DO UPDATE SET name = excluded.name
    RETURNING id
""")

#: `WHERE NOT EXISTS` rather than a unique index: the same question asked twice
#: is one evaluation case, but "the same question" is a judgement about text and
#: not something worth a constraint on a table that also holds curated rows with
#: deliberate near-duplicates.
_INSERT_QUESTION = text("""
    INSERT INTO eval_questions (dataset_id, question, expected_state, expected_family_ids, tags)
    SELECT :dataset_id, :question, CAST(:state AS answer_state),
           CAST(:documents AS uuid[]), CAST(:tags AS text[])
    WHERE NOT EXISTS (
        SELECT 1 FROM eval_questions
        WHERE dataset_id = :dataset_id AND lower(question) = lower(:question)
    )
""")


class EvalSampler:
    """Writes consented questions into the evaluation corpus.

    Never raises. Sampling is a quality-improvement nicety and a failure here
    must not turn a successfully answered question into an error the reader
    sees — the same reasoning as the audit writer, minus the queue, because
    unlike audit this write is genuinely optional and losing one is harmless.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def sample(
        self,
        *,
        consented: bool,
        question: str,
        answer_state: str,
        document_ids: list[str],
    ) -> bool:
        """Returns whether a row was written, for the tests that assert it was not."""
        if not consented or answer_state not in _WORTH_SAMPLING:
            return False
        try:
            async with self._engine.begin() as conn:
                dataset_id = (
                    await conn.execute(_UPSERT_DATASET, {"name": DATASET_NAME})
                ).scalar_one()
                result = await conn.execute(
                    _INSERT_QUESTION,
                    {
                        "dataset_id": dataset_id,
                        "question": question,
                        "state": answer_state,
                        "documents": document_ids,
                        "tags": ["sampled"],
                    },
                )
                return bool(result.rowcount)
        except Exception:
            # Deliberately swallowed — see the class docstring. Worth knowing
            # what that costs: this hid a CHECK violation during development
            # and made a broken sampler look exactly like a working one with
            # nothing to sample. Which is why `sample()` returns a bool and the
            # tests assert on it, rather than inferring success from a row
            # count that is also 0 when the write silently failed.
            _log.warning("eval sampling failed; question not recorded", exc_info=True)
            return False
