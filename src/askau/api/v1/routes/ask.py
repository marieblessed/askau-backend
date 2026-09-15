"""The question endpoint — buffered and streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from askau.api.deps import AuthzDep, OrchestratorDep
from askau.api.mappers import citation_to_source, lifecycle_display, live_state
from askau.api.schemas.wire import (
    AcceptedEvent,
    AnswerOut,
    AskRequest,
    CitationOut,
    ConflictEvent,
    DoneEvent,
    ErrorEvent,
    SourceOut,
    SourcesEvent,
    StageEvent,
    TokenEvent,
)
from askau.api.sse import sse
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.correlation import get_correlation_id
from askau.domain.answer import GroundedAnswer
from askau.domain.conversation import Citation
from askau.domain.enums import AuditOutcome
from askau.rag.orchestrator import AnswerTrace, Stage

router = APIRouter(prefix="/v1", tags=["ask"])


def _sources_of(answer: GroundedAnswer) -> list[SourceOut]:
    """The answer's citations in the client's `Source` shape.

    Mirrors `_citations_of`, including its fallback, and the fallback is the
    part that matters. The pre-generation `sources` SSE frame is emitted before
    any citation exists — that is its whole purpose, showing the reader what the
    answer will be built from while it is still being written. Without the
    fallback that frame carried an empty list and the panel stayed blank until
    `done`, which is the same as not having the frame at all.

    Never for a refusal, for the same reason as `_citations_of`: attaching
    sources to "I could not find enough information" contradicts the answer.
    """
    raw = answer.diagnostics.get("citations", ())
    items: list[Citation] = list(raw) if isinstance(raw, tuple | list) else []
    if items:
        return [citation_to_source(c, index=i) for i, c in enumerate(items, start=1)]

    if answer.retrieval is None or answer.diagnostics.get("phase") != "sources":
        return []
    return [
        SourceOut(
            id=str(chunk.chunk_id),
            title=chunk.document_title,
            section=chunk.section_ref or "",
            page=chunk.page_from or 0,
            version=chunk.version_label or "",
            published=str(chunk.effective_from.year) if chunk.effective_from else "",
            department=chunk.department or "",
            doc_type=getattr(chunk, "doc_type", None) or "other",
            effective_date=chunk.effective_from.isoformat() if chunk.effective_from else "",
            status=lifecycle_display(
                chunk.lifecycle.value if hasattr(chunk.lifecycle, "value") else chunk.lifecycle
            ),
            classification=chunk.classification.value.upper(),
            excerpt="",
            citation_index=i,
        )
        for i, chunk in enumerate(answer.retrieval.chunks, start=1)
    ]


def _citations_of(answer: GroundedAnswer) -> list[CitationOut]:
    raw = answer.diagnostics.get("citations", ())
    items: list[Citation] = list(raw) if isinstance(raw, tuple | list) else []
    out = [
        CitationOut(
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
        for c in items
    ]
    if out or answer.retrieval is None:
        return out
    # Fall back to the retrieved set ONLY for the pre-generation `sources`
    # event, where citations do not exist yet but the documents that will be
    # used do. Never for a refusal: attaching sources to "I could not find
    # enough information" contradicts the answer and invites the reader to
    # believe it found something after all.
    if answer.diagnostics.get("phase") != "sources":
        return []
    return [
        CitationOut(
            marker=i,
            document_id=str(chunk.document_id),
            document_title=chunk.document_title,
            source_name=chunk.source_name,
            section_ref=chunk.section_ref,
            page_from=chunk.page_from,
            version_label=chunk.version_label,
            effective_from=chunk.effective_from,
            classification=chunk.classification.value,
        )
        for i, chunk in enumerate(answer.retrieval.chunks, start=1)
    ]


@router.post("/ask", response_model=AnswerOut)
async def ask(
    body: AskRequest, authz: AuthzDep, orch: OrchestratorDep, request: Request
) -> AnswerOut:
    prefs = await request.app.state.conversations.preferences(str(authz.user_id))
    answer, trace = await orch.answer(
        body.content,
        authz,
        language=body.language,
        include_historical=body.include_historical,
        allow_escalation=prefs.higher_intelligence,
    )
    # §5.3. Consent-gated, and only for answers that went badly — see
    # `db/eval_sampling.py`. Awaited rather than fired off: it is one small
    # insert, and a background task here would need its own error handling to
    # avoid becoming the kind of silent failure this table exists to find.
    await request.app.state.eval_sampler.sample(
        consented=prefs.share_analytics,
        question=body.content,
        answer_state=answer.state.value,
        document_ids=[str(d) for d in answer.retrieval.document_ids] if answer.retrieval else [],
    )

    _audit(request, authz, answer, trace)
    return AnswerOut(
        state=live_state(answer.state.value, _sources_of(answer)),
        sources=_sources_of(answer),
        grounding_count=len(_sources_of(answer)) or None,
        answer_state=answer.state.value,
        content=answer.content,
        citations=_citations_of(answer),
        conflicts=[c.summary for c in answer.conflicts],
        groundedness=answer.groundedness,
        correlation_id=get_correlation_id(),
        timings={
            "retrieval_ms": trace.retrieval_ms,
            "ttft_ms": trace.ttft_ms,
            "total_ms": trace.total_ms,
        },
    )


@router.post("/ask/stream")
async def ask_stream(
    body: AskRequest, authz: AuthzDep, orch: OrchestratorDep, request: Request
) -> StreamingResponse:
    """Server-sent events.

    ``sources`` is emitted before the first ``token`` — retrieval has already
    finished, so showing which documents will be used is real progress rather
    than a spinner.
    """

    # Read before the generator starts. A `StreamingResponse` body runs after
    # the route returns, and awaiting a database read from inside it would put
    # a query after the response headers are already on the wire — where a
    # failure can no longer become an HTTP status.
    prefs = await request.app.state.conversations.preferences(str(authz.user_id))

    async def events() -> AsyncIterator[str]:
        correlation = get_correlation_id()
        yield sse("accepted", AcceptedEvent(correlation_id=correlation))
        sources_sent = False
        try:
            async for item in orch.stream(
                body.content,
                authz,
                language=body.language,
                include_historical=body.include_historical,
                allow_escalation=prefs.higher_intelligence,
            ):
                if isinstance(item, Stage):
                    yield sse("stage", StageEvent(stage=item.name, elapsed_ms=item.elapsed_ms))
                elif isinstance(item, str):
                    yield sse("token", TokenEvent(text=item))
                elif not sources_sent and item.diagnostics.get("phase") == "sources":
                    sources_sent = True
                    yield sse(
                        "sources",
                        SourcesEvent(sources=_sources_of(item), citations=_citations_of(item)),
                    )
                else:
                    for conflict in item.conflicts:
                        yield sse("conflict", ConflictEvent(summary=conflict.summary))
                    yield sse(
                        "done",
                        DoneEvent(
                            state=live_state(item.state.value, _sources_of(item)),
                            sources=_sources_of(item),
                            grounding_count=len(_sources_of(item)) or None,
                            answer_state=item.state.value,
                            content=item.content,
                            # Offered as choices the reader can pick, rather
                            # than a list they must retype.
                            topics=_topics_of(item),
                            groundedness=item.groundedness,
                            citations=_citations_of(item),
                        ),
                    )
        except Exception as exc:
            # Fails to an error event, never to an ungrounded answer (ADR-0010).
            yield sse("error", ErrorEvent(type=type(exc).__name__, correlation_id=correlation))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _topics_of(answer: GroundedAnswer) -> list[str]:
    """Clarification subjects, narrowed from the untyped diagnostics bag."""
    raw = answer.diagnostics.get("topics")
    if isinstance(raw, tuple | list):
        return [str(t) for t in raw]
    return []


def _audit(request: Request, authz: AuthzDep, answer: GroundedAnswer, trace: AnswerTrace) -> None:
    writer = request.app.state.audit
    retrieved = list(answer.retrieval.document_ids) if answer.retrieval else []
    writer.record(
        AuditEvent(
            event_type=(
                EventType.QUERY_SUBMITTED if answer.state.is_answered else EventType.QUERY_REFUSED
            ),
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            # Document ids, never the question or the answer (FR-052).
            detail={
                "answer_state": answer.state.value,
                "retrieved_documents": [str(d) for d in retrieved],
                "groundedness": answer.groundedness,
                # Recorded because escalation spends more per question. An
                # operator asking "why did usage rise" can attribute it here
                # rather than inferring it from latency.
                "escalated": trace.escalated,
            },
        )
    )
