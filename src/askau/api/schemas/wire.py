"""Wire DTOs. Domain types never reach the network.

The separation matters for one specific reason: ``AuthorizationContext`` holds
the caller's principal set, and a domain object serialized directly would put it
on the wire. The client never needs it, and shipping it would turn a rendering
bug into an information-disclosure bug.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class CitationOut(BaseModel):
    marker: int
    document_id: str
    document_title: str
    source_name: str
    section_ref: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    version_label: str | None = None
    effective_from: date | None = None
    quote: str | None = None
    can_open: bool = True
    #: public | internal | confidential | highly_restricted. The interface uses
    #: this to mark how the answer must be handled, not to decide access —
    #: access was settled before retrieval ran.
    classification: str = "internal"


class AskRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    include_historical: bool = False
    language: str = "en"


class AnswerOut(BaseModel):
    message_id: str | None = None
    answer_state: str
    content: str
    citations: list[CitationOut] = []
    conflicts: list[str] = []
    groundedness: float | None = None
    correlation_id: str
    timings: dict[str, int] = {}


class RetrievedChunkOut(BaseModel):
    chunk_id: int
    document_id: str
    document_title: str
    classification: str
    section_ref: str | None = None
    page_from: int | None = None
    score: float
    matched_both_arms: bool
    excerpt: str


class DebugRetrieveOut(BaseModel):
    question: str
    strategy: str
    took_ms: int
    count: int
    chunks: list[RetrievedChunkOut]


class MeOut(BaseModel):
    user_id: str
    email: str | None
    department: str | None
    roles: list[str]
    #: A summary, never the principal set itself.
    max_classification: str
    principal_count: int


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "error"]
    checks: dict[str, str] = {}
