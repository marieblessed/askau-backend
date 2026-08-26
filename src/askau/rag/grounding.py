"""Groundedness scoring (FR-027, FR-033).

Measures how much of an answer is actually supported by the retrieved chunks.
Deliberately lexical rather than model-based: this runs inline on every answer,
so it must be fast and deterministic. The model-based judge in the evaluation
harness is the more sensitive instrument, and it runs offline on a sample.

The lexical score is a floor, not a verdict — it reliably catches an answer that
has drifted away from its sources, and does not claim to catch subtle
misstatement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\w+", re.UNICODE)
_CITE = re.compile(r"\[\d+\]")
_STOP = frozenset(
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
        "be",
        "by",
        "on",
        "at",
        "as",
        "with",
        "that",
        "this",
        "it",
        "from",
        "may",
        "not",
        "which",
        "their",
        "they",
        "you",
        "your",
        "can",
        "will",
        "has",
        "have",
    }
)


@dataclass(frozen=True, slots=True)
class GroundingReport:
    score: float
    supported_sentences: int
    total_sentences: int
    unsupported: tuple[str, ...]

    @property
    def is_grounded(self) -> bool:
        return self.score >= 0.75

    @property
    def is_partial(self) -> bool:
        return 0.4 <= self.score < 0.75


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP and len(w) > 2}


def score(answer: str, evidence: str, *, threshold: float = 0.55) -> GroundingReport:
    """Fraction of answer sentences whose content words appear in the evidence."""
    stripped = _CITE.sub("", answer)
    sentences = [s.strip() for s in _SENTENCE.split(stripped) if len(s.strip()) > 15]
    if not sentences:
        return GroundingReport(1.0, 0, 0, ())

    evidence_words = _content_words(evidence)
    supported = 0
    unsupported: list[str] = []

    for sentence in sentences:
        words = _content_words(sentence)
        if not words:
            supported += 1
            continue
        overlap = len(words & evidence_words) / len(words)
        if overlap >= threshold:
            supported += 1
        else:
            unsupported.append(sentence)

    return GroundingReport(
        score=supported / len(sentences),
        supported_sentences=supported,
        total_sentences=len(sentences),
        unsupported=tuple(unsupported[:5]),
    )
