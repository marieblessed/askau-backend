"""The pre-index validation gate (FR-013).

"Invalid documents shall not be made available through AskAU." The gate runs
before anything is indexed, and every rejection carries a machine-readable
`code` and a human-readable `remedy`.

The remedy is not decoration. A knowledge administrator reading "validation
failed" has to escalate to an engineer; one reading "this PDF has no text layer,
enable OCR for the source or supply a text-based export" can act. FR-050 asks
for error information; information an administrator cannot act on is not
information.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from askau.domain.knowledge import ExtractionResult, ValidationFailure
from askau.ingestion.extractors import SUPPORTED_SUFFIXES

#: Above this a single document dominates a run's memory and time. AUC policy
#: documents are far smaller; anything larger is usually a scanned archive that
#: should be split by its owner.
MAX_BYTES = 50 * 1024 * 1024

#: A prose document shorter than this cannot support an answer — usually a cover
#: sheet, a scan, or an export that failed silently.
MIN_EXTRACTED_CHARS = 200

#: …but length is the wrong measure for structured documents. A per-diem rate
#: schedule is two rows and 120 characters, and it is exactly the kind of short,
#: high-value document AskAU exists to answer from.
#:
#: So the rejection needs both conditions: short *and* essentially one thing.
#: One short block is a cover sheet; two structured rows are a rate table.
MIN_STRUCTURED_BLOCKS = 2


@dataclass(frozen=True, slots=True)
class ValidatedDocument:
    content_hash: bytes
    byte_size: int
    suffix: str


def validate_file(filename: str, data: bytes) -> ValidationFailure | ValidatedDocument:
    """Check a document before extraction. Cheap checks first."""
    suffix = Path(filename).suffix.lower()

    if not data:
        return ValidationFailure(
            code="empty_file",
            message=f"{filename} is empty",
            remedy="Check the export or the repository sync; re-upload the document.",
        )

    if len(data) > MAX_BYTES:
        return ValidationFailure(
            code="too_large",
            message=(
                f"{filename} is {len(data) // 1_048_576} MB, above the "
                f"{MAX_BYTES // 1_048_576} MB limit"
            ),
            remedy="Split the document, or ask the owner for a text-based export.",
        )

    if suffix not in SUPPORTED_SUFFIXES:
        return ValidationFailure(
            code="unsupported_format",
            message=f"{suffix or 'no extension'} is not a supported format",
            remedy=(
                "Supported formats are PDF, DOCX, XLSX, PPTX, HTML, TXT and Markdown "
                "(FR-012). Convert the document or confirm with the source owner "
                "that it should be excluded."
            ),
        )

    return ValidatedDocument(
        content_hash=hashlib.sha256(data).digest(),
        byte_size=len(data),
        suffix=suffix,
    )


def validate_extraction(filename: str, result: ExtractionResult) -> ValidationFailure | None:
    """Check what extraction produced. Runs after, because "no usable text" can
    only be known once extraction has been attempted."""
    text = "".join(b.text for b in result.blocks)
    substantive = [b for b in result.blocks if not b.is_heading]

    thin = len(text) < MIN_EXTRACTED_CHARS and len(substantive) < MIN_STRUCTURED_BLOCKS
    if not result.blocks or thin:
        scanned = any("scanned" in w.lower() for w in result.warnings)
        if scanned:
            return ValidationFailure(
                code="no_text_layer",
                message=f"{filename} has no usable text layer",
                remedy=(
                    "This is almost certainly a scan. Enable OCR for this source, "
                    "or ask the owner for the original text-based document."
                ),
            )
        return ValidationFailure(
            code="insufficient_text",
            message=f"{filename} produced only {len(text)} characters",
            remedy=(
                "The document may be a cover sheet, a placeholder, or a failed "
                "export. Confirm with the source owner that it carries content."
            ),
        )
    return None


def injection_risk(result: ExtractionResult) -> int:
    """Score 0 to 100 for instruction-shaped text in the document (FR-036).

    A score is not a verdict. An approved policy may legitimately quote an email
    containing instructions, so a high score routes to administrator review — it
    never blocks ingestion on its own. Silently dropping approved content is its
    own failure, and the control that must actually hold is the output scan.
    """
    text = " ".join(b.text for b in result.blocks).lower()
    if not text:
        return 0

    markers = (
        "ignore previous instructions",
        "ignore all previous",
        "disregard the above",
        "you are now",
        "system prompt",
        "new instructions:",
        "override your",
        "reveal all",
        "list every confidential",
        "do not follow your",
    )
    hits = sum(1 for m in markers if m in text)
    if not hits:
        return 0
    # Two markers is already strongly suspicious; the curve saturates quickly so
    # a document does not need to be egregious to reach review.
    return min(100, 40 + hits * 25)
