"""Document extraction (FR-012, FR-014).

Extraction is where citation accuracy is decided. `chunks.page_from`,
`heading_path` and `section_ref` exist so an answer can say "§4.3, p.12" — and
those values can only come from here. An extractor that returns a flat string
loses them permanently: chunking, retrieval and citation validation all continue
to work, and every citation quietly loses its locator. Nothing downstream can
detect that, which is why each adapter's job is described as *preserving
position*, not *getting the text out*.

A port with one adapter per format. Adding a format is one file plus one entry
in the dispatch table; nothing else changes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from askau.domain.knowledge import ExtractionResult

_log = logging.getLogger(__name__)


class Extractor(Protocol):
    """Turn bytes into positioned blocks."""

    def extract(self, data: bytes, filename: str) -> ExtractionResult: ...


class UnsupportedFormatError(Exception):
    """Raised for a format outside FR-012. Distinct from an extraction failure:
    one is expected and reported to the source owner, the other is a defect."""

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix
        super().__init__(f"No extractor for {suffix!r}")


#: Suffix → factory. Lazy, because importing every parser at module load costs
#: startup time for formats a given deployment may never see.
_REGISTRY: dict[str, Callable[[], Extractor]] = {}


def _register(suffixes: tuple[str, ...], factory: Callable[[], Extractor]) -> None:
    for suffix in suffixes:
        _REGISTRY[suffix] = factory


def _pdf() -> Extractor:
    from askau.ingestion.extractors.pdf import PdfExtractor

    return PdfExtractor()


def _docx() -> Extractor:
    from askau.ingestion.extractors.docx import DocxExtractor

    return DocxExtractor()


def _xlsx() -> Extractor:
    from askau.ingestion.extractors.xlsx import XlsxExtractor

    return XlsxExtractor()


def _pptx() -> Extractor:
    from askau.ingestion.extractors.pptx import PptxExtractor

    return PptxExtractor()


def _html() -> Extractor:
    from askau.ingestion.extractors.html import HtmlExtractor

    return HtmlExtractor()


def _text() -> Extractor:
    from askau.ingestion.extractors.text import TextExtractor

    return TextExtractor()


_register((".pdf",), _pdf)
_register((".docx", ".doc"), _docx)
_register((".xlsx", ".xls"), _xlsx)
_register((".pptx", ".ppt"), _pptx)
_register((".html", ".htm"), _html)
_register((".txt", ".md", ".markdown"), _text)

SUPPORTED_SUFFIXES = frozenset(_REGISTRY)


def extractor_for(filename: str) -> Extractor:
    suffix = Path(filename).suffix.lower()
    factory = _REGISTRY.get(suffix)
    if factory is None:
        raise UnsupportedFormatError(suffix or "(none)")
    return factory()


def extract(data: bytes, filename: str) -> ExtractionResult:
    """Extract positioned blocks from a document.

    Raises :class:`UnsupportedFormatError` for formats outside FR-012 and lets
    genuine parse failures propagate — the validation gate distinguishes them,
    because "we don't handle .zip" and "this PDF is corrupt" need different
    answers to the source owner.
    """
    return extractor_for(filename).extract(data, filename)
