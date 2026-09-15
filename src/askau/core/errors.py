"""Error taxonomy — RFC 9457 problem+json.

Two decisions here are security decisions rather than ergonomic ones:

* An unauthorized document returns **404, not 403**. A 403 confirms that a
  restricted document exists, and for confidential AUC material existence is
  itself sensitive (ADR-0009). The audit log records the real outcome as
  ``denied`` even though the response says ``not_found``.
* Retrieval or model failure returns **502, never a fallback answer**. Answering
  from model knowledge when retrieval is down would violate FR-033 and BR-007
  (ADR-0010).

Each body is a *superset* of problem+json. RFC 9457 permits extension members,
so alongside `type`/`title`/`status`/`detail` every response also carries the
four fields the web client parses: `code`, `message`, `correlationId` and, for
validation failures, `fieldErrors`. Two consumers, one body, no second error
format to keep in step.

The `code` values are not ours to choose — they are the union in
`askau-frontend/types/api.ts`, and its `lib/api/errors.ts` maps them to error
classes under unit test. A code outside that set arrives on their side as an
opaque "unexpected error".
"""

from __future__ import annotations

from typing import Any


class AskAUError(Exception):
    """Base for every error that maps to an HTTP response."""

    status: int = 500
    type_: str = "internal_error"
    title: str = "Internal error"
    #: One of the client's `ApiErrorCode` values. Anything else reaches their UI
    #: as an opaque failure, so the default is the honest catch-all rather than
    #: something more specific-sounding.
    code: str = "SERVER_ERROR"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, correlation_id: str, instance: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            # ── RFC 9457 ────────────────────────────────────────────────────
            "type": f"https://askau.au.int/errors/{self.type_}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            # ── extension members the web client reads ──────────────────────
            "code": self.code,
            # Same text as `detail`. Duplicated rather than chosen between: the
            # RFC names it `detail` and their client reads `message`, and one
            # extra key costs less than a second error format.
            "message": self.detail,
            "correlationId": correlation_id,
        }
        if instance:
            body["instance"] = instance
        body.update(self.extra)
        return body


class InvalidRequestError(AskAUError):
    """A malformed or unacceptable request.

    **422, not 400.** Their `lib/api/errors.ts` maps 422 to `ValidationError`
    and it is the only status whose `message` and `fieldErrors` it reads;
    everything else — 400 included — becomes `UnknownApiError` with the message
    suppressed in production. So a 400 here would reach a user as "An unexpected
    error occurred" no matter how clearly we explained the problem.
    """

    status, type_, title = 422, "invalid_request", "Invalid request"
    code = "VALIDATION_ERROR"

    def __init__(
        self, detail: str | None = None, field_errors: dict[str, list[str]] | None = None
    ) -> None:
        # `fieldErrors` keyed by field name, values a list of messages — the
        # shape their form handling expects.
        super().__init__(detail, **({"fieldErrors": field_errors} if field_errors else {}))


class UnauthenticatedError(AskAUError):
    status, type_, title = 401, "unauthenticated", "Authentication required"
    code = "UNAUTHORIZED"


class InsufficientRoleError(AskAUError):
    """The caller is authenticated but lacks an *administrative role*.

    Distinct from document authorization: role membership is not sensitive in the
    way document existence is, so 403 is correct here.
    """

    status, type_, title = 403, "insufficient_role", "Insufficient role"
    code = "FORBIDDEN"


class NotFoundError(AskAUError):
    """Absent, or present but not authorized — indistinguishable by design."""

    status, type_, title = 404, "not_found", "Not found"
    code = "NOT_FOUND"


class ConflictError(AskAUError):
    """The request is well-formed but the resource is in the wrong state.

    `VALIDATION_ERROR` is not a copy-paste slip. Their `ApiErrorCode` union has
    seven members and no `CONFLICT`, and a code outside the union falls through
    their mapping to `UnknownApiError`, which suppresses the message in
    production. `VALIDATION_ERROR` is the closest member whose branch shows the
    text — and the text is the whole value of a 409, which always explains what
    state blocked the request.
    """

    status, type_, title = 409, "conflict", "Conflict"
    code = "VALIDATION_ERROR"


class PayloadTooLargeError(AskAUError):
    status, type_, title = 413, "payload_too_large", "Payload too large"
    code = "VALIDATION_ERROR"


class UnprocessableDocumentError(AskAUError):
    """FR-013 ingestion validation failure — carries an actionable remedy."""

    status, type_, title = 422, "unprocessable_document", "Document cannot be processed"
    code = "VALIDATION_ERROR"

    def __init__(self, detail: str, code: str, remedy: str) -> None:
        super().__init__(detail, error_code=code, remedy=remedy)


class RateLimitedError(AskAUError):
    status, type_, title = 429, "rate_limited", "Rate limit exceeded"
    code = "RATE_LIMITED"

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many requests. Retry in {retry_after}s.", retry_after=retry_after)
        self.retry_after = retry_after


class UpstreamUnavailableError(AskAUError):
    """A dependency is down. Fails safe: an error, never an ungrounded answer."""

    status, type_, title = 502, "upstream_unavailable", "A required service is unavailable"
    code = "SERVER_ERROR"


class NotReadyError(AskAUError):
    status, type_, title = 503, "not_ready", "Service not ready"
    code = "SERVER_ERROR"


class ConfigurationError(AskAUError):
    """Raised at startup, never in a request. Infrastructure is provisioned
    elsewhere (ADR-0014), so the application must verify what it depends on."""

    status, type_, title = 500, "configuration_error", "Configuration error"
    code = "SERVER_ERROR"
