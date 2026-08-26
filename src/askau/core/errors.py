"""Error taxonomy — RFC 9457 problem+json.

Two decisions here are security decisions rather than ergonomic ones:

* An unauthorized document returns **404, not 403**. A 403 confirms that a
  restricted document exists, and for confidential AUC material existence is
  itself sensitive (ADR-0009). The audit log records the real outcome as
  ``denied`` even though the response says ``not_found``.
* Retrieval or model failure returns **502, never a fallback answer**. Answering
  from model knowledge when retrieval is down would violate FR-033 and BR-007
  (ADR-0010).
"""

from __future__ import annotations

from typing import Any


class AskAUError(Exception):
    """Base for every error that maps to an HTTP response."""

    status: int = 500
    type_: str = "internal_error"
    title: str = "Internal error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, correlation_id: str, instance: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"https://askau.au.int/errors/{self.type_}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "correlation_id": correlation_id,
        }
        if instance:
            body["instance"] = instance
        body.update(self.extra)
        return body


class InvalidRequestError(AskAUError):
    status, type_, title = 400, "invalid_request", "Invalid request"


class UnauthenticatedError(AskAUError):
    status, type_, title = 401, "unauthenticated", "Authentication required"


class InsufficientRoleError(AskAUError):
    """The caller is authenticated but lacks an *administrative role*.

    Distinct from document authorization: role membership is not sensitive in the
    way document existence is, so 403 is correct here.
    """

    status, type_, title = 403, "insufficient_role", "Insufficient role"


class NotFoundError(AskAUError):
    """Absent, or present but not authorized — indistinguishable by design."""

    status, type_, title = 404, "not_found", "Not found"


class ConflictError(AskAUError):
    status, type_, title = 409, "conflict", "Conflict"


class PayloadTooLargeError(AskAUError):
    status, type_, title = 413, "payload_too_large", "Payload too large"


class UnprocessableDocumentError(AskAUError):
    """FR-013 ingestion validation failure — carries an actionable remedy."""

    status, type_, title = 422, "unprocessable_document", "Document cannot be processed"

    def __init__(self, detail: str, code: str, remedy: str) -> None:
        super().__init__(detail, error_code=code, remedy=remedy)


class RateLimitedError(AskAUError):
    status, type_, title = 429, "rate_limited", "Rate limit exceeded"

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many requests. Retry in {retry_after}s.", retry_after=retry_after)
        self.retry_after = retry_after


class UpstreamUnavailableError(AskAUError):
    """A dependency is down. Fails safe: an error, never an ungrounded answer."""

    status, type_, title = 502, "upstream_unavailable", "A required service is unavailable"


class NotReadyError(AskAUError):
    status, type_, title = 503, "not_ready", "Service not ready"


class ConfigurationError(AskAUError):
    """Raised at startup, never in a request. Infrastructure is provisioned
    elsewhere (ADR-0014), so the application must verify what it depends on."""

    status, type_, title = 500, "configuration_error", "Configuration error"
