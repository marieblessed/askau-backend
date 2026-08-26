"""LLM and embedding ports (FR-042).

No business logic imports a vendor SDK; adapters are the only place they may
appear, enforced by the ``no-sdk-outside-adapters`` import contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CompletionChunk:
    text: str
    is_final: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""


@runtime_checkable
class Embedder(Protocol):
    """Text to vector, for both queries and document chunks."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class LLM(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def provider(self) -> str: ...

    async def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str: ...

    def stream(
        self, system: str, user: str, *, max_tokens: int = 800
    ) -> AsyncIterator[CompletionChunk]: ...
