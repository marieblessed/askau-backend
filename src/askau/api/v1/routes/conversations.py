"""Conversations — history, follow-ups, and feedback (FR-004 … FR-006, FR-043/044)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from askau.api.deps import AuthzDep, OrchestratorDep
from askau.api.mappers import live_state, stored_message_to_wire
from askau.api.schemas.wire import (
    AcceptedEvent,
    AnswerOut,
    AskRequest,
    CitationOut,
    ConflictEvent,
    ConversationDetailOut,
    ConversationOut,
    DoneEvent,
    ErrorEvent,
    ListResponse,
    MessageOut,
    SourceOut,
    SourcesEvent,
    StageEvent,
    TokenEvent,
    WireModel,
)
from askau.api.sse import sse
from askau.audit.events import EventType
from askau.audit.writer import AuditEvent
from askau.core.correlation import get_correlation_id
from askau.core.errors import InvalidRequestError, NotFoundError
from askau.db.conversations import ConversationRepository
from askau.domain.conversation import Citation
from askau.domain.enums import AuditOutcome, FeedbackRating, FeedbackReason
from askau.rag.orchestrator import Stage

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


class NewConversation(WireModel):
    title: str | None = Field(default=None, max_length=200)


class FeedbackIn(WireModel):
    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str | None = Field(default=None, max_length=2000)


def _repo(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.conversations


async def _accessible(repo: ConversationRepository, conversation_id: str, user_id: str) -> bool:
    """Whether this caller may ask inside this conversation.

    Ownership first, unchanged. The second clause admits one further case: an
    id with **no row at all**, belonging to a caller who has history switched
    off — the ephemeral handle `create` mints above.

    It discloses nothing, and the reason is worth stating precisely rather than
    trusting. Another person's conversation *has* a row, so `owned_by` answers
    false for it and the second clause never sees it — the only ids admitted are
    ones no conversation exists under, which have no history to read and no
    owner to impersonate. `recent_turns` on such an id returns empty, so a
    guessed uuid buys a stranger exactly what an unguessed one does.

    The condition is here, in one named place, rather than inlined at the two
    call sites, because a security predicate that grows a special case in two
    copies is one edit away from disagreeing with itself.
    """
    if await repo.owned_by(conversation_id, user_id):
        return True
    prefs = await repo.preferences(user_id)
    return not prefs.save_history and not await repo.exists(conversation_id)


@router.post("", status_code=201)
async def create(body: NewConversation, authz: AuthzDep, request: Request) -> dict[str, str]:
    """Start a conversation. With history off, the id is minted and nothing is written.

    The client needs an id before it has an answer — streaming, citation
    resolution and feedback all key off it — but "save conversation history"
    has to mean no row. An unwritten uuid4 satisfies both: it is a correlation
    handle for the length of the request and nothing afterwards.

    Without this, the promise leaks in a way that is visible on screen. The row
    was being written, held no message, and still appeared in the history list —
    so switching the setting off produced a growing list of blank conversations,
    which is the opposite of what it claims.
    """
    repo = _repo(request)
    if not (await repo.preferences(str(authz.user_id))).save_history:
        return {"id": str(uuid4())}
    return {"id": await repo.create(str(authz.user_id), body.title)}


@router.get("", response_model=ListResponse[ConversationOut])
async def list_own(
    authz: AuthzDep,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="pageSize")] = 20,
) -> ListResponse[ConversationOut]:
    """FR-006. Ownership is a predicate inside the query, so another user's
    conversation is indistinguishable from one that does not exist.

    Paginated with `page`/`pageSize` because that is what the client's
    `ListResponse` declares. `pageSize` takes an alias so a camelCase query
    string works, which is how their client would build it.
    """
    repo = _repo(request)
    items = await repo.list_for(str(authz.user_id), page_size, offset=(page - 1) * page_size)
    total = await repo.count_for(str(authz.user_id))
    return ListResponse[ConversationOut].of(
        [
            ConversationOut(
                id=c.id,
                user_id=str(authz.user_id),
                title=c.title,
                status=c.status,
                message_count=c.message_count,
                last_message_preview=c.last_message_preview,
                created_at=c.created_at.isoformat(),
                updated_at=(c.last_message_at or c.created_at).isoformat(),
            )
            for c in items
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def read(conversation_id: str, authz: AuthzDep, request: Request) -> ConversationDetailOut:
    repo = _repo(request)
    messages = await repo.history(conversation_id, str(authz.user_id))
    title = await repo.title_of(conversation_id, str(authz.user_id))
    if not messages and title is None:
        # 404 rather than 403 for someone else's conversation: confirming that
        # an id exists but is not yours is itself a disclosure (§6.5).
        raise NotFoundError("Conversation not found")

    wire = [stored_message_to_wire(m, conversation_id) for m in messages]
    created = messages[0].created_at if messages else None
    updated = messages[-1].created_at if messages else None
    return ConversationDetailOut(
        id=conversation_id,
        user_id=str(authz.user_id),
        title=title,
        message_count=len(messages),
        last_message_preview=messages[-1].content[:140] if messages else None,
        created_at=created.isoformat() if created else "",
        updated_at=updated.isoformat() if updated else "",
        messages=wire,
    )


@router.get("/{conversation_id}/messages", response_model=ListResponse[MessageOut])
async def read_messages(
    conversation_id: str,
    authz: AuthzDep,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 50,
) -> ListResponse[MessageOut]:
    """The transcript on its own (FR-006).

    Their client asks for this separately from the conversation, so a long
    history can be paged without re-fetching the conversation around it. Same
    ownership predicate: `history` carries it, and a conversation belonging to
    somebody else returns empty rather than forbidden.
    """
    repo = _repo(request)
    messages = await repo.history(conversation_id, str(authz.user_id))
    if not messages and await repo.title_of(conversation_id, str(authz.user_id)) is None:
        raise NotFoundError("Conversation not found")

    start = (page - 1) * page_size
    window = messages[start : start + page_size]
    return ListResponse[MessageOut].of(
        [stored_message_to_wire(m, conversation_id) for m in window],
        total=len(messages),
        page=page,
        page_size=page_size,
    )


class RenameIn(WireModel):
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
    if not await _accessible(repo, conversation_id, str(authz.user_id)):
        raise NotFoundError("Conversation not found")

    history = await repo.recent_turns(conversation_id, str(authz.user_id))
    # One read, used twice below: whether to escalate retrieval and whether to
    # keep the turn. Reading it once also means the two decisions cannot
    # disagree because the setting changed mid-request.
    prefs = await repo.preferences(str(authz.user_id))
    answer, trace = await orch.answer(
        body.content,
        authz,
        language=body.language,
        include_historical=body.include_historical,
        history=history,
        allow_escalation=prefs.higher_intelligence,
    )

    raw_citations = answer.diagnostics.get("citations") or ()
    citations: tuple[Citation, ...] = (
        tuple(raw_citations) if isinstance(raw_citations, tuple | list) else ()
    )
    # A person who turned history off gets an answer and no record of it. The
    # `message_id` is then None, which their `AnswerActions` already reads as
    # "hide the feedback control" — feedback has a foreign key to `messages`, so
    # with nothing stored there is nothing to rate. That is the honest cost of
    # the setting rather than a gap: FR-043 cannot apply to a turn that was
    # never written.
    message_id = (
        await repo.append_turn(
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
        if prefs.save_history
        else None
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
                # Recorded because escalation spends more per question. An
                # operator asking "why did usage rise" can attribute it here
                # rather than inferring it from latency.
                "escalated": trace.escalated,
            },
        )
    )

    return AnswerOut(
        message_id=message_id,
        state=live_state(answer.state.value, _sources_of(answer)),
        sources=_sources_of(answer),
        grounding_count=len(_sources_of(answer)) or None,
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
    if not await _accessible(repo, conversation_id, str(authz.user_id)):
        raise NotFoundError("Conversation not found")

    history = await repo.recent_turns(conversation_id, str(authz.user_id))
    correlation = get_correlation_id()

    # Read before the generator starts, for the same reason as `/ask/stream`:
    # once the response has begun, a failed query can no longer become a status.
    prefs = await repo.preferences(str(authz.user_id))

    async def events() -> AsyncIterator[str]:
        yield sse("accepted", AcceptedEvent(correlation_id=correlation))
        sources_sent = False
        final: Any = None
        try:
            async for item in orch.stream(
                body.content,
                authz,
                language=body.language,
                include_historical=body.include_historical,
                history=history,
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
                        SourcesEvent(sources=_sources_of(item), citations=_wire_citations(item)),
                    )
                else:
                    final = item
                    for conflict in item.conflicts:
                        yield sse("conflict", ConflictEvent(summary=conflict.summary))
        except Exception as exc:
            yield sse("error", ErrorEvent(type=type(exc).__name__, correlation_id=correlation))
            return

        message_id = None
        if final is not None and prefs.save_history:
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
            yield sse(
                "done",
                DoneEvent(
                    message_id=message_id,
                    state=live_state(final.state.value, _sources_of(final)),
                    sources=_sources_of(final),
                    grounding_count=len(_sources_of(final)) or None,
                    answer_state=final.state.value,
                    content=final.content,
                    topics=[str(t) for t in (final.diagnostics.get("topics") or ())],
                    groundedness=final.groundedness,
                    citations=_wire_citations(final),
                ),
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sources_of(answer: Any) -> list[SourceOut]:
    from askau.api.v1.routes.ask import _sources_of as _impl

    return list(_impl(answer))


def _wire_citations(answer: Any) -> list[CitationOut]:
    from askau.api.v1.routes.ask import _citations_of

    return list(_citations_of(answer))
