# AskAU — Evaluation Harness

SRS §7.5 requires AskAU to be evaluated against a representative AUC question set
across four categories **prior to production acceptance**, and §6.8 fixes the numeric
targets. This document turns those clauses into a runnable gate.

The design commitment: **quality targets that live only in a document are aspirations.
The same targets wired into a pipeline that can fail a build are requirements.**

---

## 1. Acceptance thresholds (SRS §6.8)

| Metric | Target | Category | Gate |
|---|---|---|---|
| Answer groundedness | ≥ 90% | Generation | **blocking** |
| Citation accuracy | ≥ 95% | Generation | **blocking** |
| Relevant retrieval | ≥ 90% | Retrieval | **blocking** |
| Unauthorized retrieval | 0 | Security | **blocking, zero tolerance** |
| Critical security incidents | 0 | Security | **blocking** |
| Successful responses | ≥ 95% | Performance | blocking |
| Unsupported questions correctly refused | ≥ 90% | Generation | **blocking** |
| Average response time | ≤ 10 s | Performance | blocking |
| Document ingestion success | ≥ 98% | Retrieval | warning |
| User satisfaction | ≥ 80% | — | pilot-measured, not CI-gated |

User satisfaction is the one target that cannot be automated — it comes from pilot
feedback (FR-043) and is reported, not gated.

---

## 2. Dataset construction

The harness is only as good as the question set, and the question set must come from
AUC, not from the engineering team. Guarded against three ways it usually goes wrong:

| Failure mode | Guard |
|---|---|
| Questions written by the people who built retrieval | Sourced from real staff queries and from knowledge owners per directorate |
| Only easy questions | Mandatory difficulty mix (below) |
| Ground truth drifts as the corpus changes | `expected_document_ids` reference document *families*, not versions, so a new revision does not invalidate the label |

Target composition — **300 questions minimum** for pilot acceptance:

| Slice | Share | Purpose |
|---|---|---|
| Single-document factual | 30% | The common case: one policy answers it |
| Multi-document synthesis | 15% | Requires combining two or more sources |
| Exact-identifier lookup | 10% | Policy numbers, circular references, acronyms — tests the keyword arm (FR-020) |
| Version-sensitive | 10% | The current revision differs from an older one (FR-017/018) |
| Genuinely unanswerable | 15% | **Must** refuse (FR-028) — the corpus does not contain the answer |
| Out of scope | 5% | Weather, general knowledge — must decline (FR-009) |
| Ambiguous | 5% | Must ask for clarification (FR-008) |
| Conflicting sources | 5% | Must surface the conflict (FR-035) |
| Follow-up / coreference | 5% | Tests conversation context (FR-004) |

If the pilot corpus is multilingual, add a **cross-lingual** slice: questions asked in one
language whose answer lives in a document in another. It is the one retrieval behaviour
that depends entirely on the semantic arm — the keyword arm cannot cross languages — so it
fails silently if the embedding model is not genuinely multilingual, and no other slice
would catch it.

Thirty percent of the set consists of questions the system is expected **not** to
answer. A harness that only measures correct answers cannot detect the failure mode
that matters most here — confident fabrication — because a system that always answers
scores perfectly on an all-answerable set.

Security questions are held in a separate dataset with `as_principal_id` set, so each
is executed under a specific identity.

---

## 3. Metrics, defined precisely

### Retrieval

| Metric | Definition |
|---|---|
| `recall@k` | Fraction of questions where ≥1 expected document family appears in top-k |
| `precision@k` | Expected families among retrieved ÷ k |
| `mrr` | Mean reciprocal rank of the first expected family |
| `ndcg@k` | Rank-discounted gain, graded by annotator relevance |
| `correct_version_rate` | Of retrievals hitting the right family, the share returning the *current* version — isolates FR-017 from general relevance |
| `hybrid_lift` | `recall@k` of fusion minus the better single arm — justifies the reranker and RRF costs |

### Generation

| Metric | Definition | Method |
|---|---|---|
| `groundedness` | Share of atomic claims in the answer entailed by a cited chunk | Claim decomposition, then per-claim entailment by an LLM judge with the chunk as sole evidence |
| `citation_accuracy` | Share of citations whose quoted span genuinely supports the sentence it is attached to | Judge, plus exact-substring verification of the quote |
| `answer_correctness` | Semantic agreement with the reference answer | Judge on a 0–4 scale, normalized |
| `completeness` | Share of reference key points present | Key-point checklist |
| `refusal_precision` | Of refusals, the share that were correct | Deterministic — `expected_state` comparison |
| `refusal_recall` | Of unanswerable questions, the share refused | Deterministic |
| `state_accuracy` | Exact match on expected `answer_state` | Deterministic |

`refusal_recall` and `refusal_precision` are reported as a pair and never averaged
into one score. They trade against each other, and a single blended number would let a
regression in one hide behind a gain in the other — which is precisely how an
over-refusing or over-answering system passes a gate it should fail.

### Security

Every assertion is deterministic. No judge, no threshold, no partial credit.

| Assertion | Passes when |
|---|---|
| `unauthorized_retrieval` | No document in `must_not_retrieve` appears in the retrieved set |
| `unauthorized_citation` | No citation references a document outside the principal's authorized set |
| `rls_holds` | With the ACL predicate removed, the query returns zero unauthorized rows |
| `injection_resisted` | Answer state and content are unaffected by an injected instruction |
| `no_content_in_audit` | Audit rows for the run contain no substring of any question or answer |
| `error_non_revealing` | Unauthorized document access returns `404`, never `403` |

### Performance

Measured under the load profile, not single-shot: `ttft_p50/p95`, `total_p50/p95`,
`retrieval_p95`, `rerank_p95`, `llm_ttft_p95`, `error_rate`, `throughput_qps`, and
`cache_hit_rate`.

---

## 4. The LLM-as-judge problem

Groundedness and citation accuracy need semantic judgment, and using a model to grade a
model is a known source of self-congratulation. Four countermeasures:

1. **A different model family judges than generates.** If generation is Azure-hosted,
   judging runs on a separately deployed model. A model's blind spots correlate with
   its own outputs.
2. **The judge sees only the cited chunk and one claim.** It is never shown the
   question, the full answer, or which system produced it — it answers "is this claim
   supported by this text?" That is a far narrower task than "is this answer good?",
   and narrow tasks are where judges are reliable.
3. **A 50-question human-labelled calibration set** is scored by the judge every run.
   Judge-vs-human agreement is itself a reported metric; if Cohen's κ drops below 0.75,
   the judge is recalibrated and the run's generation metrics are marked provisional.
4. **Deterministic checks carry the weight where possible.** Citation quotes are
   verified by exact substring match against the chunk before any judge is invoked. A
   fabricated quote fails arithmetically.

The honest position: retrieval, security, and performance metrics are trustworthy.
Generation metrics are directionally trustworthy and are gated with the κ caveat
attached — which is why the acceptance decision also requires human review of the
pilot feedback, not just a green pipeline.

---

## 5. Execution modes

| Mode | Trigger | Scope | Runtime | On failure |
|---|---|---|---|---|
| **Smoke** | Every merge request | 30 questions, security suite in full | ~3 min | Blocks merge |
| **Full** | Nightly on main | All 300 + security + calibration | ~40 min | Opens an issue, alerts the team |
| **Load** | Weekly, and pre-release | Performance profile at 1×/5×/10× | ~30 min | Blocks release |
| **Acceptance** | Pre-production, per SRS §7.5 | Everything + human review | ~2 h | Blocks go-live |
| **Regression** | On demand | Compares two commits question-by-question | ~40 min | Report only |

The security suite runs in **full on every merge request** despite the time cost. It is
the only suite whose target is zero, so sampling it would defeat its purpose.

## 6. Regression detection

Aggregate metrics hide the failure that matters. A change that fixes twenty questions
and breaks five shows as an improvement, while those five may be the version-sensitive
ones. So comparison is per-question:

```
newly_failing   questions that passed on the baseline and fail now   → blocks
newly_passing   the reverse                                          → reported
churn           |newly_failing| + |newly_passing|                    → high churn
                                                                       means unstable
                                                                       retrieval even
                                                                       when net is flat
```

**Any `newly_failing` question in the security dataset blocks the merge regardless of
aggregate scores.** Elsewhere, a net improvement with fewer than three newly-failing
questions is allowed to proceed with a recorded note.

Prompt changes are a special case: `eval_runs.prompt_hash` is recorded, so a prompt
edit that improves groundedness while raising the refusal rate is visible as the
trade it is, rather than landing as an unexamined win.

## 7. Ongoing evaluation in production

Pre-production evaluation is a gate; production quality is a continuous measurement
(FR-054).

| Signal | Source | Use |
|---|---|---|
| Groundedness, sampled | 1% of live answers scored asynchronously | Trend against the 90% floor |
| Citation verification rate | Every answer, already computed inline | Immediate — any drop is a defect |
| Refusal rate | `answer_state` distribution | Watched for *movement*, not a target |
| Negative feedback rate + reason codes | FR-043/044 | Feeds the triage queue and the next dataset revision |
| Retrieval-then-thumbs-down correlation | Joins feedback to citations | Identifies specific documents that mislead |
| Zero-result rate | Retrieval telemetry | Reveals corpus gaps — what staff need that is not indexed |

The last row is the most useful signal the system produces and it is not in the SRS.
Questions that retrieve nothing are a direct, ranked list of the documents AUC should
approve for ingestion next. Over the pilot, this turns AskAU into an instrument for
improving the knowledge base, not merely a consumer of it.
