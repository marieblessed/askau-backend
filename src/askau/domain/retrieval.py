"""Retrieval value objects — what the retriever accepts and returns.

Every adapter (pgvector, OpenSearch, Azure AI Search) speaks these types, which is
what makes the search layer replaceable without touching orchestration (NFR-007).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import NewType

from askau.domain.authz import AuthorizationContext
from askau.domain.enums import Classification, Lifecycle, RetrievalStrategy

ChunkId = NewType("ChunkId", int)
DocumentId = NewType("DocumentId", str)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """A resolved, authorized retrieval request.

    Carries the ``AuthorizationContext`` rather than a user id: an adapter must not
    be able to run without one, and requiring the context as a constructor argument
    makes "forgot to filter" a type error rather than a review catch.
    """

    text: str
    embedding: list[float]
    authz: AuthorizationContext
    candidate_k: int = 60
    top_k: int = 8
    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID
    #: FR-018 — historical content is opt-in, never the default.
    include_historical: bool = False
    department_filter: tuple[str, ...] | None = None
    # Text-search configuration for the *asker's* language. Documents are stemmed
    #: with their own; cross-language keyword matching is not achievable, which is
    #: what the semantic arm is for (03-database-schema.md §3.5).
    ts_config: str = "english"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("retrieval query text cannot be empty")
        if not self.embedding:
            raise ValueError("retrieval query requires an embedding")
        if self.top_k > self.candidate_k:
            raise ValueError("top_k cannot exceed candidate_k")


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One authorized chunk, with everything a citation needs (FR-029).

    A chunk only exists as an instance of this class if it passed the ACL predicate,
    so there is no ``authorized`` flag — unauthorized content is absent, not marked.
    """

    chunk_id: ChunkId
    document_id: DocumentId
    content: str
    score: float

    # citation anchors
    document_title: str
    source_uri: str
    source_name: str
    heading_path: tuple[str, ...] = ()
    section_ref: str | None = None
    page_from: int | None = None
    page_to: int | None = None

    # provenance and currency
    classification: Classification = Classification.INTERNAL
    lifecycle: Lifecycle = Lifecycle.ACTIVE
    #: Owning directorate, and what kind of document this is. Not used by
    #: retrieval — they are carried for the citation, so a reader can see who
    #: owns a policy and whether it is a circular or a manual without opening it.
    department: str | None = None
    doc_type: str | None = None
    version_label: str | None = None
    version_seq: int = 1
    effective_from: date | None = None
    effective_to: date | None = None
    language: str = "en"
    token_count: int = 0

    # scoring detail, for evaluation and debugging
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None

    @property
    def locator(self) -> str:
        """Human-readable position, e.g. ``§4.3 · p.12``."""
        parts: list[str] = []
        if self.section_ref:
            parts.append(f"§{self.section_ref}")
        elif self.heading_path:
            parts.append(self.heading_path[-1])
        if self.page_from:
            span = (
                f"p.{self.page_from}"
                if self.page_to in (None, self.page_from)
                else f"pp.{self.page_from}-{self.page_to}"
            )
            parts.append(span)
        return " · ".join(parts)

    @property
    def matched_both_arms(self) -> bool:
        """Strong relevance signal: found by meaning *and* by wording."""
        return self.semantic_rank is not None and self.keyword_rank is not None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: tuple[RetrievedChunk, ...]
    strategy: RetrievalStrategy
    candidates_considered: int
    took_ms: int
    reranked: bool = False
    diagnostics: dict[str, object] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.chunks)

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    @property
    def document_ids(self) -> frozenset[DocumentId]:
        """The authorized document set — the reference the output guardrail checks
        a generated answer against (FR-038)."""
        return frozenset(c.document_id for c in self.chunks)

    def top(self, n: int) -> tuple[RetrievedChunk, ...]:
        return self.chunks[:n]
