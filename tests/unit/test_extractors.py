"""Document extraction (FR-012, FR-014).

These build real files at test time rather than committing binaries. A fixture
you can read the source of is one you can reason about, and a committed PDF is
opaque the moment the person who made it leaves.

The assertion that matters throughout is **position survives extraction**. Text
coming out is easy; page numbers and heading paths coming out is what makes
"§4.3, p.12" possible, and losing them is invisible everywhere downstream.
"""

from __future__ import annotations

import io

import pytest

from askau.ingestion.extractors import (
    SUPPORTED_SUFFIXES,
    UnsupportedFormatError,
    extract,
    extractor_for,
)

fitz = pytest.importorskip("fitz")
docx_mod = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")
pptx_mod = pytest.importorskip("pptx")
pytest.importorskip("bs4")


# ── fixture builders ────────────────────────────────────────────────────────


def make_pdf(pages: list[list[tuple[str, float, bool]]]) -> bytes:
    """Build a PDF from (text, font size, bold) per page."""
    doc = fitz.open()
    for spans in pages:
        page = doc.new_page()
        y = 72.0
        for text, size, bold in spans:
            page.insert_text(
                (72, y),
                text,
                fontsize=size,
                fontname="hebo" if bold else "helv",
            )
            y += size * 2.2
    data: bytes = doc.tobytes()
    doc.close()
    return data


def make_docx(items: list[tuple[str, str | None]]) -> bytes:
    """Build a DOCX from (text, style) pairs."""
    document = docx_mod.Document()
    for text, style in items:
        document.add_paragraph(text, style=style) if style else document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ── PDF ─────────────────────────────────────────────────────────────────────


class TestPdf:
    def test_page_numbers_survive(self) -> None:
        """The whole reason PyMuPDF was chosen over a text-dump library."""
        data = make_pdf(
            [
                [("Content on the first page.", 11, False)],
                [("Content on the second page.", 11, False)],
                [("Content on the third page.", 11, False)],
            ]
        )
        result = extract(data, "policy.pdf")
        pages = {b.page for b in result.blocks}
        assert pages == {1, 2, 3}
        assert result.page_count == 3

        third = next(b for b in result.blocks if "third" in b.text)
        assert third.page == 3

    def test_larger_text_becomes_a_heading(self) -> None:
        data = make_pdf(
            [
                [
                    ("Annual Leave Policy", 20, True),
                    ("Staff accrue thirty working days per calendar year.", 11, False),
                ]
            ]
        )
        result = extract(data, "leave.pdf")
        heading = next(b for b in result.blocks if b.is_heading)
        assert "Annual Leave" in heading.text
        body = next(b for b in result.blocks if not b.is_heading)
        assert body.heading_path == heading.heading_path

    def test_a_long_sentence_in_large_type_is_not_a_heading(self) -> None:
        """A pull quote is not a heading. Inventing one splits a section that
        should have stayed whole and misattributes every chunk after it."""
        long_text = (
            "This paragraph is set in a larger face for emphasis but it is "
            "plainly a sentence and runs on well past any reasonable heading "
            "length, ending in a full stop."
        )
        data = make_pdf([[("Real Heading", 18, True), (long_text, 14, False)]])
        result = extract(data, "emphasis.pdf")
        assert not any(b.is_heading and "paragraph is set" in b.text for b in result.blocks)

    def test_numbered_sections_are_captured(self) -> None:
        data = make_pdf(
            [
                [
                    ("4.3 Reimbursement", 15, True),
                    ("Claims are submitted within thirty days.", 11, False),
                ]
            ]
        )
        result = extract(data, "travel.pdf")
        assert any(b.section_ref == "4.3" for b in result.blocks)

    def test_a_scanned_page_is_reported_not_silently_empty(self) -> None:
        """The High/High programme risk. A scan indexed as an empty document
        produces "not enough evidence" for a policy that is demonstrably there."""
        doc = fitz.open()
        doc.new_page()  # no text at all
        data = doc.tobytes()
        doc.close()

        result = extract(data, "scan.pdf")
        assert result.warnings
        assert "scanned" in result.warnings[0].lower()
        assert "OCR" in result.warnings[0]


# ── DOCX ────────────────────────────────────────────────────────────────────


class TestDocx:
    def test_heading_styles_give_a_real_hierarchy(self) -> None:
        """Word states its structure, so the path is read rather than guessed."""
        data = make_docx(
            [
                ("Staff Regulations", "Heading 1"),
                ("Leave", "Heading 2"),
                ("Staff accrue thirty days per year.", None),
                ("Travel", "Heading 2"),
                ("Economy class is reimbursed.", None),
            ]
        )
        result = extract(data, "regs.docx")
        leave = next(b for b in result.blocks if "thirty days" in b.text)
        travel = next(b for b in result.blocks if "Economy class" in b.text)

        assert leave.heading_path == ("Staff Regulations", "Leave")
        assert travel.heading_path == ("Staff Regulations", "Travel")
        # The sibling heading replaced its peer rather than nesting under it.
        assert "Leave" not in travel.heading_path

    def test_tables_keep_their_column_meaning(self) -> None:
        """A table flattened into prose reads as nonsense and retrieves badly."""
        document = docx_mod.Document()
        document.add_paragraph("Per Diem", style="Heading 1")
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Region"
        table.rows[0].cells[1].text = "Rate"
        table.rows[1].cells[0].text = "Continental"
        table.rows[1].cells[1].text = "USD 180"
        buffer = io.BytesIO()
        document.save(buffer)

        result = extract(buffer.getvalue(), "rates.docx")
        row = next(b for b in result.blocks if "Continental" in b.text)
        assert "Region: Continental" in row.text
        assert "Rate: USD 180" in row.text


# ── XLSX ────────────────────────────────────────────────────────────────────


class TestXlsx:
    def test_each_row_carries_its_headers(self) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Rates"
        sheet.append(["Region", "Rate USD"])
        sheet.append(["Continental", 180])
        sheet.append(["Intercontinental", 250])
        buffer = io.BytesIO()
        workbook.save(buffer)

        result = extract(buffer.getvalue(), "rates.xlsx")
        assert len(result.blocks) == 2
        first = result.blocks[0]
        assert "Region: Continental" in first.text
        assert "Rate USD: 180" in first.text
        assert first.heading_path == ("Rates",)


# ── PPTX ────────────────────────────────────────────────────────────────────


class TestPptx:
    def test_slides_become_sections_with_their_number(self) -> None:
        presentation = pptx_mod.Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "Travel Policy Overview"
        slide.placeholders[1].text = "Economy class for continental travel."
        buffer = io.BytesIO()
        presentation.save(buffer)

        result = extract(buffer.getvalue(), "deck.pptx")
        assert all(b.page == 1 for b in result.blocks)
        assert any(b.is_heading and "Travel Policy" in b.text for b in result.blocks)
        body = next(b for b in result.blocks if "Economy class" in b.text)
        assert body.heading_path == ("Travel Policy Overview",)


# ── HTML ────────────────────────────────────────────────────────────────────


class TestHtml:
    def test_semantic_headings_are_used_directly(self) -> None:
        html = b"""
        <html><body><main>
          <h1>Staff Handbook</h1>
          <h2>Annual Leave</h2>
          <p>Staff accrue thirty working days.</p>
        </main></body></html>
        """
        result = extract(html, "handbook.html")
        body = next(b for b in result.blocks if "thirty working days" in b.text)
        assert body.heading_path == ("Staff Handbook", "Annual Leave")

    def test_navigation_chrome_is_stripped(self) -> None:
        """Left in, a site's menu appears in every chunk and every page then
        matches every query on its own navigation."""
        html = b"""
        <html><body>
          <nav><a href="/">Home</a><a href="/policies">Policies</a></nav>
          <main><h1>Leave</h1><p>Thirty days.</p></main>
          <footer>Copyright African Union</footer>
        </body></html>
        """
        result = extract(html, "page.html")
        text = " ".join(b.text for b in result.blocks)
        assert "Thirty days" in text
        assert "Home" not in text
        assert "Copyright" not in text


# ── dispatch ────────────────────────────────────────────────────────────────


class TestDispatch:
    @pytest.mark.parametrize(
        "filename", ["a.pdf", "a.docx", "a.xlsx", "a.pptx", "a.html", "a.txt", "a.md"]
    )
    def test_every_fr012_format_resolves(self, filename: str) -> None:
        assert extractor_for(filename) is not None

    @pytest.mark.parametrize("filename", ["archive.zip", "image.png", "noextension"])
    def test_an_unsupported_format_is_a_distinct_error(self, filename: str) -> None:
        """Separate from a parse failure: "we don't handle .zip" and "this PDF
        is corrupt" need different answers to the source owner."""
        with pytest.raises(UnsupportedFormatError):
            extract(b"whatever", filename)

    def test_the_supported_set_matches_fr012(self) -> None:
        for suffix in (".pdf", ".docx", ".xlsx", ".pptx", ".html", ".txt"):
            assert suffix in SUPPORTED_SUFFIXES


class TestText:
    def test_markdown_headings_nest(self) -> None:
        md = b"# Handbook\n\n## Leave\n\nStaff accrue thirty days.\n"
        result = extract(md, "handbook.md")
        body = next(b for b in result.blocks if "thirty days" in b.text)
        assert body.heading_path == ("Handbook", "Leave")

    def test_plain_text_does_not_invent_structure(self) -> None:
        """Guessing headings from blank lines produces paths that look
        authoritative and are not."""
        result = extract(b"First paragraph.\n\nSecond paragraph.\n", "notes.txt")
        assert len(result.blocks) == 2
        assert all(b.heading_path == () for b in result.blocks)

    def test_undecodable_bytes_lose_a_character_not_the_document(self) -> None:
        result = extract(b"Valid text \xff\xfe more text", "mixed.txt")
        assert result.blocks
        assert "Valid text" in result.blocks[0].text
