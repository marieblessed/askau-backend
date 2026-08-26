"""Answer types — the outcome of the RAG pipeline.

Month 2 builds retrieval only; these exist so the stub answer service and the
web shell share the shape the real pipeline will produce in Month 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from askau.domain.enums import AnswerState
from askau.domain.retrieval import DocumentId, RetrievalResult


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """The pre-generation gate (FR-028, ADR-0008).

    Evaluated *before* the model is called. When evidence is insufficient the
    pipeline returns a deterministic refusal and never invokes generation, so
    refusal cannot be talked past by a well-phrased question and costs no tokens.
    """

    sufficient: bool
    top_score: float
    supporting_chunks: int
    reason: str | None = None

    @classmethod
    def insufficient(cls, top_score: float, n: int, reason: str) -> EvidenceAssessment:
        return cls(False, top_score, n, reason)


@dataclass(frozen=True, slots=True)
class SourceConflict:
    """FR-035 — approved sources disagreeing.

    Surfaced rather than silently resolved. Where effective dates exist the more
    recent source is presented first, but the disagreement is never hidden: an
    institutional inconsistency is information the reader needs.
    """

    summary: str
    document_ids: tuple[DocumentId, ...]
    newer_document_id: DocumentId | None = None


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    state: AnswerState
    content: str
    retrieval: RetrievalResult | None = None
    conflicts: tuple[SourceConflict, ...] = ()
    groundedness: float | None = None
    evidence: EvidenceAssessment | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def is_refusal(self) -> bool:
        return not self.state.is_answered
