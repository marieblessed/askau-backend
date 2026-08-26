"""Provider selection from configuration.

Adding a provider is one adapter file plus one branch here — the promise FR-042
makes and the import contracts keep honest.
"""

from __future__ import annotations

from askau.core.errors import ConfigurationError
from askau.llm.ports import LLM, Embedder
from askau.settings import Settings


def build_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "echo":
        from askau.llm.adapters.echo import EchoLLM

        return EchoLLM(settings.llm_model)

    if settings.llm_provider == "openai_compatible":
        from askau.llm.adapters.openai_compatible import OpenAICompatibleLLM

        return OpenAICompatibleLLM(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )

    raise ConfigurationError(f"Unknown LLM provider: {settings.llm_provider}")


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hash":
        from askau.llm.adapters.hash_embedder import HashEmbedder

        return HashEmbedder(settings.embedding_dim, settings.embedding_model)

    if settings.embedding_provider == "sentence_transformers":
        from askau.llm.adapters.sentence_transformers_embedder import (
            SentenceTransformersEmbedder,
        )

        return SentenceTransformersEmbedder(settings.embedding_model, settings.embedding_dim)

    if settings.embedding_provider == "openai_compatible":
        from askau.llm.adapters.openai_compatible_embedder import OpenAICompatibleEmbedder

        return OpenAICompatibleEmbedder(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dim,
        )

    raise ConfigurationError(f"Unknown embedding provider: {settings.embedding_provider}")
