"""Pass-through reranker.

The cross-encoder is deferred: it is the highest-leverage quality lever (FR-022)
and also the most expensive stage, so it is introduced once retrieval quality is
measurable rather than before. The port exists now so adding it is one file.
"""

from __future__ import annotations

from askau.domain.retrieval import RetrievedChunk


class NoopReranker:
    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        return chunks[:top_k]
