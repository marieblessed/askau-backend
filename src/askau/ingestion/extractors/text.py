"""Plain text and Markdown (FR-012).

The fallback every other format degrades to, and the format the seeded corpus
uses. Markdown has real headings; plain text is treated as one flat section
rather than guessed at, because inventing structure from blank lines produces
heading paths that look authoritative and are not.
"""

from __future__ import annotations

import re

from askau.domain.knowledge import ExtractedBlock, ExtractionResult

_ATX = re.compile(r"^(#{1,6})\s+(.*)$")
_SETEXT = re.compile(r"^(=+|-+)\s*$")
_SECTION_REF = re.compile(r"^\s*(\d+(?:\.\d+){0,3})[.)]?\s+")
_MARKDOWN_SUFFIXES = (".md", ".markdown")


class TextExtractor:
    def extract(self, data: bytes, filename: str) -> ExtractionResult:
        # Documents from mixed sources arrive in mixed encodings; replacing an
        # undecodable byte loses one character, while raising loses the document.
        text = data.decode("utf-8", errors="replace")
        markdown = filename.lower().endswith(_MARKDOWN_SUFFIXES)
        return ExtractionResult(
            blocks=tuple(_markdown_blocks(text) if markdown else _plain_blocks(text)),
            page_count=None,
        )


def markdown_to_blocks(text: str) -> tuple[ExtractedBlock, ...]:
    """Public helper — the seed builds its corpus through the same path a real
    Markdown file would take, so the fixtures exercise the extractor rather
    than bypassing it."""
    return tuple(_markdown_blocks(text))


def _markdown_blocks(text: str) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    heading_path: tuple[str, ...] = ()
    offset = 0
    lines = text.splitlines()
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, offset
        joined = " ".join(" ".join(buffer).split())
        buffer = []
        if not joined:
            return
        blocks.append(
            ExtractedBlock(
                text=joined,
                page=None,
                heading_path=heading_path,
                section_ref=_section_ref(joined) if heading_path else None,
                char_start=offset,
                char_end=offset + len(joined),
            )
        )
        offset += len(joined) + 1

    index = 0
    while index < len(lines):
        line = lines[index].rstrip()

        atx = _ATX.match(line)
        # Setext: a line of === or --- underlines the heading above it.
        setext = (
            _SETEXT.match(lines[index + 1].rstrip())
            if index + 1 < len(lines) and line.strip()
            else None
        )

        if atx or setext:
            flush()
            if atx:
                level, title = min(len(atx.group(1)), 4), atx.group(2).strip()
            else:
                level = 1 if lines[index + 1].strip().startswith("=") else 2
                title = line.strip()
                index += 1
            heading_path = (*heading_path[: level - 1], title)
            blocks.append(
                ExtractedBlock(
                    text=title,
                    page=None,
                    heading_path=heading_path,
                    section_ref=_section_ref(title),
                    is_heading=True,
                    char_start=offset,
                    char_end=offset + len(title),
                )
            )
            offset += len(title) + 1
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
        index += 1

    flush()
    return blocks


def _plain_blocks(text: str) -> list[ExtractedBlock]:
    blocks: list[ExtractedBlock] = []
    offset = 0
    for paragraph in re.split(r"\n\s*\n", text):
        cleaned = " ".join(paragraph.split())
        if not cleaned:
            continue
        blocks.append(
            ExtractedBlock(
                text=cleaned,
                page=None,
                char_start=offset,
                char_end=offset + len(cleaned),
            )
        )
        offset += len(cleaned) + 1
    return blocks


def _section_ref(text: str) -> str | None:
    match = _SECTION_REF.match(text)
    return match.group(1) if match else None
