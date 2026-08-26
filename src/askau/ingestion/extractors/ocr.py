"""OCR for scanned documents (FR-014), as a remote service.

OCR needs an engine and a language model per script. Neither can be a Python
package: Tesseract is a system binary, and the deep-learning alternatives pull
model weights at runtime. Installing either on the host makes the application's
behaviour depend on how the machine was provisioned — the exact failure this
module used to have, where an absent binary meant scanned documents indexed as
nothing and no one found out.

So OCR is a **service the application calls**, not a library it links. The
engine, its language data and its CPU cost live in a container the platform team
runs beside PostgreSQL and Redis. Nothing is installed on the API or worker
host, and adding Amharic is a change to that image rather than to this codebase.

The trade this makes explicit: OCR output is *approximate*. A citation quoting
OCR text may not match the authoritative document character for character, which
matters when the point of a citation is that the reader can check it. So the
position is that OCR makes a scanned document **findable**, and the reader is
sent to the original to **read** it — every OCR-derived result carries that
warning, and the document is flagged for review rather than indexed silently
alongside documents with real text layers.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

import httpx

from askau.domain.knowledge import ExtractedBlock, ExtractionResult

_log = logging.getLogger(__name__)

#: OCR on a blank page or a photograph yields stray marks — "|. ~ '" — not
#: words. Length alone is the wrong test for that: a title page reading
#: "OFFICIAL TRAVEL POLICY" is 22 characters of entirely real content, and
#: discarding it loses the document's heading, which is exactly the text
#: citations and chunk `heading_path` depend on. So a page is kept when it
#: carries word-like tokens, not when it is merely long.
_MIN_WORDS = 3
_MIN_WORD_LETTERS = 2

#: OCR is slow by nature: a scanned page is seconds, not milliseconds. This is a
#: ceiling on pathological documents, not a latency target. Ingestion is a
#: background job, so waiting is cheap; a hung worker is not.
_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


class OcrUnavailableError(RuntimeError):
    """The OCR service is not configured, not reachable, or refused the work."""


class OcrEngine(Protocol):
    """Turn a document's bytes into per-page text.

    Deliberately narrow. Everything the caller needs is "give me the text of
    each page"; anything richer would leak one vendor's response shape into the
    pipeline and make the engine hard to replace.
    """

    async def pages(self, data: bytes, filename: str) -> list[str]: ...

    async def health(self) -> tuple[bool, str]: ...


def requested_languages(configured: str) -> tuple[str, ...]:
    """Parse the configured language list, tolerating spacing and empty entries.

    Falls back to English rather than to *nothing*: an empty tuple would leave
    the service to guess, and a guessed script is how a page of Amharic becomes
    a page of plausible Latin nonsense.
    """
    parsed = tuple(part.strip() for part in configured.split(",") if part.strip())
    return parsed or ("eng",)


class TikaOcrEngine:
    """Apache Tika Server (``apache/tika:*-full``), which bundles the engine.

    Chosen because it is an official image with a stable HTTP contract and its
    full variant already carries the language data — so "support Amharic" is an
    image tag, not a code change.

    Text is requested as XHTML rather than plain text specifically to keep page
    boundaries: Tika emits one ``<div class="page">`` per page, and those
    divisions are what make ``p.12`` in a citation possible. Plain text would be
    one string and every page locator would be lost with nothing to detect it.
    """

    #: Tika marks page boundaries with this div; everything between two of them
    #: is one page. Matched loosely because the attribute order is not
    #: guaranteed across Tika versions.
    _PAGE_SPLIT = re.compile(r'<div[^>]*class="[^"]*\bpage\b[^"]*"[^>]*>', re.IGNORECASE)
    _TAGS = re.compile(r"<[^>]+>")

    def __init__(self, base_url: str, languages: tuple[str, ...]) -> None:
        self._base_url = base_url.rstrip("/")
        self._languages = languages

    async def pages(self, data: bytes, filename: str) -> list[str]:
        headers = {
            "Accept": "text/html",
            "Content-Type": "application/octet-stream",
            # Tika passes this through to the OCR engine. Joined with '+'
            # because that is how Tesseract expresses "try these scripts".
            "X-Tika-OCRLanguage": "+".join(self._languages),
            # Without this Tika skips OCR on any PDF that has *some* text layer.
            # We only call it for documents already found to have none, so the
            # conservative default would mean doing nothing and reporting success.
            "X-Tika-PDFOcrStrategy": "ocr_only",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.put(f"{self._base_url}/tika", content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise OcrUnavailableError(
                f"OCR service unreachable at {self._base_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise OcrUnavailableError(f"OCR service returned {response.status_code} for {filename}")
        return self._split_pages(response.text)

    @classmethod
    def _split_pages(cls, html: str) -> list[str]:
        parts = cls._PAGE_SPLIT.split(html)
        # The first fragment is the document preamble before any page div.
        body = parts[1:] if len(parts) > 1 else parts
        return [" ".join(cls._TAGS.sub(" ", part).split()) for part in body]

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.get(f"{self._base_url}/version")
            if response.status_code >= 400:
                return False, f"OCR service returned {response.status_code}"
            return True, response.text.strip() or "reachable"
        except httpx.HTTPError as exc:
            return False, f"OCR service unreachable at {self._base_url}: {exc}"


def _has_words(text: str) -> bool:
    """Whether a page carries language rather than scanner artefacts."""
    words = [
        token
        for token in re.findall(r"[^\W\d_]+", text, re.UNICODE)
        if len(token) >= _MIN_WORD_LETTERS
    ]
    return len(words) >= _MIN_WORDS


async def extract_pdf(engine: OcrEngine, data: bytes, filename: str) -> ExtractionResult:
    """OCR a PDF that has already been found to lack a text layer.

    Only for those. Running this over a document that has real text would
    replace something accurate with a guess, and nothing downstream could tell.
    """
    texts = await engine.pages(data, filename)

    blocks: list[ExtractedBlock] = []
    warnings: list[str] = []
    empty_pages: list[int] = []
    offset = 0

    for number, text in enumerate(texts, start=1):
        if not _has_words(text):
            empty_pages.append(number)
            continue
        blocks.append(
            ExtractedBlock(
                text=text,
                page=number,
                # No heading path: OCR gives characters, not structure. A
                # guessed hierarchy here would be fabricated precision.
                char_start=offset,
                char_end=offset + len(text),
            )
        )
        offset += len(text) + 1

    if empty_pages:
        warnings.append(
            f"OCR produced no usable text for {len(empty_pages)} page(s); "
            "they may be images, diagrams or blank."
        )
    warnings.append("Extracted by OCR. Text is approximate — cite the original document.")

    _log.info("%s: OCR produced %d page(s) of text", filename, len(blocks))
    return ExtractionResult(blocks=tuple(blocks), page_count=len(texts), warnings=tuple(warnings))


def engine_for(provider: str, url: str, languages: tuple[str, ...]) -> OcrEngine | None:
    """Build the configured engine, or ``None`` when OCR is switched off.

    ``None`` is a first-class answer rather than a null object: the caller must
    decide what "no OCR" means for a scanned document, and silently returning an
    engine that produces nothing is how that decision gets skipped.
    """
    if provider == "none":
        return None
    if provider == "tika":
        return TikaOcrEngine(url, languages)
    raise ValueError(f"unknown OCR provider {provider!r}")
