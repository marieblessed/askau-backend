"""Evidence gate (FR-028) and conflict detection (FR-035)."""

from __future__ import annotations

from datetime import date

from askau.domain.enums import Classification, RetrievalStrategy
from askau.domain.retrieval import ChunkId, DocumentId, RetrievalResult, RetrievedChunk
from askau.rag.conflict import detect
from askau.rag.evidence import EvidenceThresholds, assess


def chunk(
    n: int,
    content: str,
    score: float = 0.03,
    headings: tuple[str, ...] = ("Policy", "1. Rate"),
    effective: date | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=ChunkId(n),
        document_id=DocumentId(f"doc-{n}"),
        content=content,
        score=score,
        document_title=f"Document {n}",
        source_uri=f"https://x/{n}",
        source_name="Library",
        classification=Classification.INTERNAL,
        heading_path=headings,
        effective_from=effective,
    )


def result(*chunks: RetrievedChunk) -> RetrievalResult:
    return RetrievalResult(
        chunks=chunks,
        strategy=RetrievalStrategy.HYBRID,
        candidates_considered=len(chunks),
        took_ms=1,
    )


class TestEvidenceGate:
    def test_empty_retrieval_is_insufficient(self) -> None:
        assert not assess(result()).sufficient

    def test_weak_top_score_is_insufficient(self) -> None:
        assert not assess(result(chunk(1, "Some text.", score=0.001))).sufficient

    def test_strong_match_is_sufficient(self) -> None:
        assessment = assess(
            result(chunk(1, "Annual leave entitlement is thirty working days.")),
            question="What is the annual leave entitlement?",
        )
        assert assessment.sufficient

    def test_out_of_scope_question_is_refused_despite_a_good_rank(self) -> None:
        """FR-009. Every query returns *something*: ranking cannot tell "best of
        a bad set" from "a good match", but vocabulary overlap can."""
        assessment = assess(
            result(chunk(1, "Visitors shall register at the main reception desk.")),
            question="What is the capital of Brazil and the current gold price?",
        )
        assert not assessment.sufficient
        assert "does not discuss" in (assessment.reason or "")

    def test_threshold_is_configurable(self) -> None:
        strict = EvidenceThresholds(min_top_score=0.9)
        assert not assess(result(chunk(1, "Text.", score=0.5)), strict).sufficient


class TestConflictDetection:
    def test_detects_disagreeing_monetary_values(self) -> None:
        conflicts = detect(
            (
                chunk(
                    1,
                    "The allowance is USD 180 per night.",
                    headings=("Circular", "1. Continental Rate"),
                    effective=date(2025, 4, 1),
                ),
                chunk(
                    2,
                    "The allowance is USD 150 per night.",
                    headings=("Handbook", "1. Continental Rate"),
                    effective=date(2024, 6, 1),
                ),
            )
        )
        assert len(conflicts) == 1
        assert "150" in conflicts[0].summary and "180" in conflicts[0].summary

    def test_prefers_the_more_recent_source(self) -> None:
        conflicts = detect(
            (
                chunk(
                    1,
                    "USD 180 per night.",
                    headings=("A", "1. Continental Rate"),
                    effective=date(2025, 4, 1),
                ),
                chunk(
                    2,
                    "USD 150 per night.",
                    headings=("B", "1. Continental Rate"),
                    effective=date(2024, 6, 1),
                ),
            )
        )
        assert conflicts[0].newer_document_id == DocumentId("doc-1")

    def test_agreeing_sources_are_not_a_conflict(self) -> None:
        conflicts = detect(
            (
                chunk(1, "USD 180 per night.", headings=("A", "1. Continental Rate")),
                chunk(2, "USD 180 per night.", headings=("B", "1. Continental Rate")),
            )
        )
        assert conflicts == ()

    def test_unrelated_topics_are_not_a_conflict(self) -> None:
        """A per-diem figure and a leave entitlement are not in disagreement,
        and a notice that fires on unrelated numbers trains users to ignore it."""
        conflicts = detect(
            (
                chunk(
                    1,
                    "The allowance is USD 180 per night.",
                    headings=("Travel Circular", "1. Continental Rate"),
                ),
                chunk(
                    2,
                    "Staff accrue 30 working days of leave.",
                    headings=("Leave Policy", "1. Entitlement"),
                ),
            )
        )
        assert conflicts == ()

    def test_one_document_quoting_two_figures_is_not_a_conflict(self) -> None:
        """A threshold and a ceiling in the same policy is normal drafting."""
        conflicts = detect(
            (
                chunk(
                    1,
                    "Up to USD 200 may be approved locally; above USD 500 needs sign-off.",
                    headings=("Finance", "1. Thresholds"),
                ),
            )
        )
        assert conflicts == ()
