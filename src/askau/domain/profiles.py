"""Named retrieval profiles (FR-016, the client's "higher intelligence" toggle).

Two things this module exists to prevent.

**Retrieval width must never be a client parameter.** `candidate_k` is how much
work the database is asked to do; a browser that can set it can ask for 500
candidates per question, and no amount of validation makes that a good idea.
Naming the tiers server-side means the wire carries a *choice between two
supported behaviours*, not a number, and the numbers stay tunable without an
API change.

**"Higher intelligence" is not a quality dial the reader selects.** Their copy
is precise about this — *"AskAU can automatically use more thorough retrieval
when answering complex questions"* — so the setting is consent to escalate, and
something else decides when escalation is warranted. That decision lives in
`rag/orchestrator.py`; this module only says what the two tiers are.

The escalation is a *second pass*, not a wider first pass. Deciding a question
is "complex" before retrieving is guesswork, and it would make every question
more expensive to help the few that need it. Retrieving normally and widening
only when the evidence gate is unconvinced uses the one signal that is actually
about this question, and costs nothing on the answers that were already fine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RetrievalTier(StrEnum):
    STANDARD = "standard"
    THOROUGH = "thorough"


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    """The knobs one tier sets. Widening is the only difference between them.

    `top_k` deliberately does *not* grow with the tier. More candidates give the
    reranker more to choose from, which is the point; more chunks in the prompt
    is a different and worse change — it dilutes the context, costs tokens
    linearly, and pushes the material that matters further from the instruction.
    """

    tier: RetrievalTier
    candidate_k: int
    top_k: int
    rerank_input_k: int


def profile_for(
    tier: RetrievalTier, *, candidate_k: int, top_k: int, rerank_input_k: int
) -> RetrievalProfile:
    """Build a profile from the configured baseline.

    `standard` *is* the configured baseline, so existing deployments behave
    exactly as they did and the tier system is inert until something escalates.
    `thorough` is a multiple of it rather than a second set of absolute numbers:
    an operator who tunes `retrieval_candidate_k` down for a small corpus should
    not find the escalated path still reaching for 240.
    """
    if tier is RetrievalTier.STANDARD:
        return RetrievalProfile(tier, candidate_k, top_k, rerank_input_k)
    return RetrievalProfile(
        tier=RetrievalTier.THOROUGH,
        # Capped at the same ceiling `Settings` enforces on the configured
        # value, so escalation can never ask for more work than an operator
        # could have configured deliberately.
        candidate_k=min(candidate_k * 4, 500),
        top_k=top_k,
        rerank_input_k=min(rerank_input_k * 4, 200),
    )
