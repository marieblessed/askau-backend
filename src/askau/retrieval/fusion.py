"""Reciprocal Rank Fusion.

Used instead of weighted score blending because cosine distance and
``ts_rank_cd`` are not on comparable scales — a weighting that works on one
corpus drifts as the corpus grows, whereas rank fusion needs no tuning at all
(ADR-0004).

The fusion itself runs in SQL (see ``pgvector_hybrid``). This module exists so
the arithmetic is unit-testable without a database, and so an adapter whose
backend cannot fuse server-side has a correct implementation to reuse.
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_K = 60


def rrf_score(rank: int, k: int = DEFAULT_K) -> float:
    """Contribution of one arm at ``rank`` (1-based).

    The constant ``k`` damps the influence of top ranks: without it, rank 1 would
    dominate so heavily that agreement between arms would stop mattering.
    """
    if rank < 1:
        raise ValueError("rank is 1-based")
    return 1.0 / (k + rank)


def fuse(ranked_lists: Sequence[Sequence[int]], k: int = DEFAULT_K) -> list[tuple[int, float]]:
    """Fuse ranked ID lists into one ordering, highest score first.

    An item found by both arms accumulates both contributions, so agreement
    between semantic and keyword retrieval is rewarded — which is the property
    that makes hybrid better than either alone.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for position, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + rrf_score(position, k)
    # Tie-break on ID so ordering is deterministic — an unstable sort makes
    # evaluation runs irreproducible for no benefit.
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
