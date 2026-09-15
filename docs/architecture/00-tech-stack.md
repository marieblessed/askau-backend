# AskAU — Technology Stack & Selection Rationale (Phase 1)

The SRS (§2.6) prescribes a *reference* stack and requires the implementation stay
technology-flexible (§2.7, NFR-007). Below is the committed Phase 1 selection, why
each was chosen over the alternative, and the exit path if it must change.

## Committed stack

| Layer | Choice | Version | Why this over the alternative | Exit path |
|---|---|---|---|---|
| Frontend framework | **Next.js (App Router)** | 15.x | RSC lets the chat shell and conversation list render server-side with the user's session already resolved, so first paint doesn't wait on a client fetch. Streaming SSR pairs naturally with SSE token streaming. | Any React host; UI is plain React + a thin `lib/api` client |
| Language (FE) | TypeScript | 5.6 | Shared generated types from the OpenAPI schema — API drift becomes a build error | — |
| Styling | Tailwind CSS + Radix Primitives | 3.4 / 1.x | Radix gives keyboard/ARIA correctness for the citation drawer and menus without adopting a whole design system we'd fight | Any CSS approach; tokens are CSS variables |
| Backend framework | **FastAPI** | 0.115 | SRS-mandated. Native async (retrieval + LLM are I/O-bound), OpenAPI 3.1 generated from Pydantic, first-class SSE | ASGI-standard; routes are thin over `rag/` |
| Language (BE) | Python | 3.12 | Mandated. 3.12 for `TaskGroup`, faster asyncio, better error locations | — |
| Validation | Pydantic v2 | 2.9 | Rust core — request validation is not a measurable cost on the hot path | — |
| DB driver / ORM | asyncpg + SQLAlchemy 2.0 Core | — | Core, **not** the ORM, for retrieval: the hybrid query is hand-written SQL and ORM row hydration is pure overhead. ORM used for admin CRUD where ergonomics matter more than microseconds | — |
| Migrations | Alembic | 1.13 | Standard; hand-written DDL for partitions and vector indexes | — |
| Primary datastore | **PostgreSQL** | 16 | Mandated. One system for relational metadata, ACLs, FTS *and* vectors — hybrid retrieval becomes a single local query instead of a cross-system join | Retriever port isolates it |
| Vector index | **pgvector** | 0.8+ | 0.8 is the floor, not a preference: `iterative_scan` is what makes ACL-filtered ANN return correct top-k. Earlier versions silently under-return on selective filters | `Retriever` port → OpenSearch/Azure AI Search adapter |
| Keyword index | Postgres FTS (`tsvector`, GIN) + `pg_trgm` | built-in | Co-located with vectors, so RRF fusion happens in-database. Sufficient for policy-number/acronym exact match (FR-020). **Stemming configuration is per document language** — see `03-database-schema.md` §3.5 | Same port |
| Pooling | PgBouncer | 1.23 | Transaction pooling; hundreds of API pods cannot each hold a Postgres connection | — |
| Cache / rate limit | Redis | 7.4 | Authorization contexts, embedding cache, answer cache, token buckets, and the ingestion stream — one dependency, four jobs | — |
| Queue | Redis Streams | 7.4 | Consumer groups give at-least-once + resumable checkpointing, which is exactly the ingestion need. Avoids adding RabbitMQ/Kafka for ~10³ msgs/day | Port-isolated in `worker` |
| Identity | **Microsoft Entra ID** | OIDC | Mandated. Auth-code + PKCE; groups/roles from token claims and Graph, cached | OIDC-standard; no Entra-specific logic outside `core/identity` |
| LLM (primary) | **Self-hosted open weights on vLLM** — 8B class (Llama-3.1-8B / Qwen2.5-7B) | — | Satisfies data residency (NFR-006) and enterprise data protection (FR-041) by construction rather than by contract. Size is set by latency, not preference — see below | `LLMPort` adapter |
| LLM (managed option) | Azure OpenAI | — | Retained as a configuration option if AUC later prefers it | `LLMPort` adapter |
| LLM (dev) | Ollama, plus a deterministic `EchoProvider` | — | Local dev and CI must run with **no** cloud credentials and produce deterministic outputs for tests | config value |
| Embeddings | **`bge-m3`** @ 1024 dims (self-hosted) | — | **Multilingual is a requirement, not a preference**: it is what lets an English question retrieve a French policy, which the keyword arm structurally cannot do. 1024 dims keeps HNSW memory ~33% below 1536 with negligible retrieval loss at this corpus size | `EmbedderPort` |
| Reranker | `bge-reranker-v2-m3` on vLLM (or Azure semantic ranker) | — | Cross-encoder reranking is the single highest-leverage quality lever (FR-022); multilingual matters for AUC | `RerankerPort` |
| Document extraction | PyMuPDF, python-docx, openpyxl, python-pptx, Tesseract (OCR) | — | PyMuPDF preserves page and block geometry, which is what makes page-accurate citations (FR-029) possible at all. **Tesseract needs the language packs for every script in scope** — `amh` for Ethiopic is not installed by default | `Extractor` port per MIME type |
| Observability | OpenTelemetry → Azure Monitor / Grafana-Tempo-Loki-Mimir | — | Vendor-neutral instrumentation; one span tree covers HTTP → retrieval → LLM | OTLP standard |
| CI | GitLab CI | — | Test, lint, type-check, dependency + secret scan, SBOM, migration dry-run, contract diff. Jobs invoke open-source tools directly rather than tier-gated GitLab templates, so the pipeline runs identically on any licence tier | Standard YAML; job scripts are plain shell |
| Secrets | Environment variables, injected externally | — | NFR-004c; neither repository contains a secret or a connection string | — |

## Model size is a latency decision, not a preference

With self-hosted open weights the marginal token is free, so the instinct is to run the
largest model the hardware allows. **That instinct is wrong here.** Answer time is
dominated by *per-stream* generation rate, which falls roughly linearly with model size,
and NFR-002b allows 10 seconds for a complete answer.

```
TTFT       = 173 ms (pipeline, see 07) + prompt_tokens ÷ prefill_rate + ~50 ms scheduling
generation = output_tokens ÷ per_stream_rate
```

At a 6,500-token prompt and a 400-token answer, representative single-GPU figures:

| Model | TTFT | Generation | Total | NFR-002a < 3 s | NFR-002b < 10 s |
|---|---|---|---|---|---|
| **8B** | 440 ms | 4.2 s | **4.7 s** | pass | **pass** |
| **14B** | 584 ms | 6.2 s | **6.7 s** | pass | **pass** |
| 32B | 873 ms | 10.0 s | 10.9 s | pass | **fail** |
| 70B | 1.5 s | 18.2 s | 19.7 s | pass | **fail** |

**Start at 8B; 14B is the quality upgrade path.** A 32B model only fits the budget if
answers shorten to ~250 tokens; 70B does not fit at this answer length at all.

Two qualifications, both of which should be measured rather than assumed:

- **Time to first token is comfortable in every case** — users see words within a second
  even at 70B. It is the *complete* answer that breaches, and only for long answers.
- **Answer length is a lever we control.** The system prompt already targets concise,
  structured responses (FR-032). If real answers average 250 tokens rather than 400, a
  32B model lands around 7.2 s and becomes viable.

These figures are representative, not measured — they vary with GPU, vLLM version,
quantization, and batch composition. The `messages` table records `ttft_ms` and
`total_ms` on every answer precisely so this choice can be settled with evidence from
the real corpus during Month 3.

One configuration detail that is easy to miss and expensive to get wrong: **run the KV
cache in FP8.** At FP16 it becomes the largest consumer of GPU memory at realistic
concurrency, exceeding the model weights themselves. It is a serving flag, not an
architectural change.

## Notable rejections

- **LangChain / LlamaIndex as the core.** Rejected. The RAG pipeline here is ~10 well-understood
  stages, and each one needs auditable, testable behavior tied to a specific FR ID. A framework
  that abstracts prompt assembly and retrieval makes the authorization boundary (T1) harder to prove
  to a security reviewer, which is the one thing this project cannot compromise on. We own ~600 lines
  of orchestration instead of inheriting an upgrade treadmill.
- **A dedicated vector DB (Pinecone/Weaviate/Qdrant) in Phase 1.** Rejected for now. It splits ACL
  metadata from vectors, turning one local query into a cross-system fan-out plus a consistency
  problem on permission changes. Revisit when measured p95 retrieval exceeds ~150 ms or the corpus
  passes ~10⁸ chunks.
- **GraphQL.** Rejected. The SRS mandates REST/OpenAPI (§3.4) and the surface is small and
  streaming-shaped; GraphQL would complicate SSE and per-field authorization for no gain.
- **Storing original documents in AskAU.** Rejected by BR-003/§2.7 — originals stay authoritative
  in Azure Blob Storage. AskAU stores extracted text, chunks, embeddings, and a resolvable URI.

## Out of scope for these repositories

Containerization, orchestration, and infrastructure provisioning are owned by the
existing platform tooling, maintained separately. PostgreSQL, Redis, and any
model-serving processes are provisioned there. Both application repositories reach
them through configuration and assume nothing about how they are hosted — which is
also what keeps the operating-environment choice (§2.6 leaves it to AUC) genuinely open.

## Environment matrix

Configured entirely through environment variables; the application is identical across all four.

| | Dev | Test/CI | UAT | Production |
|---|---|---|---|---|
| LLM | Echo / Ollama | Echo (deterministic) | Azure OpenAI (non-prod) | Azure OpenAI |
| Embeddings | Hash (offline) | Hash (deterministic) | Real provider | Real provider |
| Data | Synthetic corpus | Synthetic fixtures | Anonymized subset | Live approved sources |
| Auth | Local IdP stub | Stub + signed test JWTs | Entra (UAT tenant) | Entra (prod tenant) |
| Postgres | Externally provisioned | Ephemeral, CI-provided | Managed, 1 replica | Managed HA, 2 replicas, PITR |

Production data is never used in Dev (§2.6).
