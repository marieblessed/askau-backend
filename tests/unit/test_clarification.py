"""Ambiguity detection — FR-008.

The bug this guards: "What is policy" retrieves three unrelated documents,
quotes one true sentence from each, and scores 100% grounded. Grounding measures
whether statements are supported, not whether they are responsive.
"""

from __future__ import annotations

import pytest

from askau.domain.enums import Classification, RetrievalStrategy
from askau.domain.retrieval import ChunkId, DocumentId, RetrievalResult, RetrievedChunk
from askau.rag.clarification import assess, message


def chunk(n: int, title: str, score: float = 0.03) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=ChunkId(n),
        document_id=DocumentId(f"doc-{n}"),
        content="Body text for the policy.",
        score=score,
        document_title=title,
        source_uri=f"https://x/{n}",
        source_name="Library",
        classification=Classification.INTERNAL,
    )


SCATTERED = RetrievalResult(
    chunks=(
        chunk(1, "Acceptable Use of Information Systems"),
        chunk(2, "Annual Leave Policy", 0.029),
        chunk(3, "Vendor Onboarding Checklist", 0.028),
    ),
    strategy=RetrievalStrategy.HYBRID,
    candidates_considered=3,
    took_ms=1,
)


class TestVagueQuestions:
    @pytest.mark.parametrize(
        "question",
        ["What is policy", "policy", "tell me about procedures", "What are the rules?"],
    )
    def test_flagged_for_clarification(self, question: str) -> None:
        assert assess(question, SCATTERED).needed

    def test_offers_the_subjects_that_were_found(self) -> None:
        """Saying only "be more specific" pushes the work back onto someone who
        has no idea what the corpus contains — so the subjects come back too."""
        request = assess("What is policy", SCATTERED)
        assert "Annual Leave Policy" in request.topics

    def test_subjects_are_data_not_prose(self) -> None:
        """They are offered as choices to click, not titles to retype. Putting
        them in the message as well renders the same list twice."""
        request = assess("What is policy", SCATTERED)
        body = message(request)
        assert "Annual Leave Policy" not in body
        assert request.reason in body


class TestSpecificQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "What is the annual leave entitlement for staff on probation?",
            "What is the daily subsistence allowance for continental travel?",
            "What documents are required for vendor onboarding?",
            "budget reallocation thresholds",
        ],
    )
    def test_answered_not_interrupted(self, question: str) -> None:
        """A clarification prompt that fires on a well-formed question is worse
        than the failure it prevents — it teaches people to dismiss it."""
        assert not assess(question, SCATTERED).needed


class TestBoundaries:
    def test_empty_retrieval_is_not_ambiguity(self) -> None:
        """Unanswerable, not ambiguous — the evidence gate owns that path."""
        empty = RetrievalResult(
            chunks=(),
            strategy=RetrievalStrategy.HYBRID,
            candidates_considered=0,
            took_ms=1,
        )
        assert not assess("What is policy", empty).needed

    def test_organisational_filler_does_not_count_as_specificity(self) -> None:
        """ "AUC staff policy" is three words and zero discriminating terms."""
        assert assess("AUC staff policy", SCATTERED).needed


class TestASpecificQuestionIsNotVague:
    """The regression these tests did not have.

    An HR officer asked "What is the disciplinary procedure for staff members?"
    and was told to refine her question with more specific terms. It was already
    specific; the matching document was already ranked first; she had every right
    to read it.

    `procedure`, `staff` and `members` are all generic across an AU corpus, so
    the question reduced to one discriminating word against a threshold of two.
    Every example above — "tell me about procedures", "What are the rules?" —
    contributes **zero**. One is the boundary, and it was on the wrong side of it.
    """

    def test_one_discriminating_word_is_enough(self) -> None:
        assert not assess("What is the disciplinary procedure for staff members?", SCATTERED).needed

    @pytest.mark.parametrize(
        "question",
        [
            "What is the disciplinary procedure for staff members?",
            "How do I claim reimbursement?",
            "What are the thresholds for procurement?",
        ],
    )
    def test_questions_that_name_something_are_answered(self, question: str) -> None:
        """Each names exactly one thing, and each was refused before."""
        assert not assess(question, SCATTERED).needed

    def test_a_lone_candidate_is_no_choice_at_all(self) -> None:
        """Clarification offers a choice. With one document there is none, and
        asking withholds the single answer while implying the reader was
        unclear."""
        single = RetrievalResult(
            chunks=(chunk(1, "Disciplinary Procedure"), chunk(2, "Disciplinary Procedure", 0.029)),
            strategy=RetrievalStrategy.HYBRID,
            candidates_considered=2,
            took_ms=1,
        )
        assert not assess("policy", single).needed
