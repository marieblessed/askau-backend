"""Local multilingual embeddings via sentence-transformers.

Opt-in for realistic offline retrieval testing: unlike the hash embedder this
produces genuine semantic vectors, so ranking quality becomes meaningful without
standing up a model server. Slow on CPU, which is why it is not the default.

Defaults to ``bge-m3`` — multilingual is a requirement, not a preference
(ADR-0015): it is the only component that can match an English question to a
French policy.
"""

from __future__ import annotations

import asyncio
from typing import Any


class SentenceTransformersEmbedder:
    def __init__(self, model: str = "BAAI/bge-m3", dimensions: int = 1024) -> None:
        self._model_name = model
        self._dim = dimensions
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dim

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        def run() -> list[list[float]]:
            model = self._load()
            # normalize_embeddings so cosine distance behaves, matching the
            # vector_cosine_ops index the schema builds.
            arr = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            return [self._fit(list(map(float, row))) for row in arr]

        # Inference is CPU-bound and would otherwise block the event loop.
        return await asyncio.to_thread(run)

    def _fit(self, vec: list[float]) -> list[float]:
        """Truncate to the configured dimension (Matryoshka-style) or pad.

        bge-m3 emits 1024 natively, so this is normally a no-op; it exists so a
        different model can be swapped in without a migration.
        """
        if len(vec) == self._dim:
            return vec
        if len(vec) > self._dim:
            head = vec[: self._dim]
            norm = sum(v * v for v in head) ** 0.5
            return [v / norm for v in head] if norm else head
        return vec + [0.0] * (self._dim - len(vec))
