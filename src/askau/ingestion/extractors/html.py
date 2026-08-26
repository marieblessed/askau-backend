"""HTML extraction (FR-012).

The only format that states its own structure: `h1` to `h6` are real headings, so
the path is read rather than inferred.

Script, style and navigation chrome are stripped first. Left in, a site's menu
appears in every chunk and dominates keyword retrieval — every page then matches
every query on its own navigation.
"""

from __future__ import annotations

from askau.domain.knowledge import ExtractedBlock, ExtractionResult

_STRIP = ("script", "style", "nav", "header", "footer", "aside", "noscript", "form")
_BLOCK_TAGS = ("p", "li", "td", "th", "blockquote", "pre", "dd", "dt")


class HtmlExtractor:
    def extract(self, data: bytes, filename: str) -> ExtractionResult:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "beautifulsoup4 is required for HTML extraction: pip install '.[ingestion]'"
            ) from exc

        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(list(_STRIP)):
            tag.decompose()

        root = soup.find("main") or soup.find("article") or soup.body or soup
        blocks: list[ExtractedBlock] = []
        heading_path: tuple[str, ...] = ()
        offset = 0

        for element in root.find_all([*("h1", "h2", "h3", "h4", "h5", "h6"), *_BLOCK_TAGS]):
            text = " ".join(element.get_text(" ", strip=True).split())
            if not text:
                continue

            name = element.name.lower()
            if name.startswith("h") and len(name) == 2 and name[1].isdigit():
                level = min(int(name[1]), 4)
                heading_path = (*heading_path[: level - 1], text)
                blocks.append(
                    ExtractedBlock(
                        text=text,
                        page=None,
                        heading_path=heading_path,
                        is_heading=True,
                        char_start=offset,
                        char_end=offset + len(text),
                    )
                )
            else:
                blocks.append(
                    ExtractedBlock(
                        text=text,
                        page=None,
                        heading_path=heading_path,
                        char_start=offset,
                        char_end=offset + len(text),
                    )
                )
            offset += len(text) + 1

        return ExtractionResult(blocks=tuple(blocks), page_count=None)
