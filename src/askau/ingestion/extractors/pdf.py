"""PDF extraction (FR-012, FR-014).

PyMuPDF rather than a text-dump library, for one reason: it exposes each span's
page, position and font size. Page numbers are what make "p.12" possible, and
font size is the only signal a PDF gives about heading structure — PDFs have no
semantic headings, just text that happens to be larger.

The heading heuristic is deliberately conservative. A false heading splits a
section that should have stayed whole; a missed heading merges two that should
have been separate. Both hurt citation accuracy, but the missed heading is the
safer failure — the chunk is still correct, just coarser — so the thresholds
lean towards missing rather than inventing.

Scanned PDFs are the High/High programme risk: a page with almost no extractable
text is reported as such rather than silently indexed as an empty document.
"""

from __future__ import annotations

import logging
import re
import statistics

from askau.domain.knowledge import ExtractedBlock, ExtractionResult

_log = logging.getLogger(__name__)

#: A heading is meaningfully larger than body text, not marginally. 1.15 catches
#: real headings while ignoring the half-point drift common in exported PDFs.
_HEADING_SIZE_RATIO = 1.15

#: Headings are short. A large-font paragraph is a pull quote, not a heading.
_MAX_HEADING_WORDS = 14

#: Below this, the page carries no usable text layer — almost certainly a scan.
_SCANNED_CHAR_THRESHOLD = 40

#: "4.3", "4.3.1", "Article 7", "Section 2" — the section labels AUC policy uses.
_SECTION_REF = re.compile(
    r"^\s*(?:(?:Article|Section|Clause|Part)\s+)?(\d+(?:\.\d+){0,3})[.)]?\s+",
    re.IGNORECASE,
)


class PdfExtractor:
    def extract(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "PyMuPDF is required for PDF extraction: pip install '.[ingestion]'"
            ) from exc

        blocks: list[ExtractedBlock] = []
        warnings: list[str] = []
        scanned_pages: list[int] = []
        offset = 0
        heading_path: tuple[str, ...] = ()

        with fitz.open(stream=data, filetype="pdf") as doc:
            body_size = _body_font_size(doc)

            for index, page in enumerate(doc, start=1):
                page_dict = page.get_text("dict")
                page_chars = 0

                for block in page_dict.get("blocks", []):
                    if block.get("type") != 0:  # 0 = text; images are skipped
                        continue
                    text, size, bold = _flatten(block)
                    if not text:
                        continue
                    page_chars += len(text)

                    if _looks_like_heading(text, size, bold, body_size):
                        heading_path = _descend(heading_path, text, size, body_size)
                        blocks.append(
                            ExtractedBlock(
                                text=text,
                                page=index,
                                heading_path=heading_path,
                                section_ref=_section_ref(text),
                                is_heading=True,
                                char_start=offset,
                                char_end=offset + len(text),
                            )
                        )
                    else:
                        blocks.append(
                            ExtractedBlock(
                                text=text,
                                page=index,
                                heading_path=heading_path,
                                section_ref=_section_ref(text) if heading_path else None,
                                char_start=offset,
                                char_end=offset + len(text),
                            )
                        )
                    offset += len(text) + 1

                if page_chars < _SCANNED_CHAR_THRESHOLD:
                    scanned_pages.append(index)

            page_count = doc.page_count

        if scanned_pages:
            # Reported, not swallowed. A scanned page indexed as an empty
            # document is a silent hole in the knowledge base — the reader gets
            # "not enough evidence" for a policy that is demonstrably present.
            warnings.append(
                f"{len(scanned_pages)} page(s) have little or no text layer "
                f"(pages {_summarize(scanned_pages)}) — likely scanned. "
                "Enable OCR for this source to index them."
            )
            _log.warning("%s: %d page(s) appear scanned", filename, len(scanned_pages))

        return ExtractionResult(
            blocks=tuple(blocks),
            page_count=page_count,
            warnings=tuple(warnings),
        )


def _flatten(block: dict[str, object]) -> tuple[str, float, bool]:
    """Collapse a block's spans into text plus its dominant font size and weight."""
    parts: list[str] = []
    sizes: list[float] = []
    bold = False
    lines: list[dict[str, object]] = block.get("lines", [])  # type: ignore[assignment]
    for line in lines:
        for span in line.get("spans", []):  # type: ignore[attr-defined]
            content = span.get("text", "")
            if not content.strip():
                continue
            parts.append(content)
            sizes.append(float(span.get("size", 0)))
            # PyMuPDF packs style into a bit field; bit 4 is bold.
            if int(span.get("flags", 0)) & 2**4:
                bold = True
    text = " ".join(parts).strip()
    text = re.sub(r"\s+", " ", text)
    return text, (max(sizes) if sizes else 0.0), bold


def _body_font_size(doc: object) -> float:
    """The most common span size, taken as body text.

    Median rather than mean: a document with a large title page would drag a
    mean upward and make every real heading look like body text.
    """
    sizes: list[float] = []
    for page in doc:  # type: ignore[attr-defined]
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        sizes.append(float(span.get("size", 0)))
        if len(sizes) > 400:  # a sample is enough; whole-document scan is waste
            break
    return statistics.median(sizes) if sizes else 11.0


def _looks_like_heading(text: str, size: float, bold: bool, body: float) -> bool:
    words = len(text.split())
    if words == 0 or words > _MAX_HEADING_WORDS:
        return False
    if text.endswith((".", ";", ",")) and not _SECTION_REF.match(text):
        # Sentences end in punctuation; headings generally do not. A numbered
        # section label is the exception — "4.3. Reimbursement" is a heading.
        return False
    if size >= body * _HEADING_SIZE_RATIO:
        return True
    # Bold and short and numbered: a heading set at body size, which is common
    # in Word-exported policy documents.
    return bool(bold and words <= 8 and _SECTION_REF.match(text))


def _descend(current: tuple[str, ...], text: str, size: float, body: float) -> tuple[str, ...]:
    """Place a heading in the hierarchy by its size relative to body text.

    Approximate by nature — a PDF has no outline levels unless it was authored
    with them. Depth is capped at three so a document with erratic sizing cannot
    produce an unbounded path.
    """
    ratio = size / body if body else 1.0
    depth = 1 if ratio >= 1.6 else 2 if ratio >= 1.3 else 3
    trimmed = current[: depth - 1]
    return (*trimmed, text)


def _section_ref(text: str) -> str | None:
    match = _SECTION_REF.match(text)
    return match.group(1) if match else None


def _summarize(pages: list[int], limit: int = 6) -> str:
    shown = ", ".join(str(p) for p in pages[:limit])
    return shown if len(pages) <= limit else f"{shown}, …"
