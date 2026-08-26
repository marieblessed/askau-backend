"""Version and validity rules for retrieval (FR-017, FR-018).

Expressed as SQL fragments rather than post-filters. Filtering after retrieval
would silently shrink the result set below ``top_k`` — you would ask for 8 and
get 3, with no indication that 5 were discarded as superseded.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VersionPolicy:
    """How strictly retrieval restricts itself to current, in-force content."""

    include_historical: bool = False
    respect_effective_dates: bool = True

    def sql_predicate(self) -> str:
        """Predicate fragment applied inside *both* retrieval arms.

        Both, not one: a filter on the semantic arm alone would let the keyword
        arm surface superseded policy, which is the same class of bug as an ACL
        filter on one arm only.
        """
        clauses: list[str] = []
        if not self.include_historical:
            clauses.append("c.is_current AND c.lifecycle IN ('active','review_required')")
        if self.respect_effective_dates:
            clauses.append("(c.effective_from IS NULL OR c.effective_from <= CURRENT_DATE)")
            clauses.append("(c.effective_to   IS NULL OR c.effective_to   >= CURRENT_DATE)")
        return " AND ".join(clauses) if clauses else "TRUE"
