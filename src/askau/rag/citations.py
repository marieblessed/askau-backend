"""Citation extraction and validation (FR-029, FR-030).

FR-030 forbids fabricated document references. Two mechanisms enforce it, and
neither depends on the model behaving:

1. **Resolution** — a marker is kept only if it maps to a chunk that was
   actually placed in the context. Unresolvable markers are stripped from the
   answer text, not merely dropped from the citation list; leaving `[7]` visible
   with nothing behind it is itself a fabricated reference.
2. **Referential integrity** — ``citations.chunk_id`` is a foreign key, so a
   citation that resolves to nothing cannot be persisted even if this module
   were bypassed entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from askau.domain.conversation import Citation
from askau.domain.retrieval import ChunkId, DocumentId, RetrievedChunk

_MARKER = re.compile(r"\[(\d{1,2})\]")
#: Left behind when a stripped marker leaves doubled spaces or a space before
#: punctuation.
_ORPHAN_SPACE = re.compile(r" +([.,;:])")
_DOUBLE_SPACE = re.compile(r"  +")


@dataclass(frozen=True, slots=True)
class CitationResult:
    text: str
    citations: tuple[Citation, ...]
    fabricated_markers: tuple[int, ...]
    unused_sources: tuple[int, ...]

    @property
    def had_fabrication(self) -> bool:
        return bool(self.fabricated_markers)


def build_citations(
    answer: str,
    context_chunks: dict[int, RetrievedChunk],
    *,
    openable: frozenset[DocumentId] | None = None,
) -> CitationResult:
    """Resolve the markers in ``answer`` against the chunks actually supplied.

    ``context_chunks`` maps marker number to the chunk placed under it. Anything
    the model cites outside that mapping is fabricated by definition — there was
    no source there to cite.
    """
    seen: dict[int, Citation] = {}
    fabricated: list[int] = []

    for match in _MARKER.finditer(answer):
        marker = int(match.group(1))
        if marker in seen:
            continue
        chunk = context_chunks.get(marker)
        if chunk is None:
            if marker not in fabricated:
                fabricated.append(marker)
            continue
        seen[marker] = _to_citation(marker, len(seen) + 1, chunk, openable)

    cleaned = _strip_markers(answer, fabricated) if fabricated else answer
    used = set(seen)
    unused = tuple(sorted(m for m in context_chunks if m not in used))

    return CitationResult(
        text=cleaned,
        citations=tuple(seen[m] for m in sorted(seen)),
        fabricated_markers=tuple(fabricated),
        unused_sources=unused,
    )


def _to_citation(
    marker: int,
    rank: int,
    chunk: RetrievedChunk,
    openable: frozenset[DocumentId] | None,
) -> Citation:
    return Citation(
        marker=marker,
        chunk_id=ChunkId(chunk.chunk_id),
        document_id=chunk.document_id,
        document_title=chunk.document_title,
        source_uri=chunk.source_uri,
        source_name=chunk.source_name,
        rank=rank,
        quote=_best_quote(chunk.content),
        heading_path=chunk.heading_path,
        section_ref=chunk.section_ref,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        version_label=chunk.version_label,
        department=chunk.department,
        doc_type=chunk.doc_type,
        lifecycle=chunk.lifecycle.value,
        effective_from=chunk.effective_from,
        classification=chunk.classification,
        retrieval_score=chunk.score,
        verified=True,
        # FR-031: a chunk may ground an answer while the user cannot open the
        # original. Grounding and opening are separate permissions.
        can_open=openable is None or chunk.document_id in openable,
    )


def _best_quote(content: str, max_chars: int = 320) -> str:
    """The supporting span shown in the citation drawer.

    Taken verbatim from the chunk so it can be verified by exact substring match
    against the source — a quote that has been paraphrased is not evidence.
    """
    text = content.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: boundary + 1] if boundary > max_chars // 2 else cut).strip() + "…"


def _strip_markers(answer: str, markers: list[int]) -> str:
    """Remove fabricated markers and tidy the punctuation they leave behind."""
    targets = {str(m) for m in markers}
    cleaned = _MARKER.sub(lambda m: "" if m.group(1) in targets else m.group(0), answer)
    cleaned = _ORPHAN_SPACE.sub(r"\1", cleaned)
    return _DOUBLE_SPACE.sub(" ", cleaned).strip()


def verify_quotes(citations: tuple[Citation, ...], chunks: dict[int, RetrievedChunk]) -> bool:
    """Confirm every quote appears verbatim in its chunk.

    Deterministic, and run before any model-based judgement: a fabricated quote
    fails arithmetically rather than on an opinion.
    """
    for citation in citations:
        chunk = chunks.get(citation.marker)
        if chunk is None:
            return False
        if not citation.quote:
            continue
        needle = citation.quote.rstrip("…").strip()
        if needle and needle not in chunk.content:
            return False
    return True
