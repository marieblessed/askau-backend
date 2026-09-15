"""Domain → wire mapping.

One place, because the same citation is rendered by three endpoints (the ask
response, the stream's `sources` and `done` frames, and a stored conversation)
and three copies of this mapping would drift the way the two `_sse` helpers did.

The direction is one-way on purpose: nothing here reads a wire model and returns
a domain object. Requests are parsed by Pydantic at the route and validated
there; if a mapper could go both ways it would become the place where an
unvalidated field slips inward.
"""

from __future__ import annotations

from askau.api.schemas.wire import CitationOut, MessageOut, SourceOut
from askau.db.conversations import StoredMessage
from askau.domain.conversation import Citation

#: `documents.lifecycle` → the display strings the client's `SourceCard` shows.
#: Its fixture uses "Active" and "Superseded" as free text, so this is a
#: presentation mapping rather than a shared enum — which is why it lives at the
#: boundary and not in `domain/enums.py`.
_STATUS_DISPLAY: dict[str, str] = {
    "draft": "Draft",
    "active": "Active",
    "review_required": "Under review",
    "expired": "Expired",
    "superseded": "Superseded",
}


def citation_to_source(c: Citation, *, index: int | None = None) -> SourceOut:
    """A citation in the shape `features/chat` renders.

    `published` is the effective year rather than a separate publication date:
    their card shows it as a bare "2026" beside the version, and for a policy
    the date that matters is when it took effect, not when the file was written.
    A document with no effective date shows nothing there rather than a guess.
    """
    return SourceOut(
        # The chunk, not the document: two citations can quote the same document,
        # and this is a React key on their side as well as our identifier.
        id=str(c.chunk_id),
        title=c.document_title,
        section=c.section_ref or (" / ".join(c.heading_path) if c.heading_path else ""),
        page=c.page_from or 0,
        version=c.version_label or "",
        published=str(c.effective_from.year) if c.effective_from else "",
        department=c.department or "",
        # Their `DocumentType` union ends in "other", which is the right home for
        # anything the corpus has not classified.
        doc_type=c.doc_type or "other",
        effective_date=c.effective_from.isoformat() if c.effective_from else "",
        status=lifecycle_display(c.lifecycle),
        # Upper-cased here and only here. The enum is lower-case throughout the
        # domain and the database; the client's `ClassLevel` is upper-case.
        classification=c.classification.value.upper(),
        excerpt=c.quote or "",
        # FR-031: grounding and opening are separate permissions. A source may
        # support an answer the reader is not allowed to open.
        has_access=c.can_open,
        access_url=f"/api/v1/documents/{c.document_id}/open" if c.can_open else None,
        citation_index=index if index is not None else c.marker,
    )


def citation_to_wire(c: Citation) -> CitationOut:
    """The flatter citation shape, kept for the ask response's `citations`."""
    return CitationOut(
        marker=c.marker,
        document_id=str(c.document_id),
        document_title=c.document_title,
        source_name=c.source_name,
        section_ref=c.section_ref,
        page_from=c.page_from,
        page_to=c.page_to,
        version_label=c.version_label,
        quote=c.quote,
        can_open=c.can_open,
        classification=c.classification.value,
    )


def stored_message_to_wire(m: StoredMessage, conversation_id: str) -> MessageOut:
    """A persisted turn, reconstructed for the transcript.

    `groundingCount` is the number of sources, not a score: the client renders
    it as *"Grounded in N approved sources"*, and their design substitutes
    provenance for a confidence number deliberately. Our `groundedness` float
    stays out of the wire here rather than being reinterpreted as a count.
    """
    sources = [citation_to_source(c, index=i) for i, c in enumerate(m.citations, start=1)]
    state = outdated_from_citations(m.answer_state, sources) if m.answer_state else None
    return MessageOut(
        id=m.id,
        conversation_id=conversation_id,
        role=m.role.value,
        content=m.content,
        state=answer_state_display(state) if state else None,
        answer_state=m.answer_state,
        sources=sources,
        grounding_count=len(sources) or None,
        feedback=m.feedback,
        created_at=m.created_at.isoformat()
        if hasattr(m.created_at, "isoformat")
        else str(m.created_at),
    )


def live_state(answer_state: str, sources: list[SourceOut]) -> str:
    """The client-renderable state for a live answer.

    The same two steps `stored_message_to_wire` applies, in one place so the
    live and replayed views of one turn cannot disagree — they did: the
    promotion to `outdated` existed only on the stored path, so an answer resting
    on a superseded document came back `grounded` and became `outdated` on
    refresh.
    """
    return answer_state_display(outdated_from_citations(answer_state, sources))


def lifecycle_display(lifecycle: str | None) -> str:
    """Presentation text for a document's lifecycle, or empty if unknown.

    Empty rather than a fallback like "Unknown": their card shows the value as
    a status, and inventing one is worse than showing none.
    """
    return _STATUS_DISPLAY.get(lifecycle or "", "")


# ── answer state ────────────────────────────────────────────────────────────

#: Our eight answer states mapped onto the five the client can render.
#:
#: This exists because of how `features/chat/components/ai-message.tsx` is
#: built: a chain of independent `message.state === "x" &&` blocks with no
#: default branch. An unrecognised state therefore renders **nothing at all** —
#: not a fallback, not the answer text, an empty message. So `state` must always
#: be one of their five, whatever we would rather say.
#:
#: The full value travels alongside as `answerState`, so nothing is lost on the
#: wire and their UI can adopt the finer states without an API change.
_STATE_DISPLAY: dict[str, str] = {
    "grounded": "grounded",
    # Grounded with thinner support. Still grounded — `groundingCount` is what
    # conveys the difference, and their pill already renders it.
    "partially_grounded": "grounded",
    "conflict": "conflicting",
    "insufficient_evidence": "insufficient",
    "error": "error",
    # ── the three with no home in their UI ──────────────────────────────────
    #
    # All three map to `insufficient` because it is the least harmful of the
    # available renderings — a calm panel offering "refine your question",
    # "contact the responsible department" and "browse available sources", which
    # are reasonable next steps for any of them.
    #
    # It is still wrong, and specifically: **that branch renders fixed
    # translated copy and never `message.content`**, so our explanation of *why*
    # we did not answer is discarded. FR-028 requires AskAU to state that it
    # cannot answer; for these three states their UI currently cannot show that
    # statement. Closing it needs a branch on their side, not a different
    # mapping on ours.
    "clarification_needed": "insufficient",
    "out_of_scope": "insufficient",
    "refused_safety": "insufficient",
    # Produced by `outdated_from_citations` below; included so the mapping is
    # total and a round-trip through it is stable.
    "outdated": "outdated",
}


def answer_state_display(state: str) -> str:
    """The client-renderable state for one of ours.

    Falls back to `error` rather than passing an unknown value through: an
    unrecognised state renders as an empty message, and a visible error is a
    better failure than a blank one.
    """
    return _STATE_DISPLAY.get(state, "error")


def outdated_from_citations(state: str, sources: list[SourceOut]) -> str:
    """Promote a grounded answer to `outdated` when it rests on superseded material.

    Their UI renders an `outdated` banner — *"Based on an older approved
    document"* — and until now nothing could produce it, so the branch was
    unreachable. We already hold the fact: `documents.lifecycle` records
    `superseded`, and retrieval admits historical documents when the caller asks
    for them (`include_historical`).

    Only applied to answers that are otherwise grounded. A refusal that happens
    to cite an old document is still a refusal, and relabelling it would report
    the less important of the two facts.
    """
    if answer_state_display(state) != "grounded":
        return state
    if any(s.status == "Superseded" for s in sources):
        return "outdated"
    return state
