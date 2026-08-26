"""Context assembly (FR-026, FR-039, FR-040).

Builds the block of authorized material the model sees, within a token budget.
The budget is a security control as much as a cost control: FR-039 requires that
only the minimum necessary information reaches the model, and an unbounded
context is the difference between exposing eight chunks and exposing eighty.

Every chunk passes through the context shield on the way in, so no unshielded
document text can reach the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from askau.domain.retrieval import RetrievedChunk
from askau.rag.guardrails.context_shield import shield


@dataclass(frozen=True, slots=True)
class AssembledContext:
    text: str
    chunks_by_marker: dict[int, RetrievedChunk]
    tokens_used: int
    dropped_for_budget: int = 0
    injection_detections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.chunks_by_marker


def assemble(
    chunks: tuple[RetrievedChunk, ...],
    *,
    token_budget: int = 6000,
    max_chunks: int = 8,
) -> AssembledContext:
    parts: list[str] = []
    by_marker: dict[int, RetrievedChunk] = {}
    detections: list[str] = []
    used = 0
    dropped = 0

    for chunk in chunks[:max_chunks]:
        marker = len(by_marker) + 1
        report = shield(marker, chunk.document_title, chunk.locator, chunk.content)
        cost = _estimate(report.text)

        if used + cost > token_budget:
            # Stop rather than truncate: half a policy clause is worse than no
            # clause, because it reads as complete.
            dropped = len(chunks[:max_chunks]) - len(by_marker)
            break

        parts.append(_provenance(marker, chunk) + "\n" + report.text)
        by_marker[marker] = chunk
        used += cost
        detections.extend(report.detections)

    return AssembledContext(
        text="\n\n".join(parts),
        chunks_by_marker=by_marker,
        tokens_used=used,
        dropped_for_budget=dropped,
        injection_detections=tuple(dict.fromkeys(detections)),
    )


def _provenance(marker: int, chunk: RetrievedChunk) -> str:
    """Source metadata the model needs to cite accurately (FR-026).

    Version label and effective dates are included because an answer that cites
    the right document but the wrong revision is still wrong guidance.
    """
    bits = [f"Source [{marker}]: {chunk.document_title}"]
    if chunk.version_label:
        bits.append(f"version {chunk.version_label}")
    if chunk.effective_from:
        bits.append(f"effective {chunk.effective_from.isoformat()}")
    if chunk.locator:
        bits.append(chunk.locator)
    bits.append(f"repository: {chunk.source_name}")
    return " · ".join(bits)


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)
