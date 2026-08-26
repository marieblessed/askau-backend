"""Application configuration — the single config surface.

Infrastructure lives outside this repository (ADR-0014), which makes a missing or
malformed environment variable the most likely first-run failure. So this module
validates aggressively and fails at startup with a message naming the variable,
rather than surfacing twenty minutes later as a confusing query error.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["dev", "entra"]
EmbeddingProvider = Literal["hash", "sentence_transformers", "openai_compatible"]
RetrieverName = Literal["pgvector_hybrid", "opensearch", "azure_ai_search"]
RerankerName = Literal["noop", "cross_encoder"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASKAU_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── core ────────────────────────────────────────────────────────────────
    env: Literal["development", "test", "uat", "production"] = "development"
    log_level: str = "INFO"

    # ── database ────────────────────────────────────────────────────────────
    database_url: str
    database_ro_url: str = ""
    migration_database_url: str = ""
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)

    # ── redis ───────────────────────────────────────────────────────────────
    redis_url: str

    # ── identity ────────────────────────────────────────────────────────────
    auth_mode: AuthMode = "dev"
    dev_token_secret: str = ""
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_audience: str = ""

    # ── embeddings ──────────────────────────────────────────────────────────
    embedding_provider: EmbeddingProvider = "hash"
    embedding_model: str = "hash-1024"
    embedding_dim: int = Field(default=1024, ge=64, le=4096)
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    # ── retrieval ───────────────────────────────────────────────────────────
    retriever: RetrieverName = "pgvector_hybrid"
    retrieval_candidate_k: int = Field(default=60, ge=1, le=500)
    retrieval_top_k: int = Field(default=8, ge=1, le=100)
    reranker: RerankerName = "noop"
    rerank_input_k: int = Field(default=40, ge=1, le=200)
    rrf_k: int = Field(default=60, ge=1)
    min_pgvector_version: str = "0.8.0"
    hnsw_iterative_scan: Literal["off", "relaxed_order", "strict_order"] = "relaxed_order"
    hnsw_max_scan_tuples: int = Field(default=20_000, ge=1)

    # ── RAG thresholds ──────────────────────────────────────────────────────
    min_evidence_score: float = Field(default=0.015, ge=0.0, le=1.0)
    min_evidence_chunks: int = Field(default=1, ge=1, le=20)
    groundedness_floor: float = Field(default=0.40, ge=0.0, le=1.0)
    context_token_budget: int = Field(default=6000, ge=500, le=64_000)
    llm_provider: Literal["echo", "openai_compatible"] = "echo"
    llm_model: str = "echo-1"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_max_tokens: int = Field(default=800, ge=64, le=8000)

    # ── caching / limits ────────────────────────────────────────────────────
    authz_cache_ttl: int = Field(default=300, ge=0)
    embed_cache_ttl: int = Field(default=86_400, ge=0)
    rate_limit_per_minute: int = Field(default=30, ge=1)
    rate_limit_per_hour: int = Field(default=300, ge=1)

    # ── ingestion ───────────────────────────────────────────────────────────
    chunk_target_tokens: int = Field(default=380, ge=50, le=2000)
    chunk_overlap_tokens: int = Field(default=60, ge=0, le=500)
    chunk_max_tokens: int = Field(default=512, ge=64, le=4000)
    #: OCR runs as a service, never as a host install: an engine on the host
    #: makes behaviour depend on how the machine was provisioned, and a missing
    #: one fails silently (scans index as nothing). "none" is off.
    ocr_provider: Literal["none", "tika"] = "none"
    ocr_url: str = ""
    #: Language data to request, comma-separated. `amh` for Ethiopic and `ara`
    #: for Arabic are not in a minimal image. Declaring them is what makes their
    #: absence detectable — an undeclared language is invisible to every check.
    ocr_languages: str = "eng"
    acl_sync_interval_seconds: int = Field(default=900, ge=30)

    # ── validation ──────────────────────────────────────────────────────────

    @field_validator("database_url", "database_ro_url", "migration_database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if v and not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "must be a postgresql+asyncpg:// URL — the retrieval path is async and "
                f"a sync driver would block the event loop (got {v.split('://')[0]}://)"
            )
        return v

    @field_validator("min_pgvector_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", v):
            raise ValueError(f"must be a three-part version, got {v!r}")
        return v

    @model_validator(mode="after")
    def _cross_field(self) -> Settings:
        if self.chunk_overlap_tokens >= self.chunk_target_tokens:
            raise ValueError(
                "ASKAU_CHUNK_OVERLAP_TOKENS must be smaller than "
                "ASKAU_CHUNK_TARGET_TOKENS, or chunking cannot make progress"
            )
        if self.chunk_target_tokens > self.chunk_max_tokens:
            raise ValueError("ASKAU_CHUNK_TARGET_TOKENS cannot exceed ASKAU_CHUNK_MAX_TOKENS")
        if self.retrieval_top_k > self.retrieval_candidate_k:
            raise ValueError(
                "ASKAU_RETRIEVAL_TOP_K cannot exceed ASKAU_RETRIEVAL_CANDIDATE_K — "
                "you cannot rank more results than were retrieved"
            )

        if self.auth_mode == "dev":
            if self.env == "production":
                raise ValueError(
                    "ASKAU_AUTH_MODE=dev is refused in production: it accepts "
                    "locally-signed tokens and would bypass Entra entirely"
                )
            if not self.dev_token_secret:
                raise ValueError("ASKAU_DEV_TOKEN_SECRET is required when AUTH_MODE=dev")
        else:
            missing = [
                name
                for name in ("entra_tenant_id", "entra_client_id", "entra_audience")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    "AUTH_MODE=entra requires: " + ", ".join(f"ASKAU_{m.upper()}" for m in missing)
                )

        if self.llm_provider == "openai_compatible" and not self.llm_base_url:
            raise ValueError(
                "ASKAU_LLM_BASE_URL is required when ASKAU_LLM_PROVIDER=openai_compatible"
            )

        if self.env == "production" and self.llm_provider == "echo":
            raise ValueError(
                "ASKAU_LLM_PROVIDER=echo is refused in production: it is a "
                "deterministic stub that cannot reason or refuse"
            )

        if self.ocr_provider != "none" and not self.ocr_url:
            raise ValueError(
                "ASKAU_OCR_URL is required when ASKAU_OCR_PROVIDER is not 'none' — "
                "OCR runs as a service, so there is nowhere to send the work without it"
            )

        if self.embedding_provider == "openai_compatible" and not self.embedding_base_url:
            raise ValueError(
                "ASKAU_EMBEDDING_BASE_URL is required when "
                "ASKAU_EMBEDDING_PROVIDER=openai_compatible"
            )

        if self.env == "production" and self.embedding_provider == "hash":
            raise ValueError(
                "ASKAU_EMBEDDING_PROVIDER=hash is refused in production: hash embeddings "
                "are deterministic placeholders with no semantic meaning, so retrieval "
                "would silently return irrelevant results"
            )
        return self

    # ── derived ─────────────────────────────────────────────────────────────

    @property
    def read_url(self) -> str:
        """Retrieval traffic is read-only; route it to a replica when one exists."""
        return self.database_ro_url or self.database_url

    @property
    def migration_url(self) -> str:
        """Migrations need DDL rights the least-privilege app role does not have."""
        return self.migration_database_url or self.database_url

    @property
    def ocr_enabled(self) -> bool:
        return self.ocr_provider != "none"

    @property
    def is_dev_auth(self) -> bool:
        return self.auth_mode == "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton. Constructing this is where a bad environment fails."""
    return Settings()
