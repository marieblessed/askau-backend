# AskAU — Architecture Documentation

Phase 1: enterprise permission-aware RAG knowledge assistant for the African Union
Commission. All documents trace to `AskAU_SRS_v1.0` (MISD, 08-Aug-2026) and to the
*Nine-Month Work Plan* (Phase 1, Months 1–3).

## Repositories

| Repository | Contents |
|---|---|
| **`api`** | RAG pipeline, retrieval, ingestion, API, workers — Python 3.12 / FastAPI |
| **`web`** | Chat interface and administration console — TypeScript / Next.js |

Both hosted on GitLab, coupled only by a committed OpenAPI contract; see
[02-file-structure.md](02-file-structure.md) §3 for how that contract is kept honest.

**Not in scope for either repository:** containers, orchestration, infrastructure
provisioning, deployment, and the capacity planning that goes with them. PostgreSQL,
Redis, and model serving are provisioned and run by a separate team; both applications
reach them through environment configuration and assume nothing about how they are
hosted. The focus here is making the application work.

| # | Document | Answers |
|---|---|---|
| 00 | [Technology Stack](00-tech-stack.md) | What we build with, why it beat the alternative, and the exit path for each choice |
| 01 | [System Architecture](01-system-architecture.md) | Layers, topology, both hot paths, the decisions that make it fast, scale envelope, Phase 2/3 seams |
| 02 | [File Structure](02-file-structure.md) | The monorepo layout, each component's internals, the shared contract, the local dev workflow, and the lint rules that keep boundaries real |
| 03 | [Database Schema](03-database-schema.md) | Full DDL, denormalization rationale, retention, and the hot query in full |
| 04 | [API Design](04-api-endpoints.md) | Every endpoint, the streaming protocol, the error taxonomy |
| 05 | [UI Architecture](05-ui-architecture.md) | Routes, components, state, performance budget, accessibility, admin console |
| 06 | [Security & Threat Model](06-security-model.md) | Assets, trust boundaries, ten threats with controls, accepted risks, verification plan |
| 07 | [Scaling Playbook](07-scaling-playbook.md) | Capacity model, latency budget, degradation ladder, failure modes, SLOs |
| 08 | [Evaluation Harness](08-evaluation-harness.md) | Dataset design, metric definitions, the judge problem, CI gates |
| 09 | [Operational Runbook](09-runbook.md) | Health interpretation, seven incident playbooks, routine ops, safe runtime levers |
| 10 | [Traceability Matrix](10-traceability.md) | Every FR/NFR/BR → design element → test ID; open items for AUC |
| 11 | [Implementation Plan](11-implementation-plan.md) | Build order and why; Months 2–3 sprints and gates; engineering risk register |
| 12 | [Data Model & Flow Diagrams](12-data-model-diagrams.md) | ERDs for all three data domains, query and ingestion flows, the authorization contrast, answer-state machine — closes SRS Appendix C |
| 13 | [Test Strategy & UAT Plan](13-test-strategy.md) | Coverage by layer, fixtures, 24 E2E journeys, the 40-scenario UAT plan and its acceptance criteria |
| 14 | [CI & Code Quality](14-ci-and-code-quality.md) | Branching, review rules, both GitLab CI pipelines, tier-independent scanning — **designed, deferred until the Month 2 gate** |
| 15 | [Decision Records](15-decision-records.md) | Fifteen ADRs — what was decided, what it costs, and what would make us revisit it |
| 16 | [Host & Service Dependencies](16-host-dependencies.md) | What runs outside this repo — pgvector, Redis, OCR — why nothing is installed on the app host, and how each absence is made visible |

## Reading order

| Role | Path |
|---|---|
| Architect / reviewer | 01 → 12 → 03 → 06 → 07 → 15 |
| Backend engineer | 01 → 02 → 03 → 04 → 12 |
| Frontend engineer | 05 → 04 → 02 §3 (the contract) |
| Security reviewer | 06 → 01 §6 → 03 §1–3 → 10 |
| QA | 13 → 08 → 10 → 04 |
| Project lead / sponsor | 01 §1 → 11 → 13 §5 → 10 (open items) |
| Platform / operations | 09 → 07 → 14 §10 |

## The five load-bearing decisions

1. **Authorization is a SQL predicate, never a model judgment.** `acl_principals && $principals`
   in both retrieval arms, with row-level security as an independent second lock. (BR-005, BR-006)
2. **Access lists, lifecycle, and dates are denormalized onto the chunk row.** Trades an
   ACL reconciliation job — which FR-025 explicitly permits — for zero joins on the hot path.
3. **Hybrid retrieval is one SQL statement with in-database rank fusion.** One round trip,
   no score-scale tuning, no application-side merge.
4. **Refusal is a first-class designed outcome.** `answer_state` is a column, a wire field,
   and its own UI component — so honesty is measurable, not merely prompted.
5. **Every vendor sits behind a port, enforced in CI.** An import contract fails the build if
   orchestration reaches for an SDK, which is the only way replaceability survives delivery pressure.

## Status

Design complete for Phase 1. Implementation not started. CI is designed but deliberately
deferred — every gate exists as a local command first, so the pipeline later wraps
commands that already work ([02 §5](02-file-structure.md)). Items requiring an AUC
business decision before requirements baselining are listed at the end of
[10-traceability.md](10-traceability.md).
