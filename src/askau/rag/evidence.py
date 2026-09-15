"""The evidence sufficiency gate (FR-028, BR-007, ADR-0008).

Evaluated *before* generation. Below threshold the pipeline returns a
deterministic refusal and the model is never called.

That ordering is the whole point. Instructing a model to refuse is a request it
can be talked out of by a confidently-phrased question; refusing before the call
cannot be. It also costs zero tokens, which means honesty is the cheap path
rather than the expensive one.
"""

from __future__ import annotations

from dataclasses import dataclass

from askau.domain.answer import EvidenceAssessment
from askau.domain.retrieval import RetrievalResult
from askau.rag.vocabulary import discriminating_words


@dataclass(frozen=True, slots=True)
class EvidenceThresholds:
    """Tuning knobs for the gate.

    ``min_top_score`` is expressed in RRF units, which are small: a single arm
    at rank 1 with k=60 contributes 1/61 ≈ 0.0164. So a chunk found at a good
    rank by one arm clears a 0.015 floor, and a chunk found by neither arm near
    the top does not.
    """

    min_top_score: float = 0.015
    min_supporting_chunks: int = 1
    #: A result where every chunk is far below the leader is usually one lucky
    #: keyword match rather than genuine coverage.
    min_score_ratio: float = 0.35
    #: Fraction of the question's *discriminating* words that must appear in the
    #: top results. Rank alone cannot separate "the best of a bad set" from "a
    #: good match" — every query returns something.
    #:
    #: Measured on discriminating words only. Counting organisational vocabulary
    #: lets "What is the retirement age for Commission staff?" pass on
    #: *commission* and *staff*, which every document in the corpus contains.
    min_lexical_overlap: float = 0.34


def assess(
    result: RetrievalResult,
    thresholds: EvidenceThresholds | None = None,
    question: str = "",
) -> EvidenceAssessment:
    t = thresholds or EvidenceThresholds()

    if result.is_empty:
        return EvidenceAssessment.insufficient(
            0.0, 0, "No authorized content matched the question."
        )

    top = result.chunks[0].score
    if top < t.min_top_score:
        return EvidenceAssessment.insufficient(
            top,
            len(result.chunks),
            "The closest available material is only weakly related to the question.",
        )

    supporting = sum(1 for c in result.chunks if c.score >= top * t.min_score_ratio)
    if supporting < t.min_supporting_chunks:
        return EvidenceAssessment.insufficient(
            top, supporting, "Only a single weak match was found."
        )

    # FR-009: an out-of-scope question ("what is the capital of Brazil") still
    # retrieves the least-bad chunks available. Ranking cannot detect that;
    # vocabulary overlap can.
    if question:
        asked = discriminating_words(question)
        if asked:
            # Titles count as part of what a document says.
            #
            # They were excluded, and it rejected exactly the questions this
            # corpus is full of. "What is the disciplinary procedure for staff
            # members?" reduces to one discriminating word — `procedure`,
            # `staff` and `members` are generic here — and the body of the
            # matching document never uses it: it opens "on receipt of an
            # allegation of misconduct". The word is in the title.
            #
            # With a single asked word the ratio is binary, so one absent term
            # took the overlap to zero and an HR officer was told the material
            # "does not discuss the subject" of a document named after her
            # question. A reader sees the title on every citation; it is part of
            # the document for the purpose of judging relevance.
            #
            # FR-009 is unaffected: "what is the capital of Brazil" matches no
            # title in an AU corpus either.
            corpus = discriminating_words(
                " ".join(f"{c.document_title} {c.content}" for c in result.chunks[:5])
            )
            overlap = len(asked & corpus) / len(asked)
            if overlap < t.min_lexical_overlap:
                return EvidenceAssessment.insufficient(
                    top,
                    supporting,
                    "The retrieved material does not discuss the subject of the question.",
                )

    return EvidenceAssessment(sufficient=True, top_score=top, supporting_chunks=supporting)


#: FR-028 requires that AskAU state it cannot reliably answer, without
#: fabricating a source. Deterministic text, so the refusal cannot itself
#: hallucinate.
INSUFFICIENT_EVIDENCE_MESSAGE = (
    "I could not find enough supporting information in the AUC knowledge sources "
    "available to you to answer this reliably.\n\n"
    "This may mean the relevant document has not been added to AskAU yet, that it "
    "sits outside your access, or that the question needs rephrasing in the "
    "terminology the policy itself uses."
)

OUT_OF_SCOPE_MESSAGE = (
    "This question falls outside the AUC knowledge sources AskAU can draw on. "
    "AskAU answers only from approved institutional documents, so it cannot help "
    "with general knowledge or information held outside the Commission."
)
