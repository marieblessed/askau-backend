"""Redaction for logs and audit detail.

FR-052: "sensitive content should not be unnecessarily replicated into audit
logs". The rule applied here is stricter than the requirement — audit rows record
*that* a query happened and *which documents* were retrieved, never the question
or the answer text. An investigator can reconstruct what was accessed without
reading what was asked.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("[TOKEN]", re.compile(r"\b(?:ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)\b")),
    ("[BEARER]", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")),
    ("[KEY]", re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\s*[=:]\s*\S+")),
    ("[CARD]", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)

#: Keys whose values are never written to a log or audit row, at any depth.
_FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "question",
        "answer",
        "text",
        "prompt",
        "completion",
        "password",
        "secret",
        "token",
        "authorization",
        "api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "quote",
    }
)


def scrub_text(value: str) -> str:
    for replacement, pattern in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a structure destined for a log or audit row."""
    if _depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _FORBIDDEN_KEYS else scrub(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(v, _depth + 1) for v in value]
    return value


def hash_ip(ip: str | None) -> bytes | None:
    """Hash rather than store. NFR-005 data minimization: enough to correlate
    events from one source, not enough to identify a person's location."""
    if not ip:
        return None
    return hashlib.blake2b(ip.encode(), digest_size=16).digest()
