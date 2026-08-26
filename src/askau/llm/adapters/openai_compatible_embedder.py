"""Embeddings from any OpenAI-compatible endpoint.

Covers self-hosted vLLM, Ollama's compatibility endpoint, and Azure OpenAI — all
three speak the same ``/embeddings`` shape, so one adapter serves the production
path and the sovereign path alike (FR-041, FR-042).
"""

from __future__ import annotations

import httpx

from askau.core.errors import UpstreamUnavailableError


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dimensions
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dim

    async def embed_query(self, text: str) -> list[float]:
        return (await self._post([text]))[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        # Batched: one request per chunk would dominate ingestion wall-clock.
        for start in range(0, len(texts), 64):
            out.extend(await self._post(texts[start : start + 64]))
        return out

    async def _post(self, inputs: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self._model, "input": inputs}
        # Only send `dimensions` when truncation is actually wanted; models that
        # do not support Matryoshka truncation reject the field outright.
        if self._dim:
            payload["dimensions"] = self._dim
        try:
            resp = await self._client.post(f"{self._base_url}/embeddings", json=payload)
            resp.raise_for_status()
            data = resp.json()["data"]
        except Exception as exc:
            raise UpstreamUnavailableError("The embedding service is unavailable") from exc

        vectors = [list(map(float, item["embedding"])) for item in data]
        for v in vectors:
            if len(v) != self._dim:
                # A dimension mismatch would be rejected by the vector column
                # anyway, but the error there says nothing useful about why.
                raise UpstreamUnavailableError(
                    f"Embedding service returned {len(v)} dimensions, "
                    f"expected {self._dim}. Check ASKAU_EMBEDDING_MODEL and "
                    f"ASKAU_EMBEDDING_DIM agree with the deployed model."
                )
        return vectors

    async def aclose(self) -> None:
        await self._client.aclose()
