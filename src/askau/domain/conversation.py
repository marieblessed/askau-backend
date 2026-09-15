"""Conversation-side domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from askau.domain.enums import (
    AnswerState,
    Classification,
    FeedbackRating,
    FeedbackReason,
    MessageRole,
)
from askau.domain.retrieval import ChunkId, DocumentId


@dataclass(frozen=True, slots=True)
class Citation:
    """A reference that resolves to a chunk actually present in the context.

    FR-030 forbids fabricated references. ``chunk_id`` is a foreign key in the
    schema, so a citation that resolves to nothing cannot be persisted — the
    guarantee is referential, not prompt-dependent.
    """

    marker: int
    chunk_id: ChunkId
    document_id: DocumentId
    document_title: str
    source_uri: str
    source_name: str
    rank: int
    quote: str | None = None
    heading_path: tuple[str, ...] = ()
    section_ref: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    version_label: str | None = None
    #: Document governance, for the source card: who owns it, what kind of
    #: document it is, whether it is still current, and from when. A reader
    #: deciding whether to act on a quotation needs these more than they need
    #: the passage's rank.
    department: str | None = None
    doc_type: str | None = None
    lifecycle: str | None = None
    effective_from: date | None = None
    #: The sensitivity of the document this citation came from. Carried to the
    #: reader because an answer assembled from restricted material looks exactly
    #: like any other answer on screen — and the reader is the one deciding
    #: whether to forward it, paste it into a mail, or read it aloud in a room.
    #: Authorization already happened; this is about informed handling after it.
    classification: Classification = Classification.INTERNAL
    retrieval_score: float | None = None
    verified: bool = False
    #: FR-031 — a chunk may ground an answer while the user cannot open the
    #: original. Grounding and opening are separate permissions.
    can_open: bool = True


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    user_id: str
    role: MessageRole
    seq: int
    content: str
    created_at: datetime
    correlation_id: str
    answer_state: AnswerState | None = None
    citations: tuple[Citation, ...] = ()
    groundedness: float | None = None
    model_name: str | None = None
    model_provider: str | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    user_id: str
    title: str | None = None
    message_count: int = 0
    last_message_at: datetime | None = None
    is_archived: bool = False
    messages: tuple[Message, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Feedback:
    message_id: str
    user_id: str
    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str | None = None
