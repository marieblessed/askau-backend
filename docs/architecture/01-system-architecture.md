# AskAU — System Architecture (Phase 1)

> Enterprise permission-aware Retrieval-Augmented Generation platform for the
> African Union Commission. Traces to `AskAU_SRS_v1.0` (FR-001 … FR-054, NFR-001 … NFR-010).

---

## 1. Design tenets

These are non-negotiable and every component below is shaped by them.

| # | Tenet | Source | Consequence in the build |
|---|-------|--------|--------------------------|
| T1 | **The LLM is never the authorization authority.** | BR-006, FR-002, NFR-004b | ACL filtering is a `WHERE` clause in the retrieval SQL. The model receives only rows that already passed authorization. There is no code path where the model is asked "is this user allowed?" |
| T2 | **Documents are data, never instructions.** | FR-036, FR-037 | Retrieved content is wrapped in non-instruction envelopes, injection-scanned at ingest *and* at prompt time, and the system prompt states an explicit instruction hierarchy. |
| T3 | **No citation without a retrieved chunk.** | FR-030 | Post-generation `CitationValidator` rejects any citation marker that does not resolve to a chunk actually in the context window. Unresolvable markers are stripped and the answer is downgraded. |
| T4 | **Every dependency is replaceable.** | FR-042, NFR-007, §2.7 | LLM, embedder, reranker, retriever, extractor, and connector are all *ports* (Protocols) with pluggable adapters selected by config. No business logic imports a vendor SDK. |
| T5 | **Refuse rather than fabricate.** | FR-028, BR-007 | Evidence sufficiency is scored *before* generation; below threshold the pipeline short-circuits to a deterministic insufficient-evidence response and never calls the LLM. |
| T6 | **Fast by construction, not by tuning.** | NFR-002a/b | Single-round-trip hybrid retrieval, denormalized ACLs, three cache tiers, and token streaming. See §7. |

---

## 2. Logical layers

The SRS (§2.1) mandates seven logical layers so technologies can be swapped independently.
This is the realized mapping:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ L1  PRESENTATION      Next.js 15 App Router · RSC shell + streaming client   │
│                       Chat UI · Citation drawer · Admin console              │
├──────────────────────────────────────────────────────────────────────────────┤
│ L2  IDENTITY          Microsoft Entra ID (OIDC auth-code + PKCE)             │
│                       JWKS validation · group/role resolution · session      │
├──────────────────────────────────────────────────────────────────────────────┤
│ L3  API / EDGE        FastAPI · OpenAPI 3.1 · SSE streaming · rate limits    │
│                       Correlation IDs · problem+json errors · RBAC guards    │
├──────────────────────────────────────────────────────────────────────────────┤
│ L4  ORCHESTRATION     RAG pipeline: understand → retrieve → gate → generate  │
│     (RAG core)        → validate → cite.  Guardrails. Conflict detection.    │
│                       ── Phase 3 seam: swap for an agent graph here ──       │
├──────────────────────────────────────────────────────────────────────────────┤
│ L5  RETRIEVAL         Hybrid search port. pgvector + Postgres FTS adapter    │
│                       (default) · OpenSearch adapter · cross-encoder rerank  │
│                       AUTHORIZATION BOUNDARY — enforced here, in SQL         │
├──────────────────────────────────────────────────────────────────────────────┤
│ L6  GENERATION        LLM port · Azure OpenAI / vLLM / Ollama adapters       │
│                       Embedding port · complexity-based model router         │
├──────────────────────────────────────────────────────────────────────────────┤
│ L7  KNOWLEDGE         PostgreSQL 16 + pgvector (chunks, ACLs, metadata)      │
│                    Object store (originals stay in Azure Blob Storage)       │
└──────────────────────────────────────────────────────────────────────────────┘
    CROSS-CUTTING: audit log (append-only) · OpenTelemetry · model usage ledger ·
                   secrets (Key Vault) · evaluation harness
```

---

## 3. Runtime topology

```
                             ┌───────────────┐
   Browser ──── HTTPS ───────│  CDN / WAF    │
   (AUC staff)               │  Front Door   │
                             └───────┬───────┘
                     ┌───────────────┴────────────────┐
                     │                                │
             ┌───────▼────────┐              ┌─────────▼─────────┐
             │  web (Next.js) │              │  Entra ID (OIDC)  │
             │  N replicas    │              └───────────────────┘
             │  stateless     │
             └───────┬────────┘
                     │  /api/v1/*  (Bearer, HTTP/2)
             ┌───────▼──────────────────────────────────┐
             │  api (FastAPI)  ── autoscaled, stateless │
             │  ├─ auth/session   ├─ conversations      │
             │  ├─ messages(SSE)  ├─ knowledge          │
             │  └─ admin/metrics  └─ evaluation         │
             └──┬─────────┬──────────┬──────────┬───────┘
                │         │          │          │
        ┌───────▼──┐  ┌───▼─────┐ ┌──▼───────┐ ┌▼──────────────┐
        │  Redis   │  │ PgBouncer│ │ LLM      │ │ Queue         │
        │ ·authz   │  │    │     │ │ gateway  │ │ (Redis Stream)│
        │ ·embed$  │  │    ▼     │ │ ·chat    │ └──┬────────────┘
        │ ·answer$ │  │ Postgres │ │ ·embed   │    │
        │ ·rate    │  │ primary  │ │ ·rerank  │    │
        └──────────┘  │  + 2 RR  │ └──────────┘    │
                      │ pgvector │            ┌────▼──────────────┐
                      └──────────┘            │ worker (ingestion)│
                                              │ extract·chunk·    │
                                              │ embed·index·ACL   │
                                              └────┬──────────────┘
                                                   │ connectors
                                     ┌─────────────▼───────────────┐
                                     │ Azure Blob · FS · manual    │
                                     │ (authoritative originals)   │
                                     └─────────────────────────────┘
```

**Why stateless everywhere:** every request carries its own authorization context
derived from the bearer token plus a Redis-cached principal set. No sticky sessions,
so `api` and `web` scale purely horizontally (NFR-003).

---

## 4. The two hot paths

### 4.1 Query path (target: first token < 800 ms p50, < 3 s p95)

```
 1. POST /v1/conversations/{id}/messages          ~2 ms   auth: JWT verify (cached JWKS)
 2. Resolve AuthorizationContext                  ~1 ms   Redis hit → bigint[] principals
 3. Rate limit + guardrail scan on user input     ~1 ms   token bucket, injection regexes
 4. Query understanding                          ~15 ms   coref resolution from last N turns,
                                                          strategy pick, filter extraction
                                                          (heuristic-first; LLM only if needed)
 5. Answer cache probe                            ~3 ms   key = (acl_signature, normalized_q)
                                                          ── HIT → stream cached, done ──
 6. Embed query                                  ~25 ms   Redis embed-cache, else provider
 7. HYBRID RETRIEVE — one SQL round trip        ~20-40 ms vector ANN ∥ FTS, RRF-fused,
                                                          ACL-prefiltered, version-aware
 8. Rerank top-40 → top-8                       ~40 ms    cross-encoder; skipped when the
                                                          score margin is already decisive
 9. Evidence sufficiency gate                     ~1 ms   below threshold → refuse (no LLM call)
10. Context assembly + conflict detection        ~5 ms    dedupe, token budget, envelope wrap
11. LLM stream                             TTFT ~300 ms   SSE → client token by token
12. Citation validation + persist              async      never blocks the stream
```

Steps 1–10 are ~110 ms of controllable latency. The LLM dominates, so everything
before it is built to get out of the way.

### 4.2 Ingestion path (async, idempotent, resumable)

```
discover → validate → fetch → extract(+OCR) → normalize → inject-scan
        → chunk(structure-aware) → embed(batched) → index(txn)
        → version-reconcile → ACL-materialize → audit
```

Every stage is checkpointed per document in `ingestion_tasks`, so a failed run
resumes rather than restarts. Content-hash comparison means unchanged documents
cost one HEAD request.

---

## 5. The three decisions that make it fast

### 5.1 Denormalized ACLs on the chunk row → authorization is free

The naive design joins `chunks → documents → document_acl → user_principals` at
query time. That is 3 joins on the hot path, and it defeats ANN indexes because
the vector index cannot pre-filter.

Instead, `chunks` carries a materialized `acl_principals BIGINT[]` — the set of
principal IDs (user, group, role, department) allowed to read it — with a GIN index:

```sql
WHERE c.acl_principals && $user_principals::bigint[]   -- GIN overlap, index-only
```

One index operation replaces three joins. `BIGINT` (not UUID) keeps the arrays
compact so they stay in cache.

**Cost of this choice:** an ACL change at the source must be propagated to chunk
rows. That is exactly what FR-025 permits (*"within the defined synchronization
period"*). The `acl_reconciler` job bulk-updates affected chunks and the
`acl_version` counter invalidates cached authorization contexts. Correctness is
preserved because `document_acl` remains the authoritative table and the
materialized array is always derived from it — never edited directly.

### 5.2 Classification-partitioned chunk table → most queries touch less data

`chunks` is LIST-partitioned on `classification_tier` (public / internal /
confidential / highly_restricted). Each partition carries its own HNSW index.

The overwhelming majority of users are authorized for `public` + `internal` only,
so the planner prunes the two restricted partitions entirely — smaller HNSW graphs,
better recall per unit of work, and a *structural* second line of defense behind
the ACL predicate. Partition pruning is derived from the authorization context, not
from user input.

### 5.3 Hybrid retrieval as a single SQL statement with in-database RRF

Two round trips (vector, then keyword) plus application-side merge costs two
network hops and Python-side sorting. Instead one CTE does vector ANN and FTS
in parallel inside Postgres and fuses them with Reciprocal Rank Fusion:

```sql
WITH sem AS (SELECT id, row_number() OVER (ORDER BY embedding <=> $q) rnk FROM chunks
             WHERE <acl> AND <version> ORDER BY embedding <=> $q LIMIT 60),
     kw  AS (SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv,$tsq) DESC) rnk FROM chunks
             WHERE <acl> AND <version> AND tsv @@ $tsq LIMIT 60)
SELECT id, SUM(1.0/(60 + rnk)) AS rrf FROM (sem UNION ALL kw) GROUP BY id ORDER BY rrf DESC
```

RRF is used deliberately over weighted score blending: cosine distance and
`ts_rank_cd` are not on a comparable scale, so rank-based fusion needs no
per-corpus tuning and stays stable as the corpus grows.

### 5.4 Three cache tiers

| Tier | Key | TTL | Guards against |
|------|-----|-----|----------------|
| Authorization context | `authz:{user}:{acl_version}` | 5 min | Entra/DB round trip per request |
| Query embedding | `emb:{model}:{sha256(norm_q)}` | 24 h | Repeat embedding spend |
| Grounded answer | `ans:{acl_signature}:{sha256(norm_q)}` | 15 min | Full pipeline re-execution |

The answer cache key **includes the ACL signature** — a hash of the caller's
principal set. Two users with different authorization can never share a cache
entry. This is the one place where a cache bug would become a data-leak bug, so
the signature is part of the key rather than a post-hoc check.

---

## 6. Security architecture

```
                       ┌──────────────────────────────────────┐
   user question ──────│ INPUT GUARDRAIL                      │
                       │ · injection heuristics               │
                       │ · PII/secret pattern scan            │
                       │ · length + rate limits               │
                       └──────────────┬───────────────────────┘
                                      ▼
                       ┌──────────────────────────────────────┐
                       │ AUTHORIZATION BOUNDARY  (pre-LLM)    │
                       │ principals ∩ chunk.acl_principals    │
                       │ + partition pruning by classification│
                       │ + lifecycle/effective-date filter    │
                       └──────────────┬───────────────────────┘
                                      ▼  only authorized chunks exist past here
                       ┌──────────────────────────────────────┐
                       │ CONTEXT GUARDRAIL                    │
                       │ · chunks wrapped in data envelopes    │
                       │ · imperative-instruction neutralizing │
                       │ · explicit instruction hierarchy      │
                       └──────────────┬───────────────────────┘
                                      ▼
                       ┌──────────────────────────────────────┐
                       │ LLM  (no authorization role at all)  │
                       └──────────────┬───────────────────────┘
                                      ▼
                       ┌──────────────────────────────────────┐
                       │ OUTPUT GUARDRAIL                     │
                       │ · citation resolution (T3)            │
                       │ · groundedness scoring                │
                       │ · leak check vs authorized doc set     │
                       │ · authority-claim / directive scrub    │
                       └──────────────────────────────────────┘
```

Prompt-injection defense is layered rather than single-point, because no single
filter is reliable: (a) ingest-time scan flags suspicious documents for admin
review, (b) prompt-time envelope makes injected imperatives structurally
distinguishable from real instructions, (c) output-time leak check verifies the
answer references nothing outside the authorized set. Layers (a) and (b) can be
evaded; layer (c) is the one that must hold, so it compares against the concrete
retrieved-chunk set rather than against patterns.

---

## 7. Scale envelope

Design point: **2M registered users, 200k DAU, 40 QPS sustained / 400 QPS peak,
50M chunks over 2M documents.**

| Concern | Mechanism | Headroom |
|---------|-----------|----------|
| API throughput | Stateless FastAPI, HPA on RPS + p95 latency | linear |
| Connection storm | PgBouncer transaction pooling, 20:1 multiplex | 10k client conns → 500 server |
| Read volume | 2+ streaming replicas; retrieval routed read-only | linear on reads |
| Vector scale | HNSW per classification partition; `iterative_scan=relaxed_order` for correct filtered ANN | 50M chunks tested shape |
| Write volume | Monthly range partitions on `messages`, `audit_events`, `model_invocations`; detach-and-archive | unbounded |
| Ingestion | Horizontally scaled workers, per-source concurrency caps, batched embeddings | linear |
| Cost | Complexity-based model routing + answer cache | ~40% token reduction modeled |
| Hot shard risk | RRF fusion is per-query; no cross-user shared state | n/a |

**Deliberate non-goals for Phase 1:** no multi-region active-active (single region
+ warm standby meets 99.5%), no self-managed vector DB (pgvector until measurement
says otherwise), no fine-tuning (SRS §2.2 excludes it).

---

## 8. Phase 2/3 extension seams

Phase 1 must not need replacing later (§7.1). The seams that exist today:

| Future need | Seam already in place |
|-------------|----------------------|
| Multi-repository federation (Ph2) | `KnowledgeConnector` port; `knowledge_sources.source_type` + per-source config JSONB |
| Multi-source orchestration (Ph2) | `Retriever` port returns a uniform `RetrievedChunk`; a fan-out retriever composes several |
| Agent tool-calling (Ph3) | `rag/orchestrator.py` is a single entry point behind the route; `tool_registry` table and `ToolPort` are defined but unused |
| Delegated authorization (Ph3) | `AuthorizationContext` already carries `on_behalf_of` and `scopes`, unused in Phase 1 |
| Human-in-the-loop approvals (Ph3) | `approval_requests` table defined; no writer in Phase 1 |
| Model swap | `LLMPort` + registry; adding a provider is one adapter file plus one config value |

Each seam is a *type* and, where cheap, a table — not a speculative implementation.
