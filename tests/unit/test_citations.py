"""Citation resolution — FR-029, FR-030."""

from __future__ import annotations

from askau.domain.enums import Classification
from askau.domain.retrieval import ChunkId, DocumentId, RetrievedChunk
from askau.rag.citations import build_citations, verify_quotes


def chunk(n: int, content: str = "Staff accrue thirty days of leave.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=ChunkId(n),
        document_id=DocumentId(f"doc-{n}"),
        content=content,
        score=0.5,
        document_title=f"Policy {n}",
        source_uri=f"https://x/{n}",
        source_name="Library",
        classification=Classification.INTERNAL,
        section_ref=str(n),
        page_from=n,
    )


class TestResolution:
    def test_resolves_real_markers(self) -> None:
        result = build_citations(
            "Staff accrue leave [1]. Approval is needed [2].", {1: chunk(1), 2: chunk(2)}
        )
        assert [c.marker for c in result.citations] == [1, 2]
        assert not result.had_fabrication

    def test_repeated_marker_yields_one_citation(self) -> None:
        result = build_citations("A [1]. B [1]. C [1].", {1: chunk(1)})
        assert len(result.citations) == 1

    def test_reports_unused_sources(self) -> None:
        result = build_citations("Only the first [1].", {1: chunk(1), 2: chunk(2)})
        assert result.unused_sources == (2,)


class TestFabrication:
    def test_unresolvable_marker_is_recorded(self) -> None:
        result = build_citations("Claim [1]. Invented [7].", {1: chunk(1)})
        assert result.fabricated_markers == (7,)
        assert result.had_fabrication

    def test_fabricated_marker_is_stripped_from_the_text(self) -> None:
        """Leaving `[7]` visible with nothing behind it is itself a fabricated
        reference — dropping it from the citation list is not enough."""
        result = build_citations("Claim [1]. Invented [7].", {1: chunk(1)})
        assert "[7]" not in result.text
        assert "[1]" in result.text

    def test_stripping_tidies_orphaned_punctuation(self) -> None:
        result = build_citations("A statement [9].", {1: chunk(1)})
        assert result.text == "A statement."

    def test_no_citation_survives_without_a_chunk(self) -> None:
        result = build_citations("Everything invented [3] [4].", {})
        assert result.citations == ()
        assert set(result.fabricated_markers) == {3, 4}


class TestQuotes:
    def test_quote_is_verbatim_from_the_chunk(self) -> None:
        result = build_citations("Claim [1].", {1: chunk(1)})
        assert verify_quotes(result.citations, {1: chunk(1)})

    def test_altered_quote_fails_verification(self) -> None:
        """Deterministic: a fabricated quote fails arithmetically, before any
        model-based judgement is involved."""
        result = build_citations("Claim [1].", {1: chunk(1)})
        assert not verify_quotes(result.citations, {1: chunk(1, "Entirely different text.")})

    def test_long_content_is_truncated_on_a_sentence_boundary(self) -> None:
        body = "First sentence here. " * 40
        result = build_citations("Claim [1].", {1: chunk(1, body)})
        quote = result.citations[0].quote or ""
        assert len(quote) <= 330
        assert quote.endswith("…")


class TestOpenPermission:
    def test_marks_sources_the_user_cannot_open(self) -> None:
        """FR-031 — grounding and opening are separate permissions."""
        result = build_citations(
            "A [1]. B [2].",
            {1: chunk(1), 2: chunk(2)},
            openable=frozenset({DocumentId("doc-1")}),
        )
        by_marker = {c.marker: c for c in result.citations}
        assert by_marker[1].can_open
        assert not by_marker[2].can_open
