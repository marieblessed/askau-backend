"""Chat completions from any OpenAI-compatible endpoint.

One adapter covers self-hosted vLLM, Ollama and Azure OpenAI. The self-hosted
path is the Phase 1 default: it satisfies data residency (NFR-006) and the
enterprise data-protection requirement (FR-041) structurally rather than
contractually.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from askau.core.errors import UpstreamUnavailableError
from askau.llm.ports import CompletionChunk


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str,
        provider: str = "vllm",
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._provider = provider
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    def _payload(self, system: str, user: str, max_tokens: int) -> dict[str, object]:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            # Low but non-zero: grounded extraction should be near-deterministic,
            # and a temperature of exactly 0 makes some servers degenerate into
            # repetition loops on long contexts.
            "temperature": 0.1,
        }

    async def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=self._payload(system, user, max_tokens),
            )
            resp.raise_for_status()
            return str(resp.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            # Fails to an error, never to an ungrounded fallback (ADR-0010).
            raise UpstreamUnavailableError("The language model is unavailable") from exc

    async def stream(
        self, system: str, user: str, *, max_tokens: int = 800
    ) -> AsyncIterator[CompletionChunk]:
        payload = self._payload(system, user, max_tokens) | {"stream": True}
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    body = line[6:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        delta = json.loads(body)["choices"][0]["delta"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if content := delta.get("content"):
                        yield CompletionChunk(str(content))
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError("The language model is unavailable") from exc
        yield CompletionChunk("", is_final=True)

    async def aclose(self) -> None:
        await self._client.aclose()
