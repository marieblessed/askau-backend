"""The authorization context — resolved once per request, before any retrieval.

BR-006 and FR-002 require that the language model never determines authorization.
This module is where that becomes concrete: an ``AuthorizationContext`` is built
from the verified token *before* the retrieval layer runs, and the retriever's only
authorization input is ``principals``. There is deliberately no method here that
takes a document, a question, or any model output and returns an access decision —
authorization is set membership resolved upstream, not a judgment made downstream.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import NewType

from askau.domain.enums import AppRole, Classification, ClassificationRank

PrincipalId = NewType("PrincipalId", int)
UserId = NewType("UserId", str)


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Everything the retrieval layer is allowed to know about the caller.

    ``principals`` is the whole authorization surface: the flattened set of user,
    group, role and department principal IDs. It is intersected against
    ``chunks.acl_principals`` in SQL. Integers rather than UUIDs because these
    values travel inside a ``bigint[]`` on the hot path, where array size is
    cache-residency (ADR-0002).
    """

    user_id: UserId
    principals: frozenset[PrincipalId]
    acl_version: int
    roles: frozenset[AppRole] = frozenset()
    department: str | None = None
    email: str | None = None

    # ── Phase 3 seam (SRS §7.1): delegated authorization for the agent layer.
    # Defined so the context does not need reshaping later; unused in Phase 1.
    on_behalf_of: UserId | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.principals:
            # A caller with no principals can read nothing. Allowing an empty set
            # through would produce a query whose ACL predicate matches nothing —
            # correct, but indistinguishable from a bug. Fail loudly instead.
            raise ValueError(
                f"user {self.user_id} resolved to zero principals; "
                "authorization context cannot be constructed"
            )

    # ── retrieval inputs ────────────────────────────────────────────────────

    def principal_array(self) -> list[int]:
        """Sorted list for the SQL ``bigint[]`` parameter.

        Sorted so identical principal sets produce identical query parameters,
        which keeps prepared-statement plans and cache keys stable.
        """
        return sorted(int(p) for p in self.principals)

    def acl_signature(self) -> str:
        """Stable digest of the principal set.

        This is a component of the answer-cache key (Month 3). Two users with
        different authorization must never share a cache entry, so the signature
        is part of the key rather than a check performed after a lookup — the one
        place where a caching bug would become a disclosure bug.
        """
        joined = ",".join(str(p) for p in self.principal_array())
        return hashlib.blake2b(joined.encode(), digest_size=16).hexdigest()

    # ── role checks (administrative surfaces only) ──────────────────────────

    def has_role(self, role: AppRole) -> bool:
        return role in self.roles

    def has_any_role(self, *roles: AppRole) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def is_admin(self) -> bool:
        return self.has_any_role(
            AppRole.KNOWLEDGE_ADMIN, AppRole.SYSTEM_ADMIN, AppRole.SECURITY_ADMIN
        )


@dataclass(frozen=True, slots=True)
class ClassificationScope:
    """Which classification partitions a query may touch.

    A *performance* optimization derived from the caller's grants, and a
    structural second line of defence — never a substitute for the ACL predicate.
    Partition pruning narrows what is scanned; ``acl_principals`` decides what is
    returned. Both are always applied.
    """

    max_rank: ClassificationRank

    @classmethod
    def from_grants(cls, granted: frozenset[Classification]) -> ClassificationScope:
        if not granted:
            return cls(ClassificationRank.PUBLIC)
        return cls(max(ClassificationRank.of(c) for c in granted))

    def allowed(self) -> tuple[Classification, ...]:
        return tuple(c for c in Classification if ClassificationRank.of(c) <= self.max_rank)
