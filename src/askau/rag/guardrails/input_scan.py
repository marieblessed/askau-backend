"""Input guardrail — the user's question, before anything else runs."""

from __future__ import annotations

import re
from dataclasses import dataclass

from askau.rag.guardrails.context_shield import scan as scan_injection

MAX_QUESTION_CHARS = 2000

_SECRET_LIKE = re.compile(r"(?i)\b(?:password|api[_-]?key|secret|token)\s*[=:]\s*\S{6,}")


@dataclass(frozen=True, slots=True)
class InputScanResult:
    ok: bool
    reason: str = ""
    detections: tuple[str, ...] = ()


def scan(question: str) -> InputScanResult:
    text = question.strip()
    if not text:
        return InputScanResult(False, "The question is empty.")
    if len(text) > MAX_QUESTION_CHARS:
        return InputScanResult(False, f"The question exceeds {MAX_QUESTION_CHARS} characters.")
    if _SECRET_LIKE.search(text):
        # Refuse rather than scrub: the credential has already been typed, and
        # accepting it would write it into conversation history.
        return InputScanResult(
            False,
            "The question appears to contain a credential. It has not been "
            "processed or stored. Please remove it and ask again.",
        )

    detections, _ = scan_injection(text)
    # A user attempting injection is recorded but not blocked: the boundary that
    # matters is the authorization predicate, which their question cannot reach,
    # and blocking would mostly catch people asking legitimate questions about
    # security policy.
    return InputScanResult(True, detections=detections)
