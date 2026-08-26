"""Retrieval ports.

Protocols, not base classes: an adapter satisfies the contract structurally, so
adding a provider means adding one file and one config value (FR-042). The
``rag-uses-ports-only`` import contract means orchestration can reach these and
nothing behind them.

``Embedder`` deliberately lives in ``askau.llm.ports`` instead: it is a model
provider, not a search backend, and retrieval never calls it — a
``RetrievalQuery`` arrives with its vector already computed. The layered import
contract is what surfaced that; the protocol had been filed by association
rather than by responsibility.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from askau.domain.retrieval import RetrievalQuery, RetrievalResult, RetrievedChunk


@runtime_checkable
class Retriever(Protocol):
    """Finds authorized, relevant chunks.

    Every implementation takes a ``RetrievalQuery``, which carries the
    ``AuthorizationContext``. There is no overload without it — an adapter cannot
    be called in an unauthorized way.
    """

    async def search(self, query: RetrievalQuery) -> RetrievalResult: ...


@runtime_checkable
class Reranker(Protocol):
    """Reorders candidates by relevance (FR-022)."""

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]: ...
