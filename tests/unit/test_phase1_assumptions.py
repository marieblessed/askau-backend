"""Assumptions we are knowingly running on, pinned so they cannot drift.

An assumption nobody wrote down is indistinguishable from a bug, and that is
exactly how the lifecycle one survived review: `'active'` sat in an INSERT as a
bare literal, looking like a value someone had chosen for a reason.

Each test here fails when the assumption changes. That is the point — the
failure is a prompt to update the documentation that describes it, not a defect
to be silenced. A test that merely asserts current behaviour would be noise;
these assert *decisions*, and name where the decision is recorded.
"""

from __future__ import annotations

from askau.ingestion.pipeline import PHASE1_ASSUMED_LIFECYCLE
from askau.retrieval.policy import VersionPolicy


class TestEverythingFromAnApprovedSourceIsTreatedAsApproved:
    """BR-001 approves the source; Phase 1 does not approve documents.

    A knowledge administrator approves a blob container once. Nothing inspects
    the documents inside it, so a draft written into an approved container is
    indexed as current policy and can be quoted as such.

    Accepted for Phase 1 by decision. The intended fix is to take the document's
    own lifecycle from blob metadata, the way classification and the validity
    window already are (ADR-0029), when the approval work is picked up — see
    `docs/architecture/10-traceability.md`.
    """

    def test_ingestion_marks_every_document_current(self) -> None:
        assert PHASE1_ASSUMED_LIFECYCLE == "active", (
            "Ingestion no longer assumes every document is approved. If document-level "
            "approval has arrived, update the open question in "
            "docs/architecture/10-traceability.md and docs/data-models.md, then change "
            "this test to describe whatever now decides a document's lifecycle."
        )

    def test_retrieval_is_ready_for_the_distinction_ingestion_cannot_yet_make(self) -> None:
        """The half that is already built.

        Retrieval filters on lifecycle correctly and in both search arms — a
        filter on one arm only would let the other surface a draft, which is the
        same class of bug as an ACL check on one arm. So closing the gap is a
        change to what ingestion *writes*, not to how retrieval reads.
        """
        predicate = VersionPolicy().sql_predicate()
        assert "lifecycle IN ('active','review_required')" in predicate
        assert "is_current" in predicate

    def test_a_draft_would_be_excluded_if_anything_ever_marked_one(self) -> None:
        """Proves the filter would bite, so the gap really is only the input."""
        predicate = VersionPolicy().sql_predicate()
        for excluded in ("draft", "expired", "superseded"):
            assert f"'{excluded}'" not in predicate, f"retrieval would admit {excluded} documents"
