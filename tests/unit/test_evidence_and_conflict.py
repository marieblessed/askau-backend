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


class TestAModelCannotSuppressAConflict:
    """The disagreement is disclosed whether or not the answer quoted both sides.

    Conflicts were once detected only among the chunks the answer *cited*, and
    that let the language model decide whether a contradiction reached the
    reader: quote both figures and the warning appeared, quote one and ignore
    the other and the answer was reported as grounded. Same question, same
    corpus, same retrieval — measured at three times in five against a real
    model.

    A model that can suppress a safety notice by declining to cite it is a model
    deciding a safety question. Detection now spans everything retrieved on a
    topic the answer touched.
    """

    def _chunk(self, doc: str, heading: str, content: str, chunk_id: int):
        from askau.domain.enums import Classification
        from askau.domain.retrieval import RetrievedChunk

        return RetrievedChunk(
            chunk_id=chunk_id,
            document_id=doc,
            document_title=doc,
            source_uri=f"https://example/{doc}",
            source_name="test",
            content=content,
            heading_path=(heading,),
            classification=Classification.INTERNAL,
            score=1.0,
        )

    def test_a_conflict_the_answer_ignored_is_still_reported(self) -> None:
        from askau.rag import conflict as conflict_mod

        cited = self._chunk("Circular", "Continental Rate", "The rate is USD 180 per night.", 1)
        ignored = self._chunk("Handbook", "Continental Rate", "The rate is USD 150 per night.", 2)

        # What the old behaviour did — only the cited chunk — finds nothing.
        assert conflict_mod.detect((cited,)) == ()

        # Considering everything retrieved on that topic finds the disagreement.
        conflicts = conflict_mod.detect((cited,), (cited, ignored))
        assert len(conflicts) == 1
        assert "180" in conflicts[0].summary and "150" in conflicts[0].summary

    def test_an_unrelated_disagreement_is_not_dragged_in(self) -> None:
        """The rule the original design was right about, and which must survive.

        A per-diem contradiction has no business attaching itself to a question
        about annual leave. A notice that fires on unrelated questions is one
        people learn to ignore, which costs more than the notice is worth.
        """
        from askau.rag import conflict as conflict_mod

        cited = self._chunk("Leave", "Annual Leave", "Staff accrue 30 days per year.", 1)
        unrelated_a = self._chunk("Circular", "Continental Rate", "USD 180 per night.", 2)
        unrelated_b = self._chunk("Handbook", "Continental Rate", "USD 150 per night.", 3)

        assert conflict_mod.detect((cited,), (cited, unrelated_a, unrelated_b)) == ()

    def test_it_is_deterministic_for_a_fixed_retrieval(self) -> None:
        """Whichever side the model happened to quote, the verdict is the same."""
        from askau.rag import conflict as conflict_mod

        a = self._chunk("Circular", "Continental Rate", "The rate is USD 180 per night.", 1)
        b = self._chunk("Handbook", "Continental Rate", "The rate is USD 150 per night.", 2)
        retrieved = (a, b)

        cited_a_only = conflict_mod.detect((a,), retrieved)
        cited_b_only = conflict_mod.detect((b,), retrieved)
        cited_both = conflict_mod.detect((a, b), retrieved)

        assert len(cited_a_only) == len(cited_b_only) == len(cited_both) == 1
