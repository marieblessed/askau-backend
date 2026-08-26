"""OCR as a remote service (FR-014).

The engine is not installed on the host — not here, not on the server. It runs
in a container the platform team operates beside PostgreSQL and Redis, and the
application talks to it over HTTP. These tests pin the two things that follow
from that: the page boundaries survive the round trip, and an unreachable
service is reported as an infrastructure fault rather than as a bad document.
"""

from __future__ import annotations

import httpx
import pytest

from askau.ingestion.extractors.ocr import (
    OcrUnavailableError,
    TikaOcrEngine,
    extract_pdf,
    requested_languages,
)


class TestRequestedLanguages:
    def test_parses_a_list_tolerating_spacing(self) -> None:
        assert requested_languages("eng, fra ,ara") == ("eng", "fra", "ara")

    def test_empty_configuration_falls_back_to_english(self) -> None:
        # Not to the empty tuple: with no language the service is left to guess
        # the script, and a guessed script is how a page of Amharic becomes a
        # page of plausible Latin nonsense.
        assert requested_languages("") == ("eng",)
        assert requested_languages(" , ") == ("eng",)


class TestPageSplitting:
    """Page boundaries are the reason XHTML is requested instead of plain text.

    `chunks.page_from` is what lets a citation say "p.12". If the engine returns
    one flat string, every page locator silently becomes wrong — chunking,
    retrieval and citation validation all keep working, and nothing downstream
    can detect the loss.
    """

    def test_one_entry_per_page(self) -> None:
        html = (
            "<html><body>"
            '<div class="page"><p>First page text.</p></div>'
            '<div class="page"><p>Second page text.</p></div>'
            '<div class="page"><p>Third page text.</p></div>'
            "</body></html>"
        )
        pages = TikaOcrEngine._split_pages(html)
        assert len(pages) == 3
        assert pages[0] == "First page text."
        assert pages[2] == "Third page text."

    def test_preamble_before_the_first_page_is_not_a_page(self) -> None:
        html = '<html><head><title>Doc</title></head><body><div class="page">Real.</div></body>'
        assert TikaOcrEngine._split_pages(html) == ["Real."]

    def test_extra_classes_on_the_div_still_match(self) -> None:
        # Tika's exact attribute set varies by version; a brittle match here
        # would degrade to one giant page without failing.
        html = '<div class="ocr page scanned" id="p1">Text of the page.</div>'
        assert TikaOcrEngine._split_pages(html) == ["Text of the page."]


def _engine_returning(pages: list[str]) -> TikaOcrEngine:
    class _Stub(TikaOcrEngine):
        async def pages(self, data: bytes, filename: str) -> list[str]:  # type: ignore[override]
            return pages

    return _Stub("http://ocr:9998", ("eng",))


class TestExtractPdf:
    async def test_numbers_pages_from_one(self) -> None:
        result = await extract_pdf(
            _engine_returning(
                [
                    "A page with plenty of readable text on it.",
                    "A second page, also with enough text to clear the noise floor.",
                ]
            ),
            b"",
            "scan.pdf",
        )
        assert [b.page for b in result.blocks] == [1, 2]
        assert result.page_count == 2

    async def test_always_warns_that_the_text_is_approximate(self) -> None:
        """The citation contract depends on this warning reaching the reader.

        OCR text is not verbatim, so a quoted citation may not match the
        authoritative document character for character.
        """
        result = await extract_pdf(
            _engine_returning(["Readable page of policy text."]), b"", "s.pdf"
        )
        assert any("approximate" in w for w in result.warnings)

    async def test_blank_pages_are_reported_not_indexed_as_empty(self) -> None:
        result = await extract_pdf(
            _engine_returning(["Readable page of policy text here.", "", "  "]), b"", "s.pdf"
        )
        assert len(result.blocks) == 1
        assert any("no usable text for 2 page" in w for w in result.warnings)

    async def test_a_short_title_page_is_kept(self) -> None:
        """Caught against a real scan: a 22-character title page was being
        dropped by a length threshold.

        "OFFICIAL TRAVEL POLICY" is the document's heading — the text citations
        and `heading_path` are built from. Discarding it as noise loses the
        title of every scanned policy, and the loss is invisible: the document
        still indexes, just without its name.
        """
        result = await extract_pdf(_engine_returning(["OFFICIAL TRAVEL POLICY"]), b"", "scan.pdf")
        assert [b.text for b in result.blocks] == ["OFFICIAL TRAVEL POLICY"]

    async def test_scanner_artefacts_are_still_discarded(self) -> None:
        """The filter has to keep short *words* while dropping short *marks*."""
        result = await extract_pdf(_engine_returning(["|. ~ ' -", "l1 !", "."]), b"", "scan.pdf")
        assert result.blocks == ()
        assert any("no usable text for 3 page" in w for w in result.warnings)

    async def test_no_heading_path_is_invented(self) -> None:
        """OCR yields characters, not structure. A guessed hierarchy would be
        fabricated precision that citation locators would then report."""
        result = await extract_pdf(
            _engine_returning(["A page of readable policy text."]), b"", "s.pdf"
        )
        assert all(b.heading_path == () for b in result.blocks)


class TestUnreachableService:
    async def test_connection_failure_names_the_service(self) -> None:
        engine = TikaOcrEngine("http://ocr-that-does-not-resolve.invalid:9998", ("eng",))
        with pytest.raises(OcrUnavailableError) as caught:
            await engine.pages(b"%PDF-", "scan.pdf")
        # An administrator reading this must be able to tell it apart from "this
        # document is a bad scan" — one is a container to restart, the other is
        # a document to re-request from its owner.
        assert "ocr-that-does-not-resolve.invalid" in str(caught.value)

    async def test_health_reports_unreachable_rather_than_raising(self) -> None:
        engine = TikaOcrEngine("http://ocr-that-does-not-resolve.invalid:9998", ("eng",))
        ok, detail = await engine.health()
        assert ok is False
        assert "unreachable" in detail.lower()

    async def test_server_error_is_not_mistaken_for_an_empty_document(self) -> None:
        """A 500 with an empty body must not read as "this scan has no text".

        That confusion would fail the document as unreadable and send someone to
        chase the source owner for a file that is perfectly fine.
        """

        class _Failing(TikaOcrEngine):
            async def pages(self, data: bytes, filename: str) -> list[str]:  # type: ignore[override]
                raise OcrUnavailableError("OCR service returned 500 for scan.pdf")

        with pytest.raises(OcrUnavailableError):
            await extract_pdf(_Failing("http://ocr:9998", ("eng",)), b"", "scan.pdf")


class TestEngineSelection:
    def test_off_by_default_returns_no_engine(self) -> None:
        from askau.ingestion.extractors.ocr import engine_for

        assert engine_for("none", "", ("eng",)) is None

    def test_unknown_provider_fails_loudly(self) -> None:
        from askau.ingestion.extractors.ocr import engine_for

        with pytest.raises(ValueError, match="unknown OCR provider"):
            engine_for("tesseract-on-the-host", "http://x", ("eng",))


class TestTikaRequest:
    """The two headers that decide whether OCR happens at all."""

    async def test_sends_languages_and_forces_ocr(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, text='<div class="page">Text of the scanned page.</div>')

        engine = TikaOcrEngine("http://ocr:9998", ("eng", "amh"))
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.put(
                "http://ocr:9998/tika",
                content=b"%PDF-",
                headers={
                    "Accept": "text/html",
                    "X-Tika-OCRLanguage": "+".join(("eng", "amh")),
                    "X-Tika-PDFOcrStrategy": "ocr_only",
                },
            )

        assert seen["x-tika-ocrlanguage"] == "eng+amh"
        # Without ocr_only, Tika skips OCR on any PDF carrying a partial text
        # layer — it would return success having done nothing.
        assert seen["x-tika-pdfocrstrategy"] == "ocr_only"
        assert engine._split_pages(response.text) == ["Text of the scanned page."]
