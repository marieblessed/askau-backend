"""Chunking — the stage that determines what citations can say."""

from __future__ import annotations

import pytest

from askau.domain.knowledge import ExtractedBlock
from askau.ingestion.chunking import StructureAwareChunker, estimate_tokens, normalize


def block(
    text: str,
    *,
    page: int | None = 1,
    path: tuple[str, ...] = ("Policy",),
    heading: bool = False,
    start: int = 0,
) -> ExtractedBlock:
    return ExtractedBlock(
        text=text,
        page=page,
        heading_path=path,
        is_heading=heading,
        char_start=start,
        char_end=start + len(text),
    )


SENT = "The Commission shall reimburse economy class airfare for official travel. "


class TestNormalize:
    def test_collapses_horizontal_whitespace(self) -> None:
        assert normalize("a    b\tc") == "a b c"

    def test_preserves_paragraph_breaks(self) -> None:
        assert normalize("para one\n\npara two") == "para one\n\npara two"

    def test_collapses_excessive_blank_lines(self) -> None:
        assert normalize("a\n\n\n\n\nb") == "a\n\nb"


class TestHeadingBoundaries:
    def test_never_merges_across_a_heading(self) -> None:
        """A chunk spanning two sections cites one heading while containing text
        from both — a citation that is subtly wrong, which is the worst kind."""
        blocks = (
            block("Annual Leave", path=("Leave", "Annual"), heading=True),
            block("Staff accrue 30 days per year.", path=("Leave", "Annual")),
            block("Sick Leave", path=("Leave", "Sick"), heading=True),
            block("Staff accrue 15 days per year.", path=("Leave", "Sick")),
        )
        chunks = StructureAwareChunker().chunk(blocks)

        assert len(chunks) == 2
        assert "30 days" in chunks[0].content and "15 days" not in chunks[0].content
        assert "15 days" in chunks[1].content and "30 days" not in chunks[1].content
        assert chunks[0].heading_path == ("Leave", "Annual")
        assert chunks[1].heading_path == ("Leave", "Sick")

    def test_heading_text_itself_is_not_emitted_as_a_chunk(self) -> None:
        blocks = (
            block("Travel Policy", heading=True),
            block("Requests are submitted through the portal.", path=("Policy",)),
        )
        chunks = StructureAwareChunker().chunk(blocks)
        assert len(chunks) == 1
        assert "portal" in chunks[0].content


class TestCitationAnchors:
    def test_page_span_covers_every_absorbed_block(self) -> None:
        blocks = tuple(block(SENT * 3, page=p, start=p * 200) for p in (4, 5, 6))
        chunks = StructureAwareChunker(target_tokens=400).chunk(blocks)
        assert chunks[0].page_from == 4
        assert chunks[0].page_to == 6

    def test_single_page_reports_equal_span(self) -> None:
        chunks = StructureAwareChunker().chunk((block(SENT, page=12),))
        assert chunks[0].page_from == chunks[0].page_to == 12

    def test_section_ref_is_carried(self) -> None:
        b = ExtractedBlock(text=SENT, page=3, heading_path=("Travel",), section_ref="4.3")
        chunks = StructureAwareChunker().chunk((b,))
        assert chunks[0].section_ref == "4.3"

    def test_ordinals_are_contiguous_across_sections(self) -> None:
        blocks = (
            block("A" + SENT * 30, path=("One",)),
            block("B" + SENT * 30, path=("Two",)),
        )
        chunks = StructureAwareChunker(target_tokens=100).chunk(blocks)
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))


class TestSizing:
    def test_respects_target_size(self) -> None:
        chunks = StructureAwareChunker(target_tokens=100, overlap_tokens=0).chunk(
            (block(SENT * 40),)
        )
        assert len(chunks) > 1
        # target is a soft boundary — a unit is never split mid-sentence
        assert all(c.token_count <= 160 for c in chunks)

    def test_oversized_unit_is_emitted_not_dropped(self) -> None:
        """A long unbroken table row must survive, even past max_tokens."""
        giant = "x" * 8000  # no sentence boundaries at all
        chunks = StructureAwareChunker(target_tokens=100, max_tokens=200).chunk((block(giant),))
        assert len(chunks) == 1
        assert len(chunks[0].content) == 8000

    def test_no_empty_chunks(self) -> None:
        blocks = (block("   "), block(SENT), block("\n\n"))
        chunks = StructureAwareChunker().chunk(blocks)
        assert all(c.content.strip() for c in chunks)

    def test_empty_input_yields_nothing(self) -> None:
        assert StructureAwareChunker().chunk(()) == ()


class TestOverlap:
    def test_overlap_repeats_trailing_text(self) -> None:
        chunks = StructureAwareChunker(target_tokens=60, overlap_tokens=25).chunk(
            (block(SENT * 20),)
        )
        assert len(chunks) > 1
        tail = chunks[0].content.split(".")[-2].strip()
        assert tail and tail in chunks[1].content

    def test_overlap_does_not_cross_a_heading(self) -> None:
        blocks = (
            block("Alpha unique text here. " * 10, path=("A",)),
            block("Bravo", path=("B",), heading=True),
            block("Bravo unique text here. " * 10, path=("B",)),
        )
        chunks = StructureAwareChunker(target_tokens=50, overlap_tokens=20).chunk(blocks)
        b_chunks = [c for c in chunks if c.heading_path == ("B",)]
        assert b_chunks, "expected chunks in section B"
        assert all("Alpha" not in c.content for c in b_chunks)

    def test_rejects_overlap_larger_than_target(self) -> None:
        with pytest.raises(ValueError, match="overlap must be smaller"):
            StructureAwareChunker(target_tokens=50, overlap_tokens=50)


class TestTokenEstimate:
    def test_never_zero(self) -> None:
        assert estimate_tokens("a") >= 1

    def test_scales_with_length(self) -> None:
        assert estimate_tokens("x" * 400) > estimate_tokens("x" * 40)
