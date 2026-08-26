"""Knowledge-side domain types: sources, documents, chunks awaiting indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from askau.domain.enums import (
    Classification,
    IngestStatus,
    Lifecycle,
    SourceStatus,
    SourceType,
    ts_config_for,
)


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    id: str
    name: str
    source_type: SourceType
    business_owner_id: str
    department: str
    default_classification: Classification
    location: dict[str, object]
    status: SourceStatus = SourceStatus.DRAFT
    approved_by: str | None = None
    sync_cron: str | None = None

    @property
    def may_ingest(self) -> bool:
        """BR-001 — only approved sources may be indexed.

        Also enforced by the ``active_requires_approval`` CHECK constraint. This
        property is the readable form; the constraint is the one that cannot be
        bypassed by a code path that forgets to consult it.
        """
        return self.status == SourceStatus.ACTIVE and self.approved_by is not None


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Governance and currency metadata carried onto every chunk."""

    document_id: str
    family_id: str
    source_id: str
    title: str
    source_uri: str
    classification: Classification
    language: str = "en"
    department: str | None = None
    owner_user_id: str | None = None
    doc_type: str | None = None
    version_label: str | None = None
    version_seq: int = 1
    lifecycle: Lifecycle = Lifecycle.DRAFT
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    @property
    def ts_config(self) -> str:
        return ts_config_for(self.language)

    def is_effective_on(self, when: date) -> bool:
        if self.effective_from and when < self.effective_from:
            return False
        return not (self.effective_to and when > self.effective_to)


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    """A positioned unit of text from a document.

    Position is preserved from extraction because it is what makes page-accurate
    citation possible at all (FR-029). Discard it here and no later stage can
    recover it.
    """

    text: str
    page: int | None = None
    heading_path: tuple[str, ...] = ()
    section_ref: str | None = None
    char_start: int = 0
    char_end: int = 0
    is_heading: bool = False


@dataclass(frozen=True, slots=True)
class PendingChunk:
    """A chunk that has been cut but not yet embedded or indexed."""

    ordinal: int
    content: str
    token_count: int
    heading_path: tuple[str, ...] = ()
    section_ref: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    char_start: int = 0
    char_end: int = 0

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError(f"chunk {self.ordinal} is empty")


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    blocks: tuple[ExtractedBlock, ...]
    page_count: int | None = None
    detected_language: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """FR-013 / FR-050 — a rejection an administrator can act on.

    ``code`` is a closed vocabulary so failures aggregate in the admin console;
    ``remedy`` says what to do, because "validation_failed" alone sends the
    administrator back to the engineer.
    """

    code: str
    message: str
    remedy: str


@dataclass(frozen=True, slots=True)
class DocumentIngestState:
    document_id: str
    status: IngestStatus
    chunk_count: int = 0
    indexed_at: datetime | None = None
    error: ValidationFailure | None = None
    injection_risk: int = 0
    stage_timings: dict[str, int] = field(default_factory=dict)
