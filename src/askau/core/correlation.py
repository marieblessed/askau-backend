"""Correlation IDs.

FR-052 requires a correlation ID on every audit event. It is held in a
``ContextVar`` so any layer can reach it without threading it through every
signature — and so an audit write deep in the pipeline cannot accidentally omit it.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

HEADER = "X-Correlation-Id"


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def set_correlation_id(value: str | None) -> str:
    """Accept a caller-supplied ID, or mint one.

    A caller-supplied value is validated as a UUID before being accepted: this
    string reaches log lines and audit rows, and an unvalidated header is a log
    injection vector.
    """
    if value:
        try:
            value = str(uuid.UUID(value))
        except ValueError:
            value = new_correlation_id()
    else:
        value = new_correlation_id()
    _correlation_id.set(value)
    return value


def get_correlation_id() -> str:
    current = _correlation_id.get()
    if not current:
        current = new_correlation_id()
        _correlation_id.set(current)
    return current
