# AskAU — Scaling Playbook

> **Scope:** this states what the architecture permits and what the application must do
> to stay inside its latency requirements. Provisioning the capacity to run it — servers,
> GPUs, storage — belongs to the deployment team. The figures here exist so that team
> knows what it is being asked for.

Design point: **2M registered users · 200k DAU · 40 QPS sustained · 400 QPS peak ·
2M documents · 50M chunks.** The SRS leaves volumes TBD (§4.2); this is the envelope
the design is built for, so growth is a capacity exercise rather than a redesign.

## Capacity model

| Quantity | Derivation | Value |
|---|---|---|
| Questions/day | 200k DAU × 8 questions | 1.6M |
| Sustained QPS | 1.6M / 86400 × 2 (diurnal peaking) | ~37 |
| Peak QPS | 10× sustained, 15-min window | ~400 |
| Chunk storage | 50M × (700 B text + 1024×4 B vector + metadata) | ~280 GB |
| HNSW index (RAM) | 50M × 1024 × 4 B × 1.5 overhead | ~300 GB across partitions |
| Answer-cache hit rate | measured on institutional Q&A corpora | 25–35% |

The HNSW figure is the binding constraint and it drives two decisions: 1024-dimension
embeddings instead of 1536 (~33% saving) and classification partitioning, so a typical
query touches only the `public` + `internal` graphs — roughly 60–70% of the index in
practice, not all of it.

## Tier-by-tier scaling

| Tier | Scale mechanism | Trigger | Ceiling before redesign |
|---|---|---|---|
| `web` (Next.js) | Horizontal replicas on CPU + RPS | CPU > 60% | Effectively none (CDN-fronted) |
| `api` (FastAPI) | Horizontal replicas on RPS + p95 latency | p95 > 2 s or 150 RPS/instance | Linear; stateless |
| PgBouncer | Replica set, 20:1 multiplex | conns > 70% of pool | ~10k client connections |
| Postgres reads | Add streaming replicas; retrieval is read-only | replica CPU > 65% | ~6 replicas before replication lag matters |
| Postgres writes | Vertical + partition pruning; messages/audit are append-only | write IOPS > 70% | Single primary to ~5k writes/s; then shard `messages` by `user_id` |
| pgvector | Partition-level HNSW; add read replicas | retrieval p95 > 150 ms | ~10⁸ chunks; beyond that, move to a dedicated vector store via the `Retriever` port |
| Redis | Cluster mode, hash-slot sharded | memory > 70% | Linear |
| LLM | vLLM replica count | queue depth > 5 | GPU count; one 8B replica covers the modelled peak |
| Workers | Scale consumer group; per-source concurrency caps | stream lag > 1000 | Linear |

## Latency budget (p95, steady state)

```
 auth + authz (Redis hit)         5 ms   ▏
 guardrail input scan             2 ms   ▏
 query understanding             20 ms   ▎
 answer-cache probe               3 ms   ▏
 query embedding (cache miss)    35 ms   ▍
 hybrid retrieval (one SQL)      45 ms   ▌
 rerank top-40 → top-8           55 ms   ▋
 evidence gate + context          8 ms   ▏
 ───────────────────────────────────────
 pre-LLM total                  173 ms
 LLM TTFT (8B, 6.5k prefill)    270 ms   ██
 ───────────────────────────────────────
 FIRST TOKEN                    443 ms          budget: 3000 ms  (NFR-002a) ✓
 LLM completion (400 tok)      4210 ms   ████████████████████████████
 ───────────────────────────────────────
 FULL ANSWER                   4653 ms          budget: 10000 ms (NFR-002b) ✓
```

The controllable 173 ms leaves ~2.5 s of headroom before NFR-002a is at risk — the
margin that absorbs a cold cache, a replica failover, or a slow day.

**The completion line is the one under pressure, and it is set by model size.** At 8B
there is 5.3 s of slack against NFR-002b; the same answer length at 32B breaches it.
See `00-tech-stack.md` — that is why model selection is treated as a latency decision
rather than a quality-only one.

## Load-shedding and degradation ladder

Applied in order as load rises, so quality degrades before availability does:

1. Serve from the answer cache more aggressively (TTL 15 min → 60 min).
2. Skip reranking when the RRF score margin between rank 1 and rank 9 exceeds a
   threshold — the ordering would not change anyway.
3. Route low-complexity questions to the smaller/faster model (the router already does
   this to protect latency; under load the threshold moves).
4. Reduce candidate `k` from 60 → 30.
5. Queue with a visible position indicator rather than failing.
6. Shed at the edge with `429` + `Retry-After`.

**Never shed by answering ungrounded.** Retrieval failure returns `502` (§2.3 degraded
mode), because an unsourced answer violates FR-033/BR-007 and costs more institutional
trust than an error does.

## Failure modes and blast radius

| Failure | Behavior | Blast radius |
|---|---|---|
| One `api` instance dies | LB removes it; in-flight streams drop, client retries idempotently | seconds, one request |
| Postgres primary failover | Reads continue on replicas; writes pause 30–60 s | writes only |
| Redis loss | Caches cold, authz falls back to DB; ~200 ms slower per request | degraded latency, correct results |
| LLM provider outage | `502` fail-safe; retrieval-only "here are the relevant documents" fallback offered | no answers, sources still usable |
| Vector index corruption | Detected by the eval gate; rebuild per partition while others serve | one classification tier |
| Bad ACL reconciliation | RLS is the second lock; eval `must_not_retrieve` assertions catch it in CI | contained by design |
| Poisoned document ingested | Ingest-time risk score → admin review; context shield + output scan at runtime | one document, flagged |

## Observability SLOs

| SLO | Target | Alert |
|---|---|---|
| Availability | 99.5% service hours (NFR-001) | error rate > 1% for 5 min |
| TTFT p95 | < 3 s | > 2.5 s for 10 min |
| Full answer p95 | < 10 s | > 8 s for 10 min |
| Retrieval p95 | < 150 ms | > 120 ms for 10 min |
| Unauthorized retrieval | 0 | **any occurrence — page immediately** |
| Groundedness (rolling) | ≥ 90% | < 92% |
| Citation accuracy | ≥ 95% | < 96% |
| Ingestion success | ≥ 98% | < 98% over a run |
| Refusal rate | monitored, not targeted | ±50% week-over-week change |

Refusal rate has no target on purpose. Driving it down invites fabrication; driving it
up invites uselessness. A sudden *move* in either direction is the real signal — it
usually means a retrieval regression or a corpus change, and it is the earliest warning
available for both.

Alert thresholds sit *inside* the SLO (92% vs 90%) so there is time to act before the
acceptance criterion is actually breached.
