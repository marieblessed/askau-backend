"""Ambiguity detection (FR-008).

The gap this closes: "What is policy" retrieves three unrelated documents,
extracts one true sentence from each, and scores 100% grounded. Every individual
claim is supported, so neither the evidence gate nor the groundedness scorer
objects — and the reader gets a confident answer to a question that was never
answered.

Grounding measures whether statements are *supported*. It cannot measure whether
they are *responsive*. Those are different properties and need different checks.

The check is deliberately narrow: **the question carries too few discriminating
words to identify a document.** "What is policy" has none once question-shaped
and organisational filler is removed.

A second signal was tried and removed — flagging results that came from several
documents with little score separation. It fired on well-formed questions like
"What is the daily subsistence allowance for continental travel?", which legitimately
touch three travel documents. A clarification prompt that interrupts a good
question is worse than the failure it prevents: it trains people to dismiss the
prompt, and then it is worthless on the questions that need it.

That signal needs real embeddings to evaluate — rank scores from the development
hash embedder cluster tightly for every query, so there is nothing to measure
separation against. Revisit it when a semantic embedder is in place, against the
evaluation set rather than by eye.
"""

from __future__ import annotations

from dataclasses import dataclass

from askau.domain.retrieval import RetrievalResult
from askau.rag.vocabulary import discriminating_words


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    needed: bool
    reason: str = ""
    topics: tuple[str, ...] = ()

    @classmethod
    def none(cls) -> ClarificationRequest:
        return cls(needed=False)


def assess(
    question: str,
    result: RetrievalResult,
    *,
    #: A question contributing *no* discriminating vocabulary at all.
    #:
    #: It was 2, and one too high. Measured against this module's own examples:
    #: "tell me about procedures", "what are the rules", "tell me about
    #: policies" all score **0**. "What is the disciplinary procedure for staff
    #: members?" scores **1** — `procedure`, `staff` and `members` are generic
    #: across an AU corpus, leaving `disciplinary` alone — and was refused as
    #: too general, which it plainly is not.
    #:
    #: At 1, clarification fires only for questions that name nothing. That is
    #: what the docstring above always described; the threshold simply did not
    #: match it.
    min_content_words: int = 1,
) -> ClarificationRequest:
    """Decide whether to ask rather than answer."""
    if result.is_empty:
        # Not ambiguous — simply unanswerable. The evidence gate owns that path.
        return ClarificationRequest.none()

    if len(discriminating_words(question)) >= min_content_words:
        return ClarificationRequest.none()

    # Nothing to choose between, nothing to ask.
    #
    # A clarifying question offers the reader a choice: *which* of these did you
    # mean. When everything retrieved points at one document there is no choice
    # to offer, and asking anyway is absurd — it tells somebody their question
    # was too vague while holding the single answer to it.
    #
    # This is not hypothetical. "What is the disciplinary procedure for staff
    # members?" scores one discriminating word, because `procedure`, `staff` and
    # `members` are generic across this corpus. An HR officer asking about her
    # own department's policy was told to "refine your question with more
    # specific terms" — advice that could not have worked, since the question
    # was already specific and the document was already first.
    topics = _topics(result)
    if len(topics) < 2:
        return ClarificationRequest.none()

    return ClarificationRequest(
        needed=True,
        reason="This question is too general to identify which policy you mean.",
        topics=topics,
    )


def _topics(result: RetrievalResult, limit: int = 4) -> tuple[str, ...]:
    """Distinct document titles in retrieval order.

    Offered back to the reader as the choices they can pick between — a
    clarifying question that says only "please be more specific" pushes the work
    back onto someone who does not know what the corpus contains.
    """
    seen: list[str] = []
    for chunk in result.chunks:
        if chunk.document_title not in seen:
            seen.append(chunk.document_title)
        if len(seen) >= limit:
            break
    return tuple(seen)


def message(request: ClarificationRequest) -> str:
    """Compose the prose half of the clarifying question.

    The subjects themselves are **not** in here. They travel as structured
    ``topics`` so the interface can offer them as choices to click rather than
    titles to retype — and so they are not rendered twice, once as prose and
    once as controls. API consumers read ``topics`` from the response.
    """
    blocks = [request.reason]
    blocks.append(
        "Naming the topic — for example “annual leave entitlement” rather than "
        "“leave” — usually finds the right document."
    )
    return "\n\n".join(blocks)
