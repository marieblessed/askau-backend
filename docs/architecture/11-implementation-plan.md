# AskAU — Phase 1 Implementation Plan

Maps this architecture onto the delivery structure in *Ask AU Project Document &
Nine-Month Work Plan* (Months 1–3, Phase 1: Enterprise Knowledge Platform), using the
team roles defined there.

Month 1 (initiation, discovery, SRS) is complete — the SRS exists and this
architecture set is its Solution Architecture deliverable. What follows is Months 2–3.

---

## Build order, and why

The sequence is chosen so that the highest-risk, hardest-to-retrofit properties are
proven first. Two constraints drive it:

1. **The authorization boundary must exist before anything retrieves.** Retrofitting
   permission-aware retrieval onto a working RAG pipeline is a rewrite, because the ACL
   shape dictates the schema, the index strategy, and the query. Built first, it costs
   nothing extra.
2. **Security tests precede the pipeline that uses the retriever.** If the isolation
   tests are written after the pipeline works, there is pressure to make them pass
   rather than to make them true.

A third constraint applies to tooling rather than to code: **build the application
first, automate it second.** Every quality gate is written as a local command from day
one, so CI later wraps commands that already work instead of encoding logic that exists
nowhere else. See `02-file-structure.md` §5.

| Order | Work | Blocks | Owner (work-plan role) |
|---|---|---|---|
| 0 | Local dev loop: `make setup`, `make check`, seeded identities and corpus | developer velocity | Solution Architect |
| 1 | Schema + migrations + partition/index strategy | everything | Data Engineer + Solution Architect |
| 2 | Identity, principal resolution, authorization context | all retrieval | Solution Architect + Security Officer |
| 3 | Hybrid retriever **+ ACL isolation test suite** | RAG pipeline | Solution Architect + QA |
| 4 | Ingestion pipeline (extract → chunk → embed → index → ACL) | real content | Data Engineer |
| 5 | RAG orchestration: evidence gate, context, guardrails, citations | answers | Solution Architect |
| 6 | API surface + streaming protocol | UI | Backend Developer |
| 7 | Chat UI + citation drawer | pilot | Frontend Developer + UX |
| 8 | Admin console | operations | Frontend Developer |
| 9 | Evaluation harness + dataset | acceptance | QA + Business Analyst |
| 10 | Observability, audit dashboards, load testing | hardening | Solution Architect + Security Officer |

Items 4 and 9 have a dependency the work plan does not surface: **the evaluation
dataset needs approved documents to exist**, and document approval is a business
process owned by knowledge owners, not an engineering task. Starting document
cleansing and classification in parallel with item 1 is what keeps item 9 off the
critical path.

---

## Month 2 — Platform Foundation

Work-plan deliverables: *Development Environment · Vector Database · Authentication
Platform · Knowledge Processing Pipeline · Initial User Interface*

| Sprint | Focus | Exit criteria |
|---|---|---|
| 2.1 | Both repositories bootstrapped: `make setup` / `make check`, pre-commit hooks, contract export, import contracts, connection to the externally-provisioned Postgres and Redis | `make check` passes in both repos; the API starts against external services with `echo`/`hash` providers and no cloud credentials. **No CI yet — deferred to the 2.4 gate** |
| 2.1 | Schema + migrations; four chunk partitions with HNSW/GIN indexes; RLS policies attached | `EXPLAIN` on the hybrid query shows partition pruning and GIN usage on a synthetic 1M-chunk corpus |
| 2.2 | Entra integration: OIDC/PKCE, JWKS validation, principal flattening, app roles, session lifecycle | A real AUC identity signs in; groups resolve to principals; logout revokes |
| 2.2 | Authorization context + Redis cache + `acl_version` invalidation | Membership change invalidates the cached context within one request |
| 2.3 | Ingestion: connectors, extraction (incl. OCR), structure-aware chunking, embedding, indexing | A SharePoint library ingests end to end with page-accurate anchors |
| 2.3 | ACL materialization + reconciler | Revoking a group removes retrieval access within the sync window |
| 2.4 | Hybrid retriever + **security test suite** | TC-SEC-003/004/004b/005 pass, including with the ACL predicate deliberately removed |
| 2.4 | Chat shell UI against a stub answer service | Streaming renders; citation drawer opens; refusal state renders |
| 2.4 | **CI pipelines added**, wrapping the existing `make check` / `npm run check` | Both pipelines green; merge protection enabled on `main` |

**Month 2 gate:** an authenticated user can retrieve permission-filtered chunks from
genuinely ingested AUC documents, the isolation suite passes with the primary control
disabled, and CI enforces both from that point onward. No generation yet — that is deliberate. Retrieval correctness is
provable; answer quality is not, and proving the provable part first is what makes
Month 3 measurable.

---

## Month 3 — RAG Platform Development (MVP)

Work-plan deliverables: *Ask AU Knowledge Assistant (MVP) · Secure Knowledge Repository
· User Portal · Retrieval Engine · UAT Plan*

| Sprint | Focus | Exit criteria |
|---|---|---|
| 3.1 | Model abstraction + adapters; complexity routing; usage ledger | Provider swaps by config alone; import contracts pass; `ttft_ms`/`total_ms` recorded on every answer so model size can be settled with evidence |
| 3.1 | Evidence gate, context assembly, prompt v1, streaming generation | Grounded answers with citations; refusal path short-circuits before the model |
| 3.2 | Citation validation, groundedness scoring, conflict detection | Fabricated markers stripped; conflicting sources surfaced with dates |
| 3.2 | Guardrails: input scan, context shield, output scan | TC-SEC-007/009 pass |
| 3.3 | Full chat UI, feedback capture, admin console, audit dashboards | FR-043/044/048/051 demonstrable |
| 3.3 | Evaluation harness + 300-question dataset + CI gates | Nightly run reports all §6.8 metrics |
| 3.4 | Load testing, tuning, runbook validation, UAT preparation | NFR-002a/b met at 5× expected pilot load |
| 3.4 | Security testing, pilot handover | Security sign-off; release handed to the platform team for pilot rollout |

**Month 3 gate (production acceptance, SRS §7.5):** every blocking metric in
`08-evaluation-harness.md` met, security review signed off by STK-004, runbook
validated by an administrator who did not build the system.

---

## Risk register — engineering additions

The work plan and SRS §7.3 cover the programme risks. These are the ones this
architecture specifically introduces or discovers, with the mitigation already designed in.

| Risk | L | I | Mitigation | Owner |
|---|---|---|---|---|
| **Document quality is worse than assumed** — scanned PDFs, no metadata, inconsistent versioning | H | H | Validation gate rejects early with actionable errors; OCR path; freshness sweeps. Start cleansing in Month 2, not Month 3 | Data Engineer |
| **Approved corpus arrives late**, blocking evaluation | H | H | Synthetic corpus for development; dataset construction can start on any approved subset | Business Analyst |
| ACL reconciliation lag becomes an exposure | M | H | Revocations prioritized over grants; lag metric alerted; containment lever in the runbook | Security Officer |
| pgvector below 0.8 in the provided database → silent recall loss | M | H | Asserted at application startup, so it fails loudly rather than degrading quietly; `hybrid_lift` metric would also expose it | Solution Architect + platform team |
| LLM-as-judge inflates quality scores | M | M | Cross-family judge, narrow task, human calibration set, κ reporting | QA |
| Classification scheme differs from the four SRS tiers | M | M | `classification` is an enum with partition mapping; a fifth tier is a migration, not a redesign | Security Officer |
| Entra group structure does not express document permissions | M | H | `document_acl` accepts `askau_override` as a source of truth for cases the directory cannot express | Solution Architect |
| Latency target missed on the reranker | L | M | Reranker is a port with a noop adapter; disabling it is a config change | Solution Architect |

The first two are rated High/High and are **not** engineering risks — they are content
and approval risks that only business stakeholders can retire. They are the most likely
cause of a Month 3 slip, and the plan above is arranged so engineering can progress
against a synthetic corpus while they are resolved.

---

## Definition of done, per requirement

A requirement is complete when all five hold. Anything less is progress, not delivery.

1. Code merged, with the import contracts passing.
2. Unit and integration tests present and passing.
3. The mapped test ID in `10-traceability.md` implemented and green.
4. Audit events emitted where the requirement is security-relevant.
5. Documentation updated — the user guide, admin guide, or runbook entry that §2.8 requires.

## Pilot readiness checklist

| | Item |
|---|---|
| ☐ | Approved knowledge sources registered with named business owners (BR-002) |
| ☐ | Classification applied to every ingested document |
| ☐ | Ingestion success ≥ 98% on the pilot corpus |
| ☐ | All blocking evaluation metrics met |
| ☐ | Security review signed off (STK-004) |
| ☐ | Load test passed at 5× expected pilot concurrency |
| ☐ | Runbook validated by an administrator who did not build the system |
| ☐ | Backup restore drill completed and verified by query |
| ☐ | User guide, admin guide, API docs published (§2.8) |
| ☐ | Feedback triage process staffed and owned |
| ☐ | Open items in `10-traceability.md` resolved or formally accepted |
