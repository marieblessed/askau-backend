"""The question endpoint — buffered and streaming."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from askau.api.deps import AuthzDep, OrchestratorDep
from askau.api.schemas.wire import AnswerOut, AskRequest, CitationOut
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.correlation import get_correlation_id
from askau.domain.answer import GroundedAnswer
from askau.domain.conversation import Citation
from askau.domain.enums import AuditOutcome
from askau.rag.orchestrator import Stage

router = APIRouter(prefix="/v1", tags=["ask"])


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
    answer, trace = await orch.answer(
        body.content,
        authz,
        language=body.language,
        include_historical=body.include_historical,
    )
    _audit(request, authz, answer, trace)
    return AnswerOut(
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

    async def events() -> AsyncIterator[str]:
        correlation = get_correlation_id()
        yield _sse("accepted", {"correlation_id": correlation})
        sources_sent = False
        try:
            async for item in orch.stream(
                body.content,
                authz,
                language=body.language,
                include_historical=body.include_historical,
            ):
                if isinstance(item, Stage):
                    yield _sse("stage", {"stage": item.name, "elapsed_ms": item.elapsed_ms})
                elif isinstance(item, str):
                    yield _sse("token", {"text": item})
                elif not sources_sent and item.diagnostics.get("phase") == "sources":
                    sources_sent = True
                    yield _sse(
                        "sources",
                        {"citations": [c.model_dump(mode="json") for c in _citations_of(item)]},
                    )
                else:
                    for conflict in item.conflicts:
                        yield _sse("conflict", {"summary": conflict.summary})
                    yield _sse(
                        "done",
                        {
                            "answer_state": item.state.value,
                            # Refusals emit no tokens, so the explanation has to
                            # travel on `done`. FR-028 requires AskAU to *state*
                            # that it cannot answer — a bare status label is not
                            # that statement.
                            "content": item.content,
                            # Offered as choices the reader can pick, rather
                            # than a list they must retype.
                            "topics": _topics_of(item),
                            "groundedness": item.groundedness,
                            "citations": [c.model_dump(mode="json") for c in _citations_of(item)],
                        },
                    )
        except Exception as exc:
            # Fails to an error event, never to an ungrounded answer (ADR-0010).
            yield _sse(
                "error",
                {"type": type(exc).__name__, "correlation_id": correlation},
            )

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


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _audit(request: Request, authz: AuthzDep, answer: GroundedAnswer, trace: object) -> None:
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
            },
        )
    )
