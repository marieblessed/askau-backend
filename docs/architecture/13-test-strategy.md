# AskAU — Test Strategy & UAT Plan

Delivers the *UAT Plan* named in the work plan (Month 3) and the verification approach
behind SRS Appendix B. Complements `08-evaluation-harness.md`, which covers AI-specific
quality measurement; this document covers everything else.

---

## 1. Test pyramid, and where it is deliberately inverted

```
                    ╱╲          UAT — 40 scenarios, real staff, real corpus
                   ╱  ╲
                  ╱ E2E╲        24 Playwright journeys
                 ╱──────╲
                ╱ INTEG. ╲      ~120 tests, real Postgres, real pgvector
               ╱──────────╲
              ╱   UNIT     ╲    ~400 tests, pure functions, no I/O
             ╱──────────────╲
        ╱▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓╲  SECURITY — runs in full at every level
```

The security suite is drawn as a base rather than a tier because it does not sample.
Its target is zero, and a suite with a zero target cannot be sampled without becoming
meaningless.

The pyramid is also inverted in one specific place: **retrieval is tested primarily at
the integration level, not the unit level.** Mocking pgvector would test a mock. The ACL
predicate, partition pruning, RRF fusion, and HNSW recall behavior only exist in a real
database, so those tests run against a containerized Postgres with the real extension.

---

## 2. Coverage by layer

| Layer | Level | What is actually asserted |
|---|---|---|
| `domain/` | Unit | Type invariants, `acl_signature()` stability, state transitions |
| `retrieval/fusion.py` | Unit | RRF arithmetic, tie-breaks, rank stability |
| `rag/citations.py` | Unit | Marker parsing, unresolvable markers stripped, quote substring verification |
| `rag/evidence.py` | Unit | Gate thresholds, refusal short-circuit |
| `rag/guardrails/` | Unit + security | Envelope integrity, injection heuristics, output leak detection |
| `ingestion/chunking.py` | Unit | Heading paths, page anchors, boundary preservation, token counts |
| `retrieval/adapters/pgvector_hybrid.py` | **Integration** | ACL filtering, partition pruning, version predicates, recall@k |
| `db/` migrations | Integration | Up/down reversibility, constraint enforcement, partition creation |
| `core/authz.py` | Integration | Principal resolution, cache invalidation on `acl_version` bump |
| `api/` routes | Integration | Contract conformance, error taxonomy, SSE event ordering |
| `contracts/` | Contract | OpenAPI snapshot; breaking-change detection; consumer-declared response shapes |
| `web` | E2E | Journeys below, run against a live API |
| Whole system | UAT | Scenarios in §5 |

**Coverage thresholds:** 80% line coverage overall; **90% on `rag/` and `retrieval/`**.
The higher bar applies to exactly the two packages where a defect becomes either a
disclosure or a fabrication.

---

### Testing across the repository boundary

The split means no compiler checks the API contract, so three test layers replace it.

| Layer | Repo | Catches |
|---|---|---|
| OpenAPI snapshot diff | `api` | Any undeclared change to the published contract |
| Consumer-driven contract tests | `api` | A field the API treats as optional that the UI treats as guaranteed |
| Codegen drift check | `web` | Hand-edited generated types papering over a real mismatch |
| E2E against a live API | `web` | Everything the first three miss — behavior, not shape |

The second is the one that earns its keep. Shape agreement is easy to verify and rarely
where the bug is; *optionality* disagreement is subtle, passes every type check on both
sides, and surfaces as a runtime crash on the one response where the field is absent.

## 3. Test data strategy

Production data is never used in development (SRS §2.6), which means the fixture corpus
must be good enough to be worth testing against.

| Fixture set | Contents | Used by |
|---|---|---|
| `synthetic-core` | 200 generated AUC-shaped policy documents across all four classifications, with realistic heading structure, version chains, and effective dates | Integration, E2E |
| `synthetic-adversarial` | 25 documents containing embedded instructions, conflicting policy pairs, expired-but-indexed content, and a scanned-image PDF | Security, evaluation |
| `synthetic-multilingual` | The same 10 policies in English, French and Arabic, plus one non-Latin-script document | Retrieval, E2E |
| `synthetic-identities` | 12 users across 5 departments with overlapping group membership, including one user whose access is revoked mid-suite | Security |
| `anonymized-uat` | Real AUC documents, cleared by knowledge owners, for UAT only | UAT |

The adversarial fixtures matter more than the volume. A conflicting policy pair and a
document containing "ignore previous instructions" are not edge cases here — they are
the FR-035 and FR-036 requirements, and without fixtures they cannot be tested at all.

---

## 4. End-to-end journeys

| # | Journey | Asserts |
|---|---|---|
| E1 | Sign in → ask → grounded answer with citations | FR-001, FR-003, FR-029 |
| E2 | Follow-up question resolving "this" from context | FR-004 |
| E3 | Click citation → drawer → open authoritative source | FR-029, FR-031, BR-004 |
| E4 | Cited source the user cannot open shows the unavailable note | FR-031 alt flow |
| E5 | Unanswerable question → insufficient-evidence state renders | FR-028 |
| E6 | Out-of-scope question declines | FR-009 |
| E7 | Ambiguous question asks for clarification | FR-008 |
| E8 | Conflicting sources → conflict notice with both dates | FR-035 |
| E9 | Thumbs-down with reason code persists and appears in triage | FR-043, FR-044 |
| E10 | Start new conversation; history isolated | FR-005, FR-006 |
| E11 | Stop generation mid-stream | — |
| E12 | LLM unavailable → error state, never a fabricated answer | §2.3 |
| E13–E18 | Admin: register source, approve, sync, view failure, reprocess, reindex | FR-011, FR-049, FR-050 |
| E19 | Admin dashboard reflects real ingestion state | FR-048 |
| E20 | Security admin searches audit by correlation ID, reconstructs a request | FR-051 |
| E20b | Deleting a knowledge source leaves its audit history intact and exportable | §6.9 |
| E21 | `end_user` receives 403 on every admin route | NFR-004b |
| E22 | Full keyboard path: composer → send → citation → drawer → open | WCAG 2.2 AA |
| E23 | Screen reader announces the completed answer once, not per token | §6.7 |
| E24 | Streaming renders correctly on a throttled 3G profile | NFR-002a |
| E25 | An English question retrieves and cites a French source; the answer is in English, the quoted passage is **not** translated | §6.7, BR-004 |
| E26 | Chat is fully usable at 375 px; admin mutations are unavailable below the breakpoint | §3.2 |

E23 is included because it is the accessibility defect this architecture is most likely
to produce: naive `aria-live` on a token stream announces hundreds of fragments and
makes the product unusable with a screen reader while passing every automated check.

---

## 5. UAT plan

**Objective:** confirm that AskAU answers real MISD questions correctly, safely, and
fast enough that staff prefer it to searching documents themselves.

### Participants

| Group | Count | Represents |
|---|---|---|
| MISD general staff | 12 | Primary user class — the class the SRS says matters most |
| Departmental staff, mixed directorates | 6 | Cross-department permission boundaries |
| Knowledge administrators | 3 | Source and lifecycle management |
| System administrator | 2 | Operations, dashboards, runbook |
| Security/compliance administrator | 2 | Audit, access review, AI-safety events |

Participants must include staff who were **not** involved in requirements discovery.
Users who helped specify a system unconsciously ask it the questions it was built for.

### Entry criteria

| | |
|---|---|
| ☐ | All blocking evaluation metrics met (`08-evaluation-harness.md`) |
| ☐ | Security review signed off by STK-004 |
| ☐ | Approved UAT corpus ingested at ≥ 98% success |
| ☐ | All E2E journeys passing |
| ☐ | Runbook validated by an administrator who did not build the system |
| ☐ | UAT environment separate from production, with anonymized or cleared content |

### Scenario set — 40 scenarios across six themes

| Theme | Count | Representative scenario |
|---|---|---|
| Everyday answers | 12 | "What is the procedure for requesting official travel?" — expect grounded answer, correct policy, openable source |
| Follow-up & context | 5 | Ask, then "does this apply to staff on probation?" |
| Trust boundaries | 6 | Ask something the corpus does not cover — expect a refusal, and **rate whether the refusal was helpful** |
| Permissions | 5 | A Department Y user asks a question only a Department X document answers |
| Currency & conflict | 4 | Ask about a policy with a superseded prior version |
| Administration | 8 | Register a source, handle a failed ingestion, investigate via audit |

### Acceptance criteria

| Criterion | Threshold | Method |
|---|---|---|
| Task completion | ≥ 90% of scenarios completed unaided | Observation |
| Answer usefulness | ≥ 80% rated helpful | FR-043 in-product |
| Citation trust | ≥ 85% agree citations let them verify the answer | Survey |
| **Refusal acceptability** | **≥ 75% agree refusals were appropriate, not frustrating** | Survey |
| Unauthorized disclosure | **0** | Observation + audit review |
| Perceived speed | ≥ 80% rate acceptable or better | Survey |
| Preference over current method | ≥ 70% prefer AskAU to searching manually | Survey |
| Admin task completion | 100% unaided using the documentation | Observation |

Refusal acceptability is an explicit acceptance criterion because the system is designed
to refuse and refusals are the most likely source of user frustration. If staff
experience refusals as failure, adoption fails regardless of how correct the grounded
answers are — and the fix would be interface and expectation-setting work, which needs
to be discovered during UAT rather than after rollout.

### Exit criteria

All acceptance criteria met, all severity-1 and severity-2 defects resolved and
retested, sign-off from the Business Analyst, Security Officer, and Product Owner.

| Severity | Definition | Resolution |
|---|---|---|
| S1 | Unauthorized disclosure, fabricated policy, or data loss | Blocks release, no exceptions |
| S2 | Core journey broken, or a wrong answer with high confidence | Blocks release |
| S3 | Degraded experience with a workaround | Fix or formally defer |
| S4 | Cosmetic | Backlog |

---

## 6. Non-functional testing

| Type | Method | Gate |
|---|---|---|
| Load | k6 at 1×, 5×, 10× expected pilot concurrency, 30 min sustained | NFR-002a/b hold at 5× |
| Soak | 8 h at 2× | No memory growth, no connection leak, no cache degradation |
| Spike | 0 → 10× in 60 s | Graceful degradation per the ladder; no errors before step 6 |
| Failover | Kill the Postgres primary mid-load | Reads continue; writes recover < 60 s |
| Chaos | Kill Redis; kill a worker; sever the LLM endpoint | Documented behavior matches the runbook |
| Recovery | Restore from backup to a scratch instance | **Verified by running the hybrid query**, not by job status |
| Accessibility | axe-core in CI + manual screen-reader pass | WCAG 2.2 AA, zero critical |
| Penetration | Third-party, pre-production | No high or critical findings open |

Chaos tests assert against the runbook specifically. If the observed behavior and the
documented behavior disagree, the defect may be in either — and an inaccurate runbook is
a real defect, because it is what an administrator will follow at 3am.

---

## 7. Gates

Until CI is built (deferred to the Month 2 gate — see `14-ci-and-code-quality.md` §2),
every row below runs as a local command: `make check` in `api`,
`npm run check` in `web`. The pipeline, when added, calls those same commands.


| Stage | Runs | Blocks merge |
|---|---|---|
| Pre-commit | format, lint, secret scan | yes |
| Merge request | unit, integration, **full security suite**, import contracts, type check, migration dry-run, smoke evaluation, contract diff | yes |
| Merge request | dependency audit, SAST, SBOM | on high/critical |
| `main` | E2E, full evaluation, accessibility | yes |
| Scheduled — nightly | full evaluation, soak | alerts |
| Scheduled — weekly | load, chaos | alerts |
| Pre-release | acceptance evaluation, failover, recovery, pen-test review | yes |

**Migration dry-run on every merge request** catches the class of defect that is
otherwise found in production: a migration that applies cleanly to an empty schema and
fails against real data volume or an existing partition set. The job restores a seeded
snapshot into the CI-provided PostgreSQL service, then applies the migration against it.
