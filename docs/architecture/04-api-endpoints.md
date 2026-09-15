# AskAU — API Design (Phase 1)

REST over HTTPS, JSON, OAuth 2.0 / OIDC bearer tokens, OpenAPI 3.1 (SRS §3.4).
Base path `/api/v1`. The SRS lists *indicative* endpoints; this is the finalized
Phase 1 contract.

## Cross-cutting conventions

| Concern | Decision |
|---|---|
| Versioning | URI-versioned (`/api/v1`). Additive changes only within a major version. |
| Auth | `Authorization: Bearer <Entra access token>`; audience-validated against the AskAU app registration. Browser holds an httpOnly cookie; the Next.js server exchanges it for the bearer. |
| Errors | RFC 9457 `application/problem+json`. Messages are non-revealing by design: an unauthorized document returns `404 not_found`, never `403`, so existence is not leaked (§8 error handling). |
| Correlation | `X-Correlation-Id` accepted or generated; echoed on every response; present on every log line, span, and audit row. |
| Idempotency | `Idempotency-Key` required on `POST /messages` and `POST /sources/{id}/sync`. Replays return the original result. |
| Pagination | Cursor-based (`?cursor=&limit=`). Offset pagination is not offered — it degrades at depth and there is no requirement for random access. |
| Rate limits | Per-user token bucket; `429` with `Retry-After`. Defaults: 30 questions/min, 300/hour. |
| Concurrency | `ETag` / `If-Match` on admin mutations. |
| Content | `application/json`; `text/event-stream` for the streaming answer. |

## Authentication

| Method | Path | Purpose | Notes |
|---|---|---|---|
| `GET` | `/auth/config` | Public OIDC discovery metadata for the SPA | authority, client id, scopes |
| `POST` | `/auth/session` | Exchange the OIDC code for an AskAU session; resolve principals; upsert user | FR-001, FR-001a. Audits `auth.login`. |
| `GET` | `/auth/me` | Current identity, app roles, department, authorized-classification summary | Never returns the raw principal set |
| `POST` | `/auth/session/refresh` | Rotate session | |
| `DELETE` | `/auth/session` | Secure logout; revoke session; invalidate authz cache | FR-001a |

`GET /auth/me` deliberately returns a *summary* (`max_classification`, roles) rather
than the principal ID list. The client never needs the ACL set, and shipping it would
turn a UI bug into an information-disclosure bug.

## Conversations

| Method | Path | Purpose | FR |
|---|---|---|---|
| `POST` | `/conversations` | Start a new conversation | FR-005 |
| `GET` | `/conversations` | List *own* conversations, cursor-paginated | FR-006 |
| `GET` | `/conversations/{id}` | Conversation with messages + citations | FR-006 |
| `PATCH` | `/conversations/{id}` | Rename / archive | |
| `DELETE` | `/conversations/{id}` | Delete own conversation (audited, not silent) | §6.5 |

Ownership is enforced in the query (`WHERE user_id = :caller`), so another user's
conversation ID returns `404`. FR-006's "not visible to other users" is a predicate,
not a check that could be forgotten.

## Asking a question — the primary endpoint

```
POST /api/v1/conversations/{id}/messages
Content-Type: application/json
Accept: text/event-stream          ← streaming (default for the UI)
Idempotency-Key: 01J8Z...

{ "content": "What is the procedure for requesting official travel?",
  "options": { "include_historical": false, "department_filter": null } }
```

### SSE event protocol

Events are ordered and each is independently meaningful, so the UI can render
progress honestly instead of showing a spinner and hoping.

```
event: accepted        {"message_id","correlation_id"}
event: stage           {"stage":"understanding"}                     ← optional UX signal
event: stage           {"stage":"retrieving"}
event: sources         {"citations":[{"marker":1,"document_id","title","section_ref",
                                     "page_from","page_to","source_name",
                                     "version_label","effective_from",
                                     "can_open":true}]}              ← BEFORE tokens
event: token           {"text":"Official travel requests are "}
event: token           {"text":"submitted through ..."}
event: conflict        {"summary":"...","documents":[...]}            ← FR-035, when applicable
event: done            {"answer_state":"grounded","groundedness":0.94,
                        "ttft_ms":612,"total_ms":4180,"tokens":{...},
                        "citations_verified":true}
event: error           {"type":"upstream_unavailable","detail":"..."}
```

Two deliberate protocol choices:

- **`sources` is emitted before the first `token`.** Citations come from retrieval,
  which has already completed. Showing the user which documents will be used *before*
  the prose arrives builds warranted trust and makes the wait feel purposeful.
- **`done` carries `answer_state` and `groundedness`.** The client renders a different
  banner for `insufficient_evidence`, `conflict`, and `out_of_scope`. Refusals are a
  designed outcome (FR-028), so they get a first-class representation on the wire
  rather than being smuggled into prose.

Non-streaming callers (`Accept: application/json`) get the assembled response with
the same fields — required for the future agent layer and for the eval harness.

| Method | Path | Purpose | FR |
|---|---|---|---|
| `POST` | `/conversations/{id}/messages` | Ask; SSE or JSON | FR-003…FR-035 |
| `GET` | `/conversations/{id}/messages` | Message history | FR-006 |
| `GET` | `/messages/{id}` | Single message + verified citations | FR-029 |
| `GET` | `/messages/{id}/citations` | Citation detail with supporting quotes | FR-029, FR-030 |
| `POST` | `/messages/{id}/stop` | Cancel in-flight generation | |

## Citations & document access

| Method | Path | Purpose | FR |
|---|---|---|---|
| `GET` | `/documents/{id}` | Document metadata, if authorized | FR-031 |
| `GET` | `/documents/{id}/open` | `302` to a short-lived authoritative-source URL | FR-029, FR-031, BR-004 |
| `GET` | `/documents/{id}/preview?chunk_id=` | Extracted text of the cited span | FR-029 |

`/open` re-checks authorization at click time rather than trusting the `can_open`
flag the client received earlier — permissions may have changed in between (FR-025),
and every open is audited as `document.opened`.

Note the asymmetry this enables: a chunk may legitimately ground an answer while the
user cannot open the original (FR-031 alternative flow). The API expresses that as
`can_open: false` with citation metadata still present.

## Feedback

| Method | Path | Purpose | FR |
|---|---|---|---|
| `POST` | `/messages/{id}/feedback` | `helpful` / `not_helpful` + optional reason code | FR-043, FR-044 |
| `DELETE` | `/messages/{id}/feedback` | Withdraw | |

## Knowledge management (`knowledge_admin`)

| Method | Path | Purpose | FR |
|---|---|---|---|
| `GET` | `/knowledge/sources` | List sources with status + freshness | FR-046 |
| `POST` | `/knowledge/sources` | Register a source (name, type, owner, dept, classification, location, rules, cron) | FR-011 |
| `GET` | `/knowledge/sources/{id}` | Detail incl. last run summary | |
| `PATCH` | `/knowledge/sources/{id}` | Update config / status | |
| `POST` | `/knowledge/sources/{id}/approve` | Approve for ingestion | BR-001 |
| `POST` | `/knowledge/sources/{id}/sync` | Trigger sync → `202` + `run_id` | FR-049 |
| `POST` | `/knowledge/sources/{id}/reindex` | Full re-chunk + re-embed | FR-049 |
| `POST` | `/knowledge/sources/{id}/test-connection` | Validate connector before activating | FR-013 |
| `GET` | `/knowledge/documents` | Filter by source, lifecycle, status, classification, freshness | FR-046, FR-047 |
| `GET` | `/knowledge/documents/{id}` | Detail incl. chunk count, versions, injection risk | FR-050 |
| `PATCH` | `/knowledge/documents/{id}` | Correct metadata, classification, lifecycle | FR-013 |
| `POST` | `/knowledge/documents/{id}/reprocess` | Retry a single failed document | FR-049, FR-050 |
| `GET` | `/knowledge/documents/{id}/versions` | Version chain | FR-017 |
| `POST` | `/knowledge/documents/upload` | Direct upload for the `manual` source type | FR-012 |

Long-running work always returns `202 Accepted` with a `run_id` and a `Location`
header. Ingestion is minutes-scale; a synchronous API would be a lie.

## Administration & operations

| Method | Path | Purpose | FR |
|---|---|---|---|
| `GET` | `/admin/overview` | Dashboard aggregate: doc counts, active sources, ingestion status/failures, query volume, feedback, health | FR-048 |
| `GET` | `/admin/ingestion/runs` | Run history | FR-048 |
| `GET` | `/admin/ingestion/runs/{id}` | Per-document task detail + errors | FR-050 |
| `POST` | `/admin/ingestion/runs/{id}/cancel` | Cancel | |
| `GET` | `/admin/metrics` | Latency percentiles, error rate, search and model latency, refusal rate | FR-053 |
| `GET` | `/admin/usage` | Token consumption by operation, model, and directorate; cache-hit share | FR-053, §7.4 |
| `GET` | `/admin/quality` | Groundedness, citation accuracy, feedback trend, hallucination indicators | FR-054 |
| `GET` | `/admin/feedback` | Negative-feedback triage queue | FR-044 |
| `PATCH` | `/admin/feedback/{id}` | Triage + resolution | |
| `GET` | `/admin/config` | Effective non-secret configuration | |
| `PATCH` | `/admin/config` | Update retrieval/generation params (audited) | |

## Security & audit (`security_admin`)

| Method | Path | Purpose | FR |
|---|---|---|---|
| `GET` | `/security/audit-events` | Filter by actor, category, type, outcome, time, correlation ID | FR-051, FR-052 |
| `GET` | `/security/audit-events/export` | Signed NDJSON export for SIEM | FR-051 |
| `GET` | `/security/access-denials` | Unauthorized-access attempts | FR-051 |
| `GET` | `/security/ai-safety-events` | Injection detections, output-scan blocks | FR-036, FR-038 |
| `GET` | `/security/retention` · `PATCH` | Retention policy | §6.5 |

Audit is read-and-export only. There is no endpoint that mutates an audit row, in any
role — BR-008 means the log must be trustworthy to a reviewer who does not trust the
application.

## Evaluation (`system_admin`, CI service principal)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/evaluation/datasets` · `POST` | Manage eval sets (§7.5) |
| `POST` | `/evaluation/runs` | Execute a run → `202` |
| `GET` | `/evaluation/runs/{id}` | Metrics + per-question results |
| `GET` | `/evaluation/runs/compare?a=&b=` | Regression comparison between commits |

CI calls this and fails the build when groundedness or citation accuracy regresses
below the §6.8 thresholds. Quality targets in a document are aspirations; quality
targets in a pipeline gate are requirements.

## Health & platform

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/live` | Liveness — process only, no dependencies |
| `GET` | `/health/ready` | Readiness — DB, Redis, LLM, retriever reachable |
| `GET` | `/health/deep` | Per-dependency latency + version (admin) |
| `GET` | `/openapi.json` · `/docs` | Contract + Swagger UI (§2.8) |
| `GET` | `/metrics` | Prometheus (cluster-internal only) |

## Error taxonomy

| HTTP | `type` | When |
|---|---|---|
| 400 | `invalid_request` | Schema/validation failure |
| 401 | `unauthenticated` | Missing/expired/invalid token |
| 403 | `insufficient_role` | Authenticated but lacks the *admin role* (not document ACL) |
| 404 | `not_found` | Absent **or** not authorized — indistinguishable on purpose |
| 409 | `conflict` | ETag mismatch, duplicate source name |
| 413 | `payload_too_large` | Upload over limit |
| 422 | `unprocessable_document` | Ingestion validation failure (FR-013) |
| 429 | `rate_limited` | Bucket exhausted |
| 499 | `client_closed` | Client aborted a stream |
| 500 | `internal_error` | Unexpected; correlation ID returned, no stack trace |
| 502 | `upstream_unavailable` | LLM/search/repository down — **fails safe, never falls back to ungrounded generation** (§2.3 degraded mode) |
| 503 | `not_ready` | Startup or dependency loss |

The two entries that matter most: `404` for unauthorized (existence is itself
sensitive), and `502` rather than a degraded answer. A "helpful" fallback that answers
from model knowledge when retrieval is down would violate FR-033 and BR-007 — so
degraded mode returns an error, by design.
