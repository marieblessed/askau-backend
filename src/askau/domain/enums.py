"""Closed vocabularies shared across the platform.

These mirror the PostgreSQL enums in docs/architecture/03-database-schema.md §0.
Keeping them as ``str`` enums means they serialize to the wire and bind to the
database without translation layers.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Classification(StrEnum):
    """SRS §4.1 access-tiered model. Order matters: it is the partition order and
    the natural sensitivity ordering, so comparisons use :class:`ClassificationRank`."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    HIGHLY_RESTRICTED = "highly_restricted"


class ClassificationRank(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    HIGHLY_RESTRICTED = 3

    @classmethod
    def of(cls, c: Classification) -> ClassificationRank:
        return cls[c.name]


class Lifecycle(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    REVIEW_REQUIRED = "review_required"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


#: FR-018 — only these are eligible for retrieval unless history is explicitly requested.
RETRIEVABLE_LIFECYCLES: frozenset[Lifecycle] = frozenset(
    {Lifecycle.ACTIVE, Lifecycle.REVIEW_REQUIRED}
)


class IngestStatus(StrEnum):
    PENDING = "pending"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXED = "indexed"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    SKIPPED_UNCHANGED = "skipped_unchanged"


class PrincipalKind(StrEnum):
    USER = "user"
    GROUP = "group"
    ROLE = "role"
    DEPARTMENT = "department"


class SourceType(StrEnum):
    SHAREPOINT = "sharepoint"
    DMS = "dms"
    FILESYSTEM = "filesystem"
    S3 = "s3"
    HTTP = "http"
    MANUAL = "manual"


class SourceStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    ARCHIVED = "archived"


class AppRole(StrEnum):
    END_USER = "end_user"
    KNOWLEDGE_ADMIN = "knowledge_admin"
    SYSTEM_ADMIN = "system_admin"
    SECURITY_ADMIN = "security_admin"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class AnswerState(StrEnum):
    """A first-class column, not a derived flag (ADR-0008).

    Every trust behaviour the SRS requires — refusal, conflict, out-of-scope,
    clarification — becomes a measurable rate rather than a prose intention.
    """

    GROUNDED = "grounded"
    PARTIALLY_GROUNDED = "partially_grounded"
    CONFLICT = "conflict"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CLARIFICATION_NEEDED = "clarification_needed"
    OUT_OF_SCOPE = "out_of_scope"
    REFUSED_SAFETY = "refused_safety"
    ERROR = "error"

    @property
    def is_answered(self) -> bool:
        return self in {self.GROUNDED, self.PARTIALLY_GROUNDED, self.CONFLICT}


class FeedbackRating(StrEnum):
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class FeedbackReason(StrEnum):
    INCORRECT_ANSWER = "incorrect_answer"
    WRONG_SOURCE = "wrong_source"
    OUTDATED_INFORMATION = "outdated_information"
    MISSING_INFORMATION = "missing_information"
    NOT_RELEVANT = "not_relevant"
    UNCLEAR = "unclear"
    OTHER = "other"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class RetrievalStrategy(StrEnum):
    HYBRID = "hybrid"
    SEMANTIC = "semantic"
    KEYWORD = "keyword"


#: Postgres text-search configurations by ISO-639-1 code (03-database-schema.md §3.5).
#: Anything absent falls back to ``simple`` — tokenize without stemming. Guessing a
#: stemmer is worse than not stemming: it produces wrong tokens without erroring.
TS_CONFIG_BY_LANGUAGE: dict[str, str] = {
    "en": "english",
    "fr": "french",
    "pt": "portuguese",
    "es": "spanish",
    "ar": "arabic",
    "de": "german",
    "it": "italian",
    "nl": "dutch",
    "ru": "russian",
}
DEFAULT_TS_CONFIG = "simple"


def ts_config_for(language: str | None) -> str:
    """Resolve a document language to a Postgres text-search configuration.

    Kiswahili and Amharic have no Postgres stemmer, so they resolve to ``simple``
    and rely on trigram matching plus the semantic arm.
    """
    if not language:
        return DEFAULT_TS_CONFIG
    return TS_CONFIG_BY_LANGUAGE.get(language.split("-")[0].lower(), DEFAULT_TS_CONFIG)
