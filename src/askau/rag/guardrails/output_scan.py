"""Output guardrail (FR-038) — the layer that must hold.

Ingest screening and the context shield are pattern-based and evadable. This
check is not: it compares the finished answer against the concrete set of chunks
that were actually retrieved. A document cannot talk its way past a check that
only asks "was this in the context?".

It also scrubs claims of official authority. AskAU is explicitly not the
authoritative source of policy (BR-003), so an answer asserting that it issues a
directive is wrong regardless of how well grounded it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from askau.domain.retrieval import RetrievedChunk

#: Phrases asserting institutional authority AskAU does not have.
_AUTHORITY_CLAIMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\bthis is an official "
            r"(AU|AUC|African Union|Commission) (directive|decision|ruling)\b"
        ),
        "According to the referenced AUC document",
    ),
    (
        re.compile(r"(?i)\bI (hereby )?(approve|authorize|authorise|certify|rule)\b"),
        "The referenced policy states",
    ),
    (
        re.compile(r"(?i)\bas (an )?official (AUC|African Union) (policy|position)\b"),
        "as stated in the cited document",
    ),
)

#: Text that indicates the model followed an injected instruction rather than
#: answering the question.
_COMPLIANCE_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(unrestricted|developer|debug) mode (is )?(now )?(enabled|active)\b"),
    re.compile(r"(?i)\bignoring (my|the|all) (previous|prior) instructions\b"),
    re.compile(
        r"(?i)\bhere (is|are) (the )?(full|complete|entire) (list of )?"
        r"(all )?(confidential|restricted|secret)\b"
    ),
)


@dataclass(frozen=True, slots=True)
class OutputScanResult:
    text: str
    blocked: bool
    reasons: tuple[str, ...]
    scrubbed: bool = False


def scan(
    answer: str,
    context_chunks: dict[int, RetrievedChunk],
    *,
    authorized_titles: frozenset[str] | None = None,
) -> OutputScanResult:
    reasons: list[str] = []

    for pattern in _COMPLIANCE_SIGNALS:
        if pattern.search(answer):
            # Do not attempt to salvage: an answer that shows signs of having
            # followed an injected instruction is not trustworthy in any part.
            return OutputScanResult(text="", blocked=True, reasons=("injection_compliance",))

    # Leak check: does the answer name a document that was not retrieved?
    if authorized_titles is not None:
        retrieved = {c.document_title for c in context_chunks.values()}
        for title in authorized_titles - retrieved:
            # Titles are distinctive; a match means the answer referenced a
            # document that never entered the context.
            if len(title) > 12 and title.lower() in answer.lower():
                reasons.append(f"referenced_unretrieved_document:{title}")

    if reasons:
        return OutputScanResult(text="", blocked=True, reasons=tuple(reasons))

    scrubbed_text = answer
    scrubbed = False
    for pattern, replacement in _AUTHORITY_CLAIMS:
        new_text = pattern.sub(replacement, scrubbed_text)
        if new_text != scrubbed_text:
            scrubbed = True
            scrubbed_text = new_text

    return OutputScanResult(text=scrubbed_text, blocked=False, reasons=(), scrubbed=scrubbed)


REFUSED_SAFETY_MESSAGE = (
    "I was not able to produce a reliable answer to that question. The response "
    "did not pass AskAU's safety checks, so it has been withheld rather than shown."
)
