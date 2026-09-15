"""Audit event taxonomy (FR-051, FR-052).

A closed vocabulary, so events aggregate in the security console rather than
becoming free-text nobody can query.
"""

from __future__ import annotations

from enum import StrEnum


class EventCategory(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    QUERY = "query"
    RETRIEVAL = "retrieval"
    DOCUMENT_ACCESS = "document_access"
    ADMINISTRATION = "administration"
    CONFIGURATION = "configuration"
    SECURITY = "security"
    AI_SAFETY = "ai_safety"
    INGESTION = "ingestion"


class EventType(StrEnum):
    LOGIN = "auth.login"
    LOGOUT = "auth.logout"
    LOGIN_FAILED = "auth.login_failed"
    QUERY_SUBMITTED = "query.submitted"
    QUERY_REFUSED = "query.refused"
    RETRIEVAL_PERFORMED = "retrieval.performed"
    DOCUMENT_OPENED = "document.opened"
    ACCESS_DENIED = "security.access_denied"
    INJECTION_DETECTED = "ai.injection_detected"
    OUTPUT_BLOCKED = "ai.output_blocked"
    CITATION_FABRICATED = "ai.citation_fabricated"
    SOURCE_CREATED = "admin.source_created"
    SOURCE_APPROVED = "admin.source_approved"
    REINDEX_TRIGGERED = "admin.reindex_triggered"
    USER_PROVISIONED = "admin.user_provisioned"
    CONFIG_CHANGED = "admin.config_changed"
    CONVERSATION_DELETED = "conversation.deleted"
    FEEDBACK_SUBMITTED = "feedback.submitted"


#: Which category each type belongs to. A mapping rather than a naming
#: convention, so a renamed event cannot quietly change category and disappear
#: from a security filter.
CATEGORY_OF: dict[EventType, EventCategory] = {
    EventType.LOGIN: EventCategory.AUTHENTICATION,
    EventType.LOGOUT: EventCategory.AUTHENTICATION,
    EventType.LOGIN_FAILED: EventCategory.AUTHENTICATION,
    EventType.QUERY_SUBMITTED: EventCategory.QUERY,
    EventType.QUERY_REFUSED: EventCategory.QUERY,
    EventType.RETRIEVAL_PERFORMED: EventCategory.RETRIEVAL,
    EventType.DOCUMENT_OPENED: EventCategory.DOCUMENT_ACCESS,
    EventType.ACCESS_DENIED: EventCategory.SECURITY,
    EventType.INJECTION_DETECTED: EventCategory.AI_SAFETY,
    EventType.OUTPUT_BLOCKED: EventCategory.AI_SAFETY,
    EventType.CITATION_FABRICATED: EventCategory.AI_SAFETY,
    EventType.SOURCE_CREATED: EventCategory.ADMINISTRATION,
    EventType.SOURCE_APPROVED: EventCategory.ADMINISTRATION,
    EventType.REINDEX_TRIGGERED: EventCategory.ADMINISTRATION,
    EventType.USER_PROVISIONED: EventCategory.ADMINISTRATION,
    EventType.CONFIG_CHANGED: EventCategory.CONFIGURATION,
    EventType.CONVERSATION_DELETED: EventCategory.QUERY,
    EventType.FEEDBACK_SUBMITTED: EventCategory.QUERY,
}
