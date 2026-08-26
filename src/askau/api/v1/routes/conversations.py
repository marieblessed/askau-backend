"""Conversations — history, follow-ups, and feedback (FR-004 … FR-006, FR-043/044)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from askau.api.deps import AuthzDep, OrchestratorDep
from askau.api.schemas.wire import AnswerOut, AskRequest, CitationOut
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.correlation import get_correlation_id
from askau.core.errors import InvalidRequestError, NotFoundError
from askau.domain.conversation import Citation
from askau.domain.enums import AuditOutcome, FeedbackRating, FeedbackReason
from askau.rag.orchestrator import Stage

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


class NewConversation(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class FeedbackIn(BaseModel):
    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str | None = Field(default=None, max_length=2000)


def _repo(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.conversations


@router.post("", status_code=201)
async def create(body: NewConversation, authz: AuthzDep, request: Request) -> dict[str, str]:
    return {"id": await _repo(request).create(str(authz.user_id), body.title)}


@router.get("")
async def list_own(
    authz: AuthzDep, request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> dict[str, Any]:
    """FR-006. Ownership is a predicate inside the query, so another user's
    conversation is indistinguishable from one that does not exist."""
    items = await _repo(request).list_for(str(authz.user_id), limit)
    return {
        "count": len(items),
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "message_count": c.message_count,
                "last_message_at": c.last_message_at,
                "created_at": c.created_at,
            }
            for c in items
        ],
    }


@router.get("/{conversation_id}")
async def read(conversation_id: str, authz: AuthzDep, request: Request) -> dict[str, Any]:
    messages = await _repo(request).history(conversation_id, str(authz.user_id))
    if not messages and not await _repo(request).owned_by(conversation_id, str(authz.user_id)):
        raise NotFoundError("Conversation not found")
    return {
        "id": conversation_id,
        "messages": [
            {
                "id": m.id,
                "role": m.role.value,
                "seq": m.seq,
                "content": m.content,
                "answer_state": m.answer_state,
                "groundedness": m.groundedness,
                "feedback": m.feedback,
                "created_at": m.created_at,
                "citations": [
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
                    ).model_dump(mode="json")
                    for c in m.citations
                ],
            }
            for m in messages
        ],
    }


class RenameIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    is_archived: bool | None = None


@router.patch("/{conversation_id}")
async def rename(
    conversation_id: str, body: RenameIn, authz: AuthzDep, request: Request
) -> dict[str, str]:
    if not await _repo(request).rename(
        conversation_id, str(authz.user_id), body.title, body.is_archived
    ):
        raise NotFoundError("Conversation not found")
    return {"id": conversation_id, "updated": "ok"}


@router.delete("/{conversation_id}", status_code=204)
async def delete(conversation_id: str, authz: AuthzDep, request: Request) -> None:
    if not await _repo(request).delete(conversation_id, str(authz.user_id)):
        raise NotFoundError("Conversation not found")
    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.CONVERSATION_DELETED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="conversation",
            resource_id=conversation_id,
        )
    )


@router.post("/{conversation_id}/messages", response_model=AnswerOut)
async def ask_in_conversation(
    conversation_id: str,
    body: AskRequest,
    authz: AuthzDep,
    orch: OrchestratorDep,
    request: Request,
) -> AnswerOut:
    """Ask within a conversation, resolving follow-ups against its history.

    FR-004: "does this apply to staff on probation?" only means something
    relative to the previous turn. The prior exchanges are passed to query
    understanding, not concatenated into the retrieval query — a follow-up
    should inherit *subject*, not inherit every keyword that came before.
    """
    repo = _repo(request)
    if not await repo.owned_by(conversation_id, str(authz.user_id)):
        raise NotFoundError("Conversation not found")

    history = await repo.recent_turns(conversation_id, str(authz.user_id))
    answer, trace = await orch.answer(
        body.content,
        authz,
        language=body.language,
        include_historical=body.include_historical,
        history=history,
    )

    raw_citations = answer.diagnostics.get("citations") or ()
    citations: tuple[Citation, ...] = (
        tuple(raw_citations) if isinstance(raw_citations, tuple | list) else ()
    )
    message_id = await repo.append_turn(
        conversation_id=conversation_id,
        user_id=str(authz.user_id),
        question=body.content,
        answer=answer,
        citations=citations,
        correlation_id=get_correlation_id(),
        model_provider=getattr(orch.llm, "provider", None),
        model_name=getattr(orch.llm, "model_name", None),
        ttft_ms=trace.ttft_ms,
        total_ms=trace.total_ms,
    )

    request.app.state.audit.record(
        AuditEvent(
            event_type=(
                EventType.QUERY_SUBMITTED if answer.state.is_answered else EventType.QUERY_REFUSED
            ),
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="conversation",
            resource_id=conversation_id,
            detail={
                "answer_state": answer.state.value,
                "retrieved_documents": (
                    [str(d) for d in answer.retrieval.document_ids] if answer.retrieval else []
                ),
                "groundedness": answer.groundedness,
            },
        )
    )

    return AnswerOut(
        message_id=message_id,
        answer_state=answer.state.value,
        content=answer.content,
        citations=[
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
            )
            for c in citations
        ],
        conflicts=[c.summary for c in answer.conflicts],
        groundedness=answer.groundedness,
        correlation_id=get_correlation_id(),
        timings={
            "retrieval_ms": trace.retrieval_ms,
            "ttft_ms": trace.ttft_ms,
            "total_ms": trace.total_ms,
        },
    )


feedback_router = APIRouter(prefix="/v1/messages", tags=["feedback"])


@feedback_router.post("/{message_id}/stop", status_code=202)
async def stop_generation(message_id: str, authz: AuthzDep, request: Request) -> dict[str, str]:
    """Cancel an in-flight answer.

    Honest about what it does: the SSE connection closing is what actually
    stops generation, and the browser does that when the reader navigates away
    or presses stop. This endpoint records the intent so a partial answer is
    distinguishable from a completed one in the metrics — an abandoned answer
    counted as delivered would inflate the success rate.
    """
    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.QUERY_REFUSED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="message",
            resource_id=message_id,
            detail={"reason": "cancelled_by_user"},
        )
    )
    return {"message_id": message_id, "status": "cancellation_recorded"}


@feedback_router.delete("/{message_id}/feedback", status_code=204)
async def withdraw_feedback(message_id: str, authz: AuthzDep, request: Request) -> None:
    """A rating is an opinion, and people change their minds."""
    if not await _repo(request).withdraw_feedback(message_id, str(authz.user_id)):
        raise NotFoundError("No feedback to withdraw")


@feedback_router.post("/{message_id}/feedback", status_code=204)
async def submit_feedback(
    message_id: str, body: FeedbackIn, authz: AuthzDep, request: Request
) -> None:
    """FR-043/044.

    A reason code is required for negative feedback: "not helpful" with no
    reason cannot be triaged, and an administrator staring at a count has no
    idea whether the answer was wrong, outdated, or simply about the wrong
    document.
    """
    if body.rating is FeedbackRating.NOT_HELPFUL and body.reason is None:
        raise InvalidRequestError("A reason is required when marking an answer not helpful")

    recorded = await _repo(request).record_feedback(
        message_id,
        str(authz.user_id),
        body.rating.value,
        body.reason.value if body.reason else None,
        body.comment,
    )
    if not recorded:
        raise NotFoundError("Message not found")

    request.app.state.audit.record(
        AuditEvent(
            event_type=EventType.FEEDBACK_SUBMITTED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=str(authz.user_id),
            actor_email=authz.email,
            resource_type="message",
            resource_id=message_id,
            # The rating and reason are metadata; the comment is user-authored
            # text and stays out of the audit row (FR-052).
            detail={
                "rating": body.rating.value,
                "reason": body.reason.value if body.reason else None,
            },
        )
    )


@router.post("/{conversation_id}/messages/stream")
async def stream_in_conversation(
    conversation_id: str,
    body: AskRequest,
    authz: AuthzDep,
    orch: OrchestratorDep,
    request: Request,
) -> StreamingResponse:
    """The streaming counterpart, with persistence.

    The turn is written *after* the stream completes rather than incrementally:
    a half-written answer in the history would be indistinguishable from a
    complete one, and the quality metrics computed from `messages` would count
    it as a real answer.
    """
    repo = _repo(request)
    if not await repo.owned_by(conversation_id, str(authz.user_id)):
        raise NotFoundError("Conversation not found")

    history = await repo.recent_turns(conversation_id, str(authz.user_id))
    correlation = get_correlation_id()

    async def events() -> AsyncIterator[str]:
        yield _sse("accepted", {"correlation_id": correlation})
        sources_sent = False
        final: Any = None
        try:
            async for item in orch.stream(
                body.content,
                authz,
                language=body.language,
                include_historical=body.include_historical,
                history=history,
            ):
                if isinstance(item, Stage):
                    yield _sse("stage", {"stage": item.name, "elapsed_ms": item.elapsed_ms})
                elif isinstance(item, str):
                    yield _sse("token", {"text": item})
                elif not sources_sent and item.diagnostics.get("phase") == "sources":
                    sources_sent = True
                    yield _sse("sources", {"citations": _wire_citations(item)})
                else:
                    final = item
                    for conflict in item.conflicts:
                        yield _sse("conflict", {"summary": conflict.summary})
        except Exception as exc:
            yield _sse("error", {"type": type(exc).__name__, "correlation_id": correlation})
            return

        message_id = None
        if final is not None:
            raw = final.diagnostics.get("citations") or ()
            cites: tuple[Citation, ...] = tuple(raw) if isinstance(raw, tuple | list) else ()
            message_id = await repo.append_turn(
                conversation_id=conversation_id,
                user_id=str(authz.user_id),
                question=body.content,
                answer=final,
                citations=cites,
                correlation_id=correlation,
                model_provider=getattr(orch.llm, "provider", None),
                model_name=getattr(orch.llm, "model_name", None),
                ttft_ms=None,
                total_ms=None,
            )
            request.app.state.audit.record(
                AuditEvent(
                    event_type=(
                        EventType.QUERY_SUBMITTED
                        if final.state.is_answered
                        else EventType.QUERY_REFUSED
                    ),
                    outcome=AuditOutcome.SUCCESS,
                    actor_user_id=str(authz.user_id),
                    actor_email=authz.email,
                    resource_type="conversation",
                    resource_id=conversation_id,
                    detail={"answer_state": final.state.value},
                )
            )
            yield _sse(
                "done",
                {
                    "message_id": message_id,
                    "answer_state": final.state.value,
                    "content": final.content,
                    "topics": list(final.diagnostics.get("topics", ()) or ()),
                    "groundedness": final.groundedness,
                    "citations": _wire_citations(final),
                },
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _wire_citations(answer: Any) -> list[dict[str, Any]]:
    from askau.api.v1.routes.ask import _citations_of

    return [c.model_dump(mode="json") for c in _citations_of(answer)]


def _sse(event: str, data: dict[str, Any]) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
