"""Structure-aware chunking.

Chunking decides what citations can say. A chunker that concatenates text and
cuts every N characters produces chunks that cannot state which section or page
they came from, and no later stage can recover that — so FR-029's "document
title, section, page" becomes unimplementable at ingestion time, not at citation
time.

This chunker therefore never merges across a heading boundary, and carries the
heading path and page span of every block it absorbs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from askau.domain.knowledge import ExtractedBlock, PendingChunk

#: Rough tokens-per-character for policy prose. Deliberately not a real tokenizer:
#: chunk sizing needs a cheap, stable estimate, and tying ingestion to a specific
#: model's tokenizer would make chunk boundaries change when the model changes.
_CHARS_PER_TOKEN = 4

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ɏ؀-ۿ])")
_WHITESPACE = re.compile(r"[ \t]+")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def normalize(text: str) -> str:
    """Collapse horizontal whitespace, preserve paragraph structure.

    Extractors emit ragged spacing from PDF layout; leaving it in wastes context
    budget and makes exact-quote verification in citation validation unreliable.
    """
    text = _WHITESPACE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


class StructureAwareChunker:
    """Cuts a document into retrievable chunks that preserve their position.

    Two rules define the behaviour:

    1. **Never merge across a heading.** A chunk spanning "Annual Leave" and
       "Sick Leave" cites one heading while containing text from both, which
       produces a citation that is subtly wrong — the worst kind, because it
       looks right.
    2. **Overlap only within a section.** Overlap exists so a sentence split
       across a boundary is still retrievable; carrying it across a heading would
       reintroduce rule 1 through the back door.
    """

    def __init__(
        self,
        target_tokens: int = 380,
        overlap_tokens: int = 60,
        max_tokens: int = 512,
    ) -> None:
        if overlap_tokens >= target_tokens:
            raise ValueError("overlap must be smaller than target, or chunking cannot progress")
        if target_tokens > max_tokens:
            raise ValueError("target cannot exceed max")
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens
        self.max_tokens = max_tokens

    def chunk(self, blocks: tuple[ExtractedBlock, ...]) -> tuple[PendingChunk, ...]:
        out: list[PendingChunk] = []
        for section in self._sections(blocks):
            out.extend(self._chunk_section(section, start_ordinal=len(out)))
        return tuple(out)

    # ── internals ───────────────────────────────────────────────────────────

    def _sections(self, blocks: tuple[ExtractedBlock, ...]) -> Iterator[tuple[ExtractedBlock, ...]]:
        """Group blocks into runs that share a heading path."""
        current: list[ExtractedBlock] = []
        current_path: tuple[str, ...] | None = None

        for block in blocks:
            if block.is_heading:
                if current:
                    yield tuple(current)
                    current = []
                current_path = block.heading_path
                continue
            if current_path is not None and block.heading_path != current_path and current:
                yield tuple(current)
                current = []
            current_path = block.heading_path
            current.append(block)

        if current:
            yield tuple(current)

    def _chunk_section(
        self, blocks: tuple[ExtractedBlock, ...], start_ordinal: int
    ) -> list[PendingChunk]:
        if not blocks:
            return []

        heading_path = blocks[0].heading_path
        section_ref = blocks[0].section_ref
        units = self._split_units(blocks)
        if not units:
            return []

        chunks: list[PendingChunk] = []
        buffer: list[tuple[str, int | None, int, int]] = []
        buffer_tokens = 0

        def flush() -> None:
            nonlocal buffer, buffer_tokens
            if not buffer:
                return
            text = normalize(" ".join(t for t, _, _, _ in buffer))
            if not text:
                buffer, buffer_tokens = [], 0
                return
            pages = [p for _, p, _, _ in buffer if p is not None]
            chunks.append(
                PendingChunk(
                    ordinal=start_ordinal + len(chunks),
                    content=text,
                    token_count=estimate_tokens(text),
                    heading_path=heading_path,
                    section_ref=section_ref,
                    page_from=min(pages) if pages else None,
                    page_to=max(pages) if pages else None,
                    char_start=buffer[0][2],
                    char_end=buffer[-1][3],
                )
            )
            buffer, buffer_tokens = self._carry_overlap(buffer)
            buffer_tokens = sum(estimate_tokens(t) for t, _, _, _ in buffer)

        for unit in units:
            unit_tokens = estimate_tokens(unit[0])

            # A single oversized unit (a long table row, an unbroken paragraph)
            # is emitted alone rather than silently truncated.
            if unit_tokens > self.max_tokens:
                flush()
                buffer, buffer_tokens = [unit], unit_tokens
                flush()
                buffer, buffer_tokens = [], 0
                continue

            if buffer_tokens + unit_tokens > self.target_tokens and buffer:
                flush()

            buffer.append(unit)
            buffer_tokens += unit_tokens

        flush()
        # The final flush leaves overlap in the buffer; that is carry-forward for
        # a chunk that will never come, so discard it rather than emit a duplicate.
        return chunks

    def _split_units(
        self, blocks: tuple[ExtractedBlock, ...]
    ) -> list[tuple[str, int | None, int, int]]:
        """Break blocks into sentence-ish units, keeping each unit's position."""
        units: list[tuple[str, int | None, int, int]] = []
        for block in blocks:
            text = normalize(block.text)
            if not text:
                continue
            offset = block.char_start
            for sentence in _SENTENCE_END.split(text):
                sentence = sentence.strip()
                if not sentence:
                    continue
                units.append((sentence, block.page, offset, offset + len(sentence)))
                offset += len(sentence) + 1
        return units

    def _carry_overlap(
        self, buffer: list[tuple[str, int | None, int, int]]
    ) -> tuple[list[tuple[str, int | None, int, int]], int]:
        """Take trailing units up to the overlap budget, for the next chunk."""
        if self.overlap_tokens <= 0:
            return [], 0
        carried: list[tuple[str, int | None, int, int]] = []
        total = 0
        for unit in reversed(buffer):
            t = estimate_tokens(unit[0])
            if total + t > self.overlap_tokens:
                break
            carried.insert(0, unit)
            total += t
        return carried, total
