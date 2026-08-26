"""XLSX extraction (FR-012).

A spreadsheet is not prose and chunking it as prose destroys it. Each row
becomes its own block with its column headers attached, so a retrieved row still
carries the meaning of its numbers — "Region: Continental · Rate: USD 180"
rather than a bare "Continental 180" that could be anything.

Sheet names become the heading path, which is usually how a workbook is
organised anyway.
"""

from __future__ import annotations

from askau.domain.knowledge import ExtractedBlock, ExtractionResult

#: Guard against a sheet with a runaway used-range — a common artefact of
#: spreadsheets edited over years, where the used range is far larger than the
#: data and the rest is empty cells.
_MAX_ROWS_PER_SHEET = 5_000


class XlsxExtractor:
    def extract(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            import io

            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "openpyxl is required for XLSX extraction: pip install '.[ingestion]'"
            ) from exc

        # read_only + data_only: formulas are read as their cached values, which
        # is what a reader of the policy cares about, and read_only keeps a
        # large workbook from being loaded whole.
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        blocks: list[ExtractedBlock] = []
        warnings: list[str] = []
        offset = 0

        for sheet in workbook.worksheets:
            header: list[str] = []
            emitted = 0

            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                values = ["" if v is None else str(v).strip() for v in row]
                if not any(values):
                    continue
                if not header:
                    header = values
                    continue
                if emitted >= _MAX_ROWS_PER_SHEET:
                    warnings.append(
                        f"Sheet {sheet.title!r} truncated at {_MAX_ROWS_PER_SHEET} rows"
                    )
                    break

                pairs = [f"{h}: {v}" for h, v in zip(header, values, strict=False) if v and h] or [
                    v for v in values if v
                ]
                text = " · ".join(pairs)
                blocks.append(
                    ExtractedBlock(
                        text=text,
                        page=None,
                        heading_path=(sheet.title,),
                        section_ref=f"row {index + 1}",
                        char_start=offset,
                        char_end=offset + len(text),
                    )
                )
                offset += len(text) + 1
                emitted += 1

        workbook.close()
        return ExtractionResult(blocks=tuple(blocks), page_count=None, warnings=tuple(warnings))
