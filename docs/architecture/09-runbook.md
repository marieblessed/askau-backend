# AskAU — Operational Runbook

Deliverable required by SRS §2.8. Written for the MISD system administrator on call,
not for the engineer who built the system.

**Scope:** this covers operating the AskAU application. Provisioning, container
lifecycle, and infrastructure changes belong to the separate platform tooling and its
own procedures. Everything below is achievable through application configuration,
the administration API, and database access.

---

## 1. Service map

| Service | Purpose | Stateless | Restart safe | Impact if down |
|---|---|---|---|---|
| `web` | Next.js UI | yes | yes | No access; API unaffected |
| `api` | FastAPI edge + RAG pipeline | yes | yes | Total outage |
| `worker` | Ingestion & reconciliation | yes | yes | No new/updated content; queries unaffected |
| | *Postgres, Redis and model serving are externally provisioned — see the platform tooling's own runbook for their lifecycle* | | | |
| `postgres` | Knowledge, ACLs, conversations, audit | **no** | failover | Total outage |
| `pgbouncer` | Connection pooling | yes | yes | API connection exhaustion |
| `redis` | Caches, rate limits, queue | semi | yes | Degraded latency, ingestion pauses |
| LLM endpoint | Generation & embeddings | external | — | No answers; sources still retrievable |

**The one thing to know:** `api` and `worker` are always safe to restart. Nothing
queued is lost — ingestion checkpoints per document, and the queue uses consumer-group
acknowledgement.

---

## 2. Health interpretation

| Endpoint | Green means | Use it for |
|---|---|---|
| `/health/live` | Process is up | Liveness probing — never gate this on dependencies, or a database blip restarts every instance and turns a degradation into an outage |
| `/health/ready` | DB, Redis, retriever, LLM all reachable | Load-balancer membership |
| `/health/deep` | Per-dependency latency and version | First call during an incident |

---

## 3. Incident playbooks

### P1 — Unauthorized retrieval detected

**This is the only page-immediately alert. Treat as a data-disclosure incident.**

1. **Contain.** Set `ASKAU_RETRIEVAL_MAX_CLASSIFICATION=internal` and roll `api`.
   Confidential and restricted tiers stop being queried within ~60 s. Prefer this over
   a full shutdown — it preserves service for the majority while closing exposure.
2. **Scope.** Query `audit_events` for `event_category='retrieval'` joined to
   `citations` over the suspect window; the retrieved document IDs are recorded per
   message, so exposure is enumerable exactly — who saw which document, when.
3. **Diagnose.** Determine which control failed:
   - ACL predicate present in both query arms?
   - `chunks.acl_principals` matches `document_acl` for the affected document?
   - `acl_synced_at` stale → reconciler failure (see P4)
   - RLS policy still attached to all four partitions?
4. **Escalate** to the Security/Compliance Administrator (STK-004) with the exposure
   list. Do not resolve unilaterally; this has reporting obligations.
5. **Fix forward,** add the case to the security evaluation dataset as a permanent
   regression test, then lift containment.

### P2 — LLM provider unavailable

Expected behavior: `502 upstream_unavailable`. The system is *working correctly* when
it refuses to answer — do not attempt to restore service by disabling grounding.

1. Confirm via `/health/deep` whether chat, embeddings, or both are affected.
2. Embeddings only → answer cache still serves; ingestion pauses. Usually ride it out.
3. Chat affected → switch provider: set `ASKAU_LLM_PROVIDER` to the standby
   (`vllm`) and roll `api`. Verify with a smoke evaluation run before announcing.
4. Extended outage → enable `ASKAU_RETRIEVAL_ONLY_MODE=true`. Users get ranked source
   documents with no generated prose. Degraded but genuinely useful, and honest.
5. Never point at a consumer AI endpoint. FR-041 prohibits it and the config
   allow-list will reject it.

### P3 — Latency breach (TTFT p95 > 2.5 s)

Work the pipeline in order; the metric names map to spans in one trace.

| Check | If elevated |
|---|---|
| `cache_hit_rate` collapsed | Redis evicting or restarted — check memory, warm the cache |
| `retrieval_p95` > 120 ms | See P6 |
| `rerank_p95` > 80 ms | Set `ASKAU_RERANKER=noop` temporarily; costs relevance, restores latency |
| `llm_ttft_p95` elevated | Provider-side; route to the fast model tier |
| All flat but total high | Saturation — check pod count, HPA state, PgBouncer wait time |

Descend the degradation ladder (`07-scaling-playbook.md` §Degradation) rather than
improvising. Steps 1–4 are config-only and reversible.

### P4 — ACL reconciliation lag

Directly a security concern: revocations are not yet in effect.

1. `GET /admin/overview` → reconciler lag; or query `MIN(acl_synced_at)` on `chunks`.
2. Under 15 min: normal, within the FR-025 window.
3. 15–60 min: check worker consumer-group lag; scale workers.
4. Over 60 min: run `python -m askau.scripts.reconcile_acl --full --priority=revocations`.
   Revocations are processed before grants, deliberately — a missing grant is an
   inconvenience, a missing revocation is an exposure.
5. Reconciler dead entirely → treat as P1-adjacent. Consider containment per P1 step 1
   until the lag clears.

### P5 — Ingestion failures above 2%

1. `GET /admin/ingestion/runs/{id}` — failures are grouped by `error_code`.
2. Common causes, in order of frequency:

| `error_code` | Cause | Action |
|---|---|---|
| `extract_failed` | Scanned PDF with no text layer | Enable OCR for the source |
| `unsupported_format` | Format outside FR-012 | Expected; confirm with the knowledge owner |
| `validation_failed` | Missing required metadata | Return to the source owner (BR-002) |
| `connector_auth` | Expired repository credential | Rotate in Key Vault |
| `embed_timeout` | Provider throttling | Reduce per-source concurrency; retry |
| `injection_flagged` | High risk score | Route to admin review, do not auto-approve |

3. Retry a single document with `POST /knowledge/documents/{id}/reprocess`; a whole
   source with `.../reindex`. Both are idempotent — content hashing skips unchanged files.

### P6 — Retrieval slow or returning too few results

1. **Check `hnsw.iterative_scan` is set.** This is the most likely cause and the most
   easily missed: without it, a selective ACL filter makes the vector index under-return
   silently. Symptom is *fewer results and lower answer quality*, not an error.
2. `EXPLAIN (ANALYZE)` the hybrid query — confirm partition pruning is occurring and
   the GIN index on `acl_principals` is being used.
3. Check for index bloat after a large ingestion; `REINDEX` per partition, one at a time
   (the others keep serving).
4. Confirm retrieval traffic is on read replicas, and check replication lag.

### P7 — Postgres primary failover

1. Reads continue on replicas; retrieval keeps working. Writes fail 30–60 s.
2. In-flight answers error; clients retry idempotently.
3. After failover: verify PgBouncer reconnected, confirm partition-maintenance jobs
   still scheduled, run a smoke evaluation.
4. Check `messages` and `audit_events` for the current month have partitions — a
   failover during month rollover is the one case where a missing partition surfaces as
   write errors.

---

## 4. Routine operations

| Task | Cadence | Command / location |
|---|---|---|
| Create next month's partitions | Monthly, automated + verified | `ops/sql/partition_maintenance.sql` |
| Archive audit partitions older than 90 d | Monthly | `ops/scripts/archive_partitions.py` |
| Full ACL reconciliation | Nightly | worker schedule |
| Source sync | Per-source `sync_cron` | worker schedule |
| Freshness sweep (flag review-required) | Weekly | worker schedule |
| Full evaluation run | Nightly | CI |
| Load test | Weekly | CI |
| Base image rebuild + rescan | Weekly | CI |
| Secret rotation | Quarterly | Key Vault |
| Restore drill | Quarterly | **restore to a scratch instance and query it** |
| Access review of app roles | Quarterly | Security admin |

The restore drill is listed as a real task because a backup that has never been
restored is a hypothesis. Verify by running the hybrid query against the restored
instance, not merely by confirming the job succeeded.

---

## 5. Configuration levers safe to change at runtime

All are environment variables applied on pod roll; none require a migration or a
rebuild. These are the knobs an on-call engineer may turn without an engineer present.

| Variable | Effect | Risk |
|---|---|---|
| `ASKAU_RERANKER=noop` | Drops ~55 ms, costs relevance | Low |
| `ASKAU_RETRIEVAL_CANDIDATE_K` | Lower = faster, less recall | Low |
| `ASKAU_ANSWER_CACHE_TTL` | Higher = cheaper, staler | Low |
| `ASKAU_LLM_PROVIDER` | Failover between approved providers | Medium — smoke test after |
| `ASKAU_LLM_MODEL` | Route to a faster tier | Medium — affects quality metrics |
| `ASKAU_RATE_LIMIT_PER_MINUTE` | Shed load | Low |
| `ASKAU_RETRIEVAL_ONLY_MODE` | Sources without generation | High visibility — announce it |
| `ASKAU_RETRIEVAL_MAX_CLASSIFICATION` | **Containment** | High — reduces service, closes exposure |
| `ASKAU_MIN_EVIDENCE_SCORE` | **Do not touch during an incident** | Raises fabrication risk |

The last row matters: lowering the evidence threshold makes refusals disappear and will
look like a fix. It is not one — it converts an honest refusal into a possible
fabrication, which is the failure this system exists to prevent.

---

## 6. Escalation

| Condition | Owner | Urgency |
|---|---|---|
| Unauthorized retrieval | Security/Compliance Administrator | Immediate, any hour |
| Total outage | System Administrator → MISD Product Owner | Immediate |
| Fabrication reported by a user | Product Owner + knowledge owner | Same day |
| Ingestion failure on one source | Knowledge Administrator + source business owner | Next business day |
| Cost anomaly | System Administrator → Product Owner | Same day |
| Content accuracy dispute | Knowledge source business owner (BR-002) | Next business day — **not** an engineering issue |

The final row is a boundary worth defending. AskAU is not the authoritative source
(BR-003); when the underlying document is wrong, the fix belongs to the document owner,
not to the platform team.
