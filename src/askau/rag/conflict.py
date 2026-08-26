"""Cross-source contradiction detection (FR-035).

Where approved sources disagree, the disagreement is surfaced rather than
silently resolved. An institutional inconsistency is information the reader
needs — quietly picking one source hides a governance problem and produces
confident guidance that half the organisation will contradict.

Detection is deliberately narrow: numeric and monetary claims about the same
topic. Broad semantic contradiction detection produces false positives, and a
conflict notice that fires spuriously trains users to ignore it.
"""

from __future__ import annotations

import re
from collections import defaultdict

from askau.domain.answer import SourceConflict
from askau.domain.retrieval import RetrievedChunk

_MONEY = re.compile(
    r"(?i)\b(?:USD|US\$|\$)\s?([\d,]+(?:\.\d+)?)\b|\b([\d,]+(?:\.\d+)?)\s*"
    r"(?:United States dollars|US dollars|USD)\b"
)
_DURATION = re.compile(r"(?i)\b(\d+)\s*\(?\d*\)?\s*(working days|days|hours|months|years)\b")
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "for",
        "and",
        "or",
        "to",
        "in",
        "is",
        "are",
        "shall",
        "per",
        "within",
        "with",
        "by",
        "on",
        "at",
        "that",
        "this",
        "be",
        "may",
        "not",
    }
)


def _numbers(text: str) -> set[str]:
    out = {m.group(1) or m.group(2) for m in _MONEY.finditer(text)}
    out |= {f"{m.group(1)} {m.group(2).lower()}" for m in _DURATION.finditer(text)}
    return {n.replace(",", "") for n in out if n}


def _topic(chunk: RetrievedChunk) -> frozenset[str]:
    """Coarse topic key: heading words plus distinctive content words."""
    words = " ".join(chunk.heading_path).lower().split()
    if not words:
        words = chunk.content.lower().split()[:12]
    return frozenset(w.strip(".,;:()") for w in words if w not in _STOPWORDS and len(w) > 3)


def _leaf(chunk: RetrievedChunk) -> str:
    return chunk.heading_path[-1].lower().strip() if chunk.heading_path else ""


def detect(chunks: tuple[RetrievedChunk, ...]) -> tuple[SourceConflict, ...]:
    """Find numeric disagreements between different documents on one topic.

    Two chunks are the same topic when their section headings match exactly, or
    when they share at least two distinctive terms. Exact heading match is the
    strong signal: two documents both headed "Continental Rate" are talking
    about the same thing even though the heading is only two words long, and a
    word-count threshold alone would miss precisely the conflicts that matter.

    False positives are prevented downstream instead, by requiring the two
    documents to assert *disjoint* value sets — which is a much more reliable
    discriminator than topic similarity.
    """
    conflicts: list[SourceConflict] = []
    by_topic: dict[frozenset[str], list[RetrievedChunk]] = defaultdict(list)
    leaves: dict[frozenset[str], str] = {}

    for chunk in chunks:
        topic = _topic(chunk)
        leaf = _leaf(chunk)
        for existing in by_topic:
            same_heading = bool(leaf) and leaves.get(existing) == leaf
            if same_heading or len(topic & existing) >= 2:
                by_topic[existing].append(chunk)
                break
        else:
            by_topic[topic].append(chunk)
            leaves[topic] = leaf

    for group in by_topic.values():
        documents = {c.document_id for c in group}
        if len(documents) < 2:
            continue

        # Group values by document, then require two documents to assert
        # *disjoint* value sets. Simply seeing two different numbers in a group
        # is not a conflict: one document legitimately quoting two figures
        # (a threshold and a ceiling) is normal, and treating it as a
        # contradiction trains users to ignore the notice.
        by_document: dict[str, set[str]] = defaultdict(set)
        chunk_by_document: dict[str, RetrievedChunk] = {}
        for chunk in group:
            values_here = _numbers(chunk.content)
            if values_here:
                by_document[str(chunk.document_id)] |= values_here
                chunk_by_document.setdefault(str(chunk.document_id), chunk)

        if len(by_document) < 2:
            continue

        disagreeing: dict[str, RetrievedChunk] = {}
        documents_list = sorted(by_document)
        for i, a in enumerate(documents_list):
            for b in documents_list[i + 1 :]:
                if by_document[a] and by_document[b] and not (by_document[a] & by_document[b]):
                    disagreeing[a] = chunk_by_document[a]
                    disagreeing[b] = chunk_by_document[b]
        if len(disagreeing) < 2:
            continue

        involved = {c.document_id: c for c in disagreeing.values()}
        dated = [c for c in involved.values() if c.effective_from]
        newer = max(dated, key=lambda c: c.effective_from).document_id if dated else None  # type: ignore[arg-type,return-value]

        all_values = sorted({v for d in disagreeing for v in by_document[d]})
        values = ", ".join(all_values[:4])
        titles = sorted({c.document_title for c in involved.values()})
        conflicts.append(
            SourceConflict(
                summary=(
                    f"{len(titles)} approved sources state different values "
                    f"({values}) on this point: {'; '.join(titles)}."
                ),
                document_ids=tuple(involved),
                newer_document_id=newer,
            )
        )

    return tuple(conflicts)
