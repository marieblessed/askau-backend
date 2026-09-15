# AskAU — Architecture Decision Records

Each record states what was decided, what it costs, and — most importantly — **what
would make us revisit it**. A decision without a revisit trigger becomes dogma the
moment the person who made it leaves the project.

Status values: `accepted` · `superseded` · `revisit-pending`.

New records go in `docs/architecture/adr/NNNN-short-title.md` once the repository
exists; these are the founding set.

---

### ADR-0001 — Authorization is enforced in the retrieval query, not by the model
`accepted`

**Context.** BR-006 and FR-002 require that the LLM never determine authorization. The
tempting implementation is to retrieve broadly and instruct the model to withhold what
the user may not see.

**Decision.** Permission filtering is a predicate in the retrieval SQL, applied in both
the semantic and keyword arms. Row-level security on `chunks` provides an independent
second enforcement point. No code path passes an authorization question to the model.

**Consequences.** Unauthorized content never enters the context, so no prompt failure
can leak it. Costs: the ACL must be queryable at retrieval time, which drives ADR-0002.

**Revisit if:** never. This is the requirement, not an implementation choice.

---

### ADR-0002 — Denormalize access lists onto the chunk row
`accepted`

**Context.** ADR-0001 needs the ACL available at retrieval time. Normalized, that is
`chunks → documents → document_acl → user_principals` — three joins on the hot path,
and the vector index cannot pre-filter through them.

**Decision.** `chunks.acl_principals BIGINT[]`, GIN-indexed, materialized from
`document_acl`. The normalized table remains authoritative; the array is always derived.

**Consequences.** One indexed array-overlap replaces three joins — the largest single
latency win in the design. Cost: permission changes require reconciliation, so there is
a window during which a revocation is not yet effective. FR-025 explicitly permits this.
Revocations are prioritized over grants in the reconciler because the two have
asymmetric risk.

**Revisit if:** the required sync window drops below ~1 minute, or ACL churn becomes
high enough that reconciliation cost exceeds the query saving.

---

### ADR-0003 — Partition chunks by classification
`accepted`

**Context.** 50M chunks in one HNSW index is a large graph to search, and restricted
content sits in the same structure as public content.

**Decision.** LIST-partition `chunks` on `classification`, one HNSW index per partition.

**Consequences.** Most callers are authorized for two of four tiers, so the planner
prunes the rest — smaller graphs, better recall per unit of work, and structural
containment behind the ACL predicate. Cost: reclassifying a document moves rows between
partitions, and the composite primary key `(id, classification)` propagates into
`citations`.

**Revisit if:** AUC adopts a classification scheme with many more tiers, or if
cross-tier queries become the common case rather than the exception.

---

### ADR-0004 — Hybrid retrieval as a single SQL statement with in-database RRF
`accepted`

**Context.** FR-021 wants semantic and keyword retrieval combined. The usual approach
issues two queries and merges in the application.

**Decision.** One CTE runs both arms and fuses them with Reciprocal Rank Fusion (k=60)
inside Postgres.

**Consequences.** One round trip instead of two; no application-side sort. RRF over
weighted score blending because cosine distance and `ts_rank_cd` are not on comparable
scales — rank fusion needs no per-corpus tuning and stays stable as the corpus grows.
Cost: the fusion weighting is less tunable than explicit score blending.

**Revisit if:** a measured retrieval-quality gain from tuned score blending exceeds the
operational cost of maintaining that tuning, or if the search layer moves to OpenSearch,
where fusion would move with it.

---

### ADR-0005 — pgvector rather than a dedicated vector database
`accepted`

**Context.** Dedicated vector stores offer better raw ANN performance at very large scale.

**Decision.** PostgreSQL with pgvector ≥ 0.8 for Phase 1, behind a `Retriever` port.

**Consequences.** ACL metadata, full-text index, and vectors live in one system, so the
authorization predicate and the fusion happen locally — with a separate vector store,
ADR-0001 becomes a cross-system consistency problem, which is a far worse failure mode
than slower search. Cost: ANN performance ceiling around 10⁸ chunks.

**Version floor is a hard requirement, not a preference:** below 0.8, `iterative_scan`
is unavailable and a selective ACL filter causes HNSW to under-return silently. The
failure mode is degraded recall with no error, which is the worst kind.

**Revisit if:** measured retrieval p95 exceeds 150 ms after tuning, or the corpus
approaches 10⁸ chunks.

---

### ADR-0006 — 1024-dimension embeddings rather than 1536
`accepted`

**Context.** HNSW index memory is the binding infrastructure constraint at the design
point (~300 GB at 1024 dimensions across partitions).

**Decision.** Truncate `text-embedding-3-large` to 1024 dimensions.

**Consequences.** ~33% less index memory and a smaller database tier, for negligible
retrieval quality loss at this corpus size. Cost: changing dimension later requires
re-embedding the entire corpus.

**Revisit if:** evaluation shows `recall@10` below 0.95 attributable to dimension, or
the corpus grows enough that quality loss becomes measurable.

---

### ADR-0007 — Own the RAG orchestration rather than adopt a framework
`accepted`

**Context.** LangChain, LlamaIndex, and similar frameworks provide pre-built RAG pipelines.

**Decision.** Implement the ~10 pipeline stages directly against our own ports.

**Consequences.** Every stage is auditable and traceable to a specific FR ID, which is
what a security reviewer needs in order to verify ADR-0001. A framework that abstracts
prompt assembly and retrieval makes the authorization boundary harder to demonstrate,
and that is the one property this project cannot compromise. Cost: ~600 lines of
orchestration to own, and no community-contributed connectors.

**Revisit if:** the Phase 3 agent layer needs graph orchestration complex enough that
building it outweighs the auditability cost — LangGraph and Microsoft Agent Framework
are both named as options in SRS §2.6 for exactly that phase.

---

### ADR-0008 — Refusal is a first-class state, and the evidence gate runs before the model
`accepted`

**Context.** FR-028 requires refusal when evidence is insufficient. The common
implementation instructs the model to refuse and hopes it complies.

**Decision.** Score evidence sufficiency before generation; below threshold, return a
deterministic refusal without calling the model. `answer_state` is persisted, sent on
the wire, and rendered as its own UI component.

**Consequences.** Refusal cannot be talked past by a well-phrased question, costs zero
tokens, and becomes a measurable rate rather than a hoped-for behavior. Cost: the
threshold is a tuning parameter, and setting it too high refuses answerable questions.

**Revisit if:** evaluation shows refusal recall above 90% but precision below 80% —
that combination means the gate is firing on questions the corpus can actually answer.

---

### ADR-0009 — Unauthorized resources return 404, not 403
`accepted`

**Context.** `403 Forbidden` is the semantically correct status for an authorization failure.

**Decision.** Return `404 not_found` for both "absent" and "not authorized".

**Consequences.** A user cannot enumerate which restricted documents exist. For
confidential AUC material, existence is itself sensitive (asset A2 in the threat model).
Cost: harder to debug legitimate permission problems — mitigated by audit events, which
record the real outcome as `denied` even though the response says `not_found`.

**Revisit if:** never for document resources. Administrative routes correctly return
`403`, since role membership is not sensitive in the same way.

---

### ADR-0010 — Degraded mode returns an error rather than an ungrounded answer
`accepted`

**Context.** When retrieval or the model provider is unavailable, the system could fall
back to answering from model knowledge.

**Decision.** Return `502 upstream_unavailable`. Optionally serve ranked source
documents with no generated prose (`ASKAU_RETRIEVAL_ONLY_MODE`).

**Consequences.** The system never produces an answer that is not grounded in approved
AUC sources, satisfying FR-033 and BR-007 even during an outage. Cost: reduced
availability as users perceive it — an error rather than a plausible answer.

The judgment behind it: a wrong answer about AUC policy costs more institutional trust
than an outage does, and trust lost this way is not recovered by later uptime.

**Revisit if:** never. This is the product's core promise.

---

### ADR-0011 — Server-rendered shell with a single client boundary for streaming
`accepted`

**Context.** A conversational interface is naturally a client-side application.

**Decision.** Next.js App Router; session, conversation list, and message history render
as server components. Only the live answer, composer, citation drawer, and feedback
control are client components.

**Consequences.** First paint arrives with real content and no client-side auth round
trip; the access token never reaches client JavaScript, removing the XSS-token-theft
class entirely. Client bundle stays under 120 KB on the chat route. Cost: a more complex
mental model than a pure SPA, and care needed at the server/client boundary.

**Revisit if:** the Phase 3 agent interface needs rich client-side state that makes the
boundary unwieldy.

---

### ADR-0012 — Build i18n scaffolding despite Phase 1 having no i18n requirement
`revisit-pending`

**Context.** SRS §6.7 explicitly leaves internationalization undefined and flags it as
an open item, noting AUC's multilingual context.

**Decision.** Build the UI i18n-ready anyway: message catalogs, no hardcoded strings,
logical CSS properties throughout, `lang`/`dir` driven by user preference. Ship English
only.

**Consequences.** Right-to-left support and translation become a content exercise rather
than a refactor. AUC operates in Arabic, English, French, Kiswahili, Portuguese and
Spanish, so the probability this is needed is high. Cost: modest discipline overhead, and
some scaffolding that may go unused.

This is the one decision here made on a *forecast* rather than a requirement, which is
why it is marked revisit-pending rather than accepted.

**Revisit when:** AUC resolves the §6.7 open item. If the answer is genuinely
English-only in perpetuity, the scaffolding is harmless; if it is six languages, this
decision saves a rewrite.

---

### ADR-0013 — Split backend and frontend into separate repositories
`superseded by ADR-0016`

**Context.** A monorepo gives compile-time coupling between the API and its client:
change a response shape and the frontend fails to build immediately. That is genuinely
valuable, and giving it up needs a reason.

**Decision.** Two repositories, `api` and `web`, coupled only by a
committed OpenAPI contract.

**Consequences.** Independent release cadence — interface copy changes stop dragging
the Python suite through CI. Path-based security review becomes structural rather than
a per-path approval-rule convention. And the API is treated as a real published surface with the
web app as one consumer among several, which is what SRS §3.4 actually asks for and
what the Phase 2 integration layer will need.

**The cost is the contract.** Four mechanisms replace the compiler: the API generates
and commits its own `openapi.json`, breaking changes must be declared in a changelog or
CI fails, the web app regenerates types from a pinned ref and fails on drift, and
consumer-driven contract tests catch optionality mismatches that shape checks miss.
That is more machinery than a monorepo needs, and it is the honest price.

**Revisit if:** contract churn becomes high enough that the pinning workflow slows
delivery more than the independent cadence speeds it up — in practice, if more than
about one in five API merge requests requires a coordinated frontend change.

**Superseded.** That revisit condition was met before a line of the pinning workflow was
written: the interface and the API are being built together, and coordinated changes are
the common case rather than one in five. See ADR-0016.

---

### ADR-0014 — Neither repository owns containerization or infrastructure
`accepted`

**Context.** AUC already maintains separate tooling that provisions and runs
PostgreSQL, Redis, and supporting services.

**Decision.** No container definitions, orchestration manifests, or infrastructure code
in either application repository. Both reach their dependencies through environment
variables and assume nothing about hosting.

**Consequences.** One owner for infrastructure instead of two competing definitions.
The application stays genuinely portable, which also keeps SRS §2.6's deliberately open
operating-environment choice open. Local development, CI, and every deployed
environment differ only by configuration.

Two obligations fall out of this and are worth naming. Configuration validation must
**fail fast at startup** with a clear message, because a missing variable is now the
most likely first-run failure. And the pgvector version and session parameters that
retrieval correctness depends on are asserted by the application at startup rather than
assumed — the platform team provisions the database, so the application has to verify
the properties it needs rather than trusting them.

**Revisit if:** the platform tooling cannot supply a required capability — most
plausibly a pgvector version below 0.8, which is a hard floor rather than a preference.

---

### ADR-0015 — Text-search configuration is per document language, not global
`accepted`

**Context.** The natural implementation of the keyword arm is a generated column:
`tsvector GENERATED ALWAYS AS (to_tsvector('english', content))`. The Commission works in
six official languages and is headquartered in a country using Ethiopic script.

**Decision.** `chunks.tsv` is written by the ingestion pipeline using a configuration
derived from the document's detected language, not generated with a fixed one. Languages
Postgres has no stemmer for fall back to `simple`.

**Consequences.** Each document is stemmed in its own language, so keyword retrieval works
for non-English content. The cost is that `tsv` is no longer maintained by the database —
if the pipeline fails to set it, the row is simply unsearchable by keyword, which is a
silent failure and needs a coverage check in ingestion validation.

Two things follow that are easy to miss. The embedding model **must** be multilingual,
because it is the only component that can match an English question to a French document —
keyword search cannot cross languages by construction. And answers must never present a
*translated* quotation: a translated passage is no longer verifiable against the
authoritative source (BR-004).

**Why it matters that this was nearly missed:** the wrong version does not error. English
stemming applied to French produces poor tokens, the semantic arm partially compensates,
and retrieval is simply worse for every non-English document with nothing in the logs to
say so.

**Revisit if:** AUC confirms an English-only corpus in perpetuity, in which case the
generated column is simpler and adequate.

---

### ADR-0016 — One repository, independently deployed components
`superseded by ADR-0017` · superseded ADR-0013

**Context.** ADR-0013 chose two repositories and named the price: four mechanisms —
committed contract, breaking-change changelog, pinned-ref codegen, consumer-driven
contract tests — standing in for what a compiler does for free. Its own revisit
condition was "more than about one in five API merge requests requires a coordinated
frontend change".

Building the two together made the answer obvious well before that threshold. Adding
`classification` to a citation touched the domain model, the wire schema, two routes and
three components of the interface. That is one change. Across two repositories it is two
merge requests that must land in order, and the window between them is an interface
reading a field the API has not shipped yet — or worse, a contract regenerated from an
unmerged branch.

**Decision.** One repository. `api`, `web` and `contracts/` are
separate components within it, each deployed on its own cadence and pipeline. A monorepo,
explicitly not a monolith: nothing is released as a unit and no component imports
another's source.

**What this does not give up.** The three reasons ADR-0013 gave were real, and each is
met without a repository boundary:

| Concern from ADR-0013 | How it is met here |
|---|---|
| Interface copy changes should not drag the Python suite through CI | Path-filtered pipeline rules. A merge request touching only `web/**` runs only that component's jobs |
| `retrieval/` and `core/authz.py` need Security Officer review; frontend does not | Per-path approval rules on those directories. ADR-0013 called separate repositories "structural rather than a convention" — but a path rule that CI enforces is equally structural, and it survives the two changing together |
| The API is a published surface with the web app as one consumer among several | Unchanged. `contracts/openapi.json` is a top-level component precisely so it does not read as a backend build output. A second consumer adds a directory, not a repository |

**Consequences.** A change spanning both components is one reviewable diff, and the
contract check runs against the same commit that changed the API rather than against a
pinned ref that may be behind. The breaking-change changelog and consumer-driven contract
tests remain worthwhile for the Phase 2 integration layer — where consumers genuinely are
outside this repository — but they are no longer load-bearing for the web client.

The cost is discipline that a tool cannot fully enforce: it is now *possible* for a
component to reach into another's source, and nothing but review stops it. The mitigation
is the rule stated in the root `README`: every component must build, test and deploy from
its own directory with no reference to the root, and the root `Makefile` may only
delegate.

**Revisit if:** a component acquires an independent owner outside the programme, or the
repository grows a component whose toolchain cannot be path-filtered cleanly.

---

### ADR-0017 — The web client is a separate repository after all
`accepted` · supersedes ADR-0016

**Context.** ADR-0016 chose one repository on the strength of a specific argument: that a
change spanning the API and the interface is *one* change, and splitting it makes two
merge requests that must land in order. That argument was sound, and it assumed a fact
that turned out not to hold — that we owned both sides.

A separate team had already built the web client (`askau-frontend`: Next.js 16, NextAuth +
Entra, next-intl, four locales). It is not ours to absorb, and the `web/` directory in this
repository was a second, competing implementation of the same product.

**Decision.** Delete `web/`. This repository is the API, the ingestion worker, the shared
contract and the documentation. The web client lives in `askau-frontend`.

**What decided it.** Not the principle — the running cost. In a single session, renaming
the API surface to `/api/v1` and adopting the client's camelCase and envelope conventions
required `web/` to be re-pointed and its types reshaped twice, purely to keep an
application with no users compiling. Every future backend change carried the same tax, and
paying it bought nothing: the client that matters could not benefit, because it is not
here.

Admin was the one thing `web/` had that `askau-frontend` does not — their `/admin` is a
stub. That does not save it, because administration is out of Phase 1 scope.

Precisely what is lost is worth stating, because it is narrower than "admin": the
endpoints behind FR-046 to FR-053 all remain, are role-gated, and are covered by
`tests/security/test_route_guard_coverage.py` and `tests/integration/test_feature_smoke.py`.
What is gone is the **console UI**, which only FR-048 cited. That row in the traceability
matrix now reads "API only; no console UI" rather than pointing at a directory that no
longer exists.

**What replaces the compile-time coupling ADR-0016 wanted.** Three things, all built:

1. `contracts/openapi.json`, generated by `make contract` and gated by `make contract-check`
   — the API cannot change without the contract changing in the same commit.
2. `api/tests/contract/`, which reads the client's own TypeScript types from its checkout
   and asserts our payloads carry every field they declare. It caught nothing at the time
   of writing because the shapes were built from those types, which is the point: it will
   catch the *next* divergence.
3. The state-mapping tests, which pin the places where the two models deliberately differ
   so a difference cannot quietly become a defect.

This is weaker than a compiler and stronger than the pinned-ref workflow ADR-0013
described, because the check runs against the client's working tree rather than a tag
somebody remembered to bump.

**Consequences.** A spanning change is again two merge requests in two repositories, and
the interval between them is real. The contract test narrows the window rather than
closing it. Accepted, because the alternative — maintaining a frontend nobody uses so that
spanning changes stay atomic — costs more every week than the coordination does.

**Revisit if:** the client team wants to co-locate, or a second consumer of this API
arrives and the contract mechanisms prove insufficient for two of them.

---

### ADR-0018 — "Save conversation history" off means no row, not a hidden row
`accepted`

**Context.** The web client has shipped a *Save conversation history* toggle since its
first commit, wired to nothing. A switch that tells someone their questions are not kept,
while `messages` records every one, is worse than no switch — it is a privacy promise the
product does not keep.

Two facts made the design non-obvious.

First, **a conversation id is needed even when nothing is kept.** Streaming, citation
resolution and feedback all key off `conversation_id`, so the turn has to exist for the
life of the request. The first implementation wrote the `conversations` row and skipped
only the messages. That was wrong in a way that was visible on screen: the empty row still
appeared in the history list, so switching the setting off produced a growing list of blank
conversations — the opposite of what it claims.

Second, **opting out of history is not opting out of audit.** BR-008 makes `audit_events`
append-only and non-optional. That somebody asked something, and which documents their
question reached, is a security record, not a convenience anyone may decline.

**Decision.** With `save_history` off, `POST /conversations` mints a uuid4 and writes
nothing. The ownership check accepts an id with *no row at all* from a caller who has
history off, alongside its usual ownership clause.

The audit event is written regardless, and it never contains the question or the answer.
That was verified rather than assumed: `audit_events.detail` carries the answer state, the
groundedness and the retrieved document ids, and nothing else. Honouring the flag therefore
costs the audit log nothing, which is what lets both requirements hold at once.

**Why widening the ownership predicate is safe.** Another person's conversation *has* a
row, so the ownership clause answers false for it and the second clause never sees it. The
only ids admitted are ones no conversation exists under — no history to read, no owner to
impersonate. A guessed uuid buys a stranger exactly what an unguessed one does. The
condition lives in one named helper rather than at each call site, because a security
predicate with a special case in two copies is one edit from disagreeing with itself.

**Consequences.** Feedback is impossible for these turns — `message_feedback` has a foreign
key to `messages`, so there is nothing to rate. The response returns a null `messageId`,
which the client's `AnswerActions` already reads as "hide the feedback control", so no
client change is needed. FR-043 does not apply to a turn that was never written; that is
the honest cost of the setting rather than a gap.

**Revisit if:** a retention policy requires conversations to survive independently of user
preference, in which case this becomes a conflict for the records officer rather than an
engineering decision.

---

### ADR-0019 — "Higher intelligence" is consent to escalate, not a quality tier
`accepted`

**Context.** The client's copy is precise and easy to misread: *"AskAU can **automatically**
use more thorough retrieval when answering complex questions."* That is not a setting the
reader uses to pick a quality level — it is permission for the system to spend more when it
judges the first attempt weak. Building it as a tier the client selects would implement a
different feature than the one described.

**Decision.** Two pieces.

*Named server-side profiles.* `standard` and `thorough` resolve to `candidate_k` and
`rerank_input_k` inside the backend (`domain/profiles.py`). `standard` **is** the configured
baseline, so the tier system is inert until something escalates and no existing deployment
changes behaviour. `thorough` is a multiple of the baseline rather than a second set of
absolute numbers, so an operator who tunes the corpus down does not find the escalated path
still reaching for the default's multiple.

`top_k` does not grow with the tier. More candidates give the reranker more to choose from,
which is the point; more chunks in the prompt is a different and worse change — it dilutes
the context, costs tokens linearly, and pushes the material that matters further from the
instruction.

*An escalation trigger, not a complexity guess.* The second pass runs when the evidence gate
is unconvinced **and** the caller has consented. Deciding a question is "complex" before
retrieving is guesswork that would make every question more expensive to help the few that
need it; the evidence signal is about *this* question and costs nothing on answers that
were already grounded.

**Retrieval width is never a client parameter.** `AskRequest` sets `extra="forbid"`, so a
body carrying `topK`, `candidateK` or `tier` is a 422. Ignoring it would leave an integrator
believing it worked and quietly getting different results than they think — a bug report
about relevance months later with nothing in the logs to explain it.

**What escalation does not do.** It never lowers the bar. The wider result is re-assessed on
its own merits, and a second pass that still finds nothing is still a refusal. Getting this
wrong is how a "more thorough" setting becomes a setting that fabricates answers for the
people who switched it on.

**Consequences.** Both the buffered and streaming paths share one gate (`_assess`), because
a gate that escalated on one path only would answer or refuse the same question depending on
which endpoint was called — these two have drifted before. Escalation is recorded in the
audit detail so a rise in usage is attributable rather than inferred from latency. The query
embedding is computed once and reused across passes; re-embedding an identical string would
bill `model_invocations` for work that is not work.

**Revisit if:** escalation turns out to fire often enough to matter for cost, at which point
the trigger wants a rate limit rather than a different design.

---

### ADR-0020 — `shareAnalytics` gates evaluation sampling, and nothing else
`accepted`

**Context.** The client's copy — *"Help improve AskAU by sharing anonymised usage data"* —
describes telemetry. Telemetry is not something this toggle can govern: `audit_events` is
non-optional under BR-008, and `model_invocations` backs the §7.4 resource reporting the AUC
requires. A user cannot decline either, and a settings screen claiming otherwise would be
lying.

**Decision.** The setting means exactly one thing: *may this person's questions be sampled
into `eval_questions` for quality measurement.*

That is honest, enforceable, and it touches nothing required. It also converts a vague
toggle into the consent gate for the four `eval_*` tables, which have been schema-only since
they were created. Real staff questions are the most valuable evaluation corpus there is;
a synthetic one written by the team that built the retriever is the least valuable, because
it asks things the way the retriever expects them to be asked.

**What is stored.** The question, the state the answer reached, and the documents it drew
on. Not the answer, not the user id, and `as_principal_id` stays NULL — a question plus the
exact access footprint of the person who asked it is a small enough set to name someone,
which would make "anonymised" false. An evaluation run supplies its own principal anyway.

**Which questions.** Only the ones that went badly: insufficient evidence, a conflict, thin
support, a clarification. A grounded answer teaches the corpus nothing it does not already
know, and an unfiltered sample of production traffic becomes thousands of near-identical
rows.

**Consequences.** The UI copy needs to change to match — "anonymised usage data" implies
telemetry rather than content sampling, and the gap between the two is exactly the kind of
thing a data-protection review should catch. Sampling failures are swallowed so that a
quality nicety cannot fail an answered question; the cost of that is real and was paid once
during development, when a CHECK violation made a broken sampler indistinguishable from a
working one. `sample()` therefore returns a boolean and the tests assert on it, rather than
inferring success from a row count that is also zero when the write silently failed.

**Revisit if:** the AUC's data-protection position is that question text may not be retained
for improvement at all, in which case the toggle should be removed rather than defaulted off.

---

### ADR-0021 — An endpoint that only ever refuses is not a tested endpoint
`accepted`

**Context.** `test_route_guard_coverage` enumerates every route from the application's own
OpenAPI document and asserts that each one denies an unauthenticated caller and an ordinary
member of staff. It is a good test and it caught real gaps. It also created a blind spot
that nobody noticed for the life of the project.

A route-coverage trace — middleware recording `method + route template + status` across the
whole suite — showed the shape of it. Twelve mutating administrative operations had only
ever returned **401 or 403**. Approving a knowledge source, starting a sync, reindexing,
reprocessing a document, cancelling a run, triaging feedback, reclassifying a document:
every one proven to refuse, none proven to work. The three session endpoints — sign in,
refresh, sign out — had never been called at all, because every other test mints a bearer
token directly and skips the API that writes the session. So had all four health probes and
`/metrics`.

The failure mode is specific and it is quiet: **in a passing suite, an endpoint that always
refuses is indistinguishable from an endpoint that is broken.** A guard test is satisfied by
both.

**Decision.** Every documented operation must be exercised to a success (2xx or 3xx) by
some test, not merely to a refusal. That is now true for all 63.

**What writing those tests found**, none of which any existing test could have caught:

* **`POST /sources/{id}/sync` returned an unreachable `location`.** The API moved to
  `/api/v1` and the two hand-built strings still read `/v1`, so an operator following the
  pointer from a successful call got a 404. Nothing caught it because no test had ever read
  the response body.
* **FastAPI's own validation errors bypassed the error contract entirely.** `InvalidRequestError`
  had been moved to 422 for the client's sake, which is exactly what made this easy to miss:
  errors we *raise* were right, and errors FastAPI raises for us returned
  `{"detail": [...]}` — a shape their `lib/api/errors.ts` cannot read. It looks for `message`
  and `fieldErrors`, finds neither, produces `UnknownApiError`, and the production build
  suppresses the message. The single most common error a client hits reached the user as
  "An unexpected error occurred". Now converted by a `RequestValidationError` handler that
  emits the same problem document with `fieldErrors` keyed by field name.
* **Admin request bodies were snake_case** while every response was camelCase. `RenameIn`
  is the one that mattered — `PATCH /conversations/{id}` is a route their client calls.
  All request models moved onto `WireModel`; `populate_by_name` keeps snake_case parsing,
  so the change is additive.
* **A second ingestion run on one source is refused with 409**, naming the run in flight.
  Correct, undocumented, and discovered only because a test tried to sync and reindex the
  same source.
* **`ConflictError.code` is `VALIDATION_ERROR`**, which reads as a copy-paste slip and is
  not: their `ApiErrorCode` union has no `CONFLICT`, and a code outside the union falls
  through to `UnknownApiError` with the message suppressed. Now commented at the definition.

**Consequences.** The trace was scaffolding, not a fixture — temporary middleware, removed
after measuring. Making it permanent would mean production code carrying test bookkeeping,
and the alternative (a maintained list of covered routes) is precisely the hand-written
matrix `test_route_guard_coverage` exists to avoid. The measurement is repeatable in a few
minutes when it is wanted; the tests it produced are permanent.

**Revisit if:** the suite grows a natural place to record this — a session-scoped fixture
that owns the app — at which point the check is worth automating rather than re-running by
hand.

---

### ADR-0022 — SharePoint documents carry their own access list, never the source's
`accepted`

**Context.** Until now every document in a source inherited that source's
`knowledge_sources.access_rules`. That is correct for a filesystem export, which genuinely
has no per-file permissions. It is catastrophic for SharePoint, where two documents in one
library routinely have different audiences — a directorate's library holds both the staff
handbook and the disciplinary files.

A connector that ingested SharePoint under the source-level rule would publish confidential
material to everyone who can reach the library, and nothing about the result would look
wrong. This is the single worst failure this product can have, and it is one word away at
all times: `principals or source_rules` reads like a sensible default.

**Decision.** `RemoteDocument.principals` is per document, and `pipeline._write_acl`
branches on `None` versus a tuple rather than on emptiness:

* `None` — the source has no per-item access control. Inherit `access_rules`, as before.
* a tuple, **including an empty one** — the connector determined the audience. Use exactly
  that. An empty audience means nobody may read it, which is the correct reading of "we
  found no grantee we could map".

Those two cases must never collapse. A test asserts the empty case specifically, and a
mutation reintroducing the fallback fails it.

**The permission mapping**, which is the part with judgement in it:

| Graph | Mapped to | Why |
|---|---|---|
| `grantedToV2.user/group.id` | a principal on the Entra object id | never the display name or UPN — both are mutable, and an ACL keyed on a mutable field breaks the day somebody changes department |
| `grantedToIdentitiesV2[]` | the same, per entry | a sharing link's recipients are a real grant; missing it under-shares |
| `link.scope == "organization"` | the configured tenant-wide group | |
| `link.scope == "anonymous"` | **the document is refused** | see below |
| `grantedToV2.siteGroup` | **skipped** | SharePoint-local, no Entra object id to intersect against; guessing from a display name would be a guess that grants access |
| no `roles`, or `restricted` | nothing | Graph omits `roles` on some shapes, and reading "unspecified" as "read" turns an ambiguity in someone else's API into access in ours |

**Anonymous links are refused rather than narrowed.** Mapping "anyone with the link" to the
tenant group would be *narrower* than the truth, which sounds safe and is not: it would
present material the organisation has already published outside itself as controlled.

**Fail closed, and fail visibly.** A document whose permissions cannot be read is failed
with its own code (`access_unavailable`) and its own remedy, not folded in with corrupt
PDFs — an administrator sees "3 documents could not be assessed" rather than three documents
quietly missing.

**What building it found.** Three defects, none of which any existing test could have
caught:

* **Raising from inside `documents()` aborted the whole sync.** The first version let
  `AccessUnavailableError` escape the async generator, so one anonymously-shared file would
  have ended the enumeration of a forty-thousand-item library with every later document
  silently absent and the run reporting success. The error is now carried on the document.
  Note the interaction that makes it safe: a document with an `access_error` also carries an
  empty principal set, so a pipeline that forgot the check would store it readable by
  *nobody* rather than by everybody.
* **`documents.version_seq` defaulted to 1 and nothing ever set it.** A family could hold
  exactly one revision, so re-ingesting a changed document failed on the unique constraint —
  meaning re-sync, the whole of FR-049, did not work. No test had ever ingested the same
  document twice with different content. It also explains a gap elsewhere:
  `lifecycle = 'superseded'` could not arise naturally, which left the `outdated` answer
  state (ADR-0019's sibling work) with no producer.
* **Revocation has to reach superseded revisions.** A family is one item in the source
  repository and SharePoint permissions are on the item, not the revision. Scoping the ACL
  replacement to the newest document would leave somebody whose access was revoked still
  able to retrieve last year's version of the same policy through `include_historical`.

**Consequences.** `Sites.Selected` is the scope to ask the AUC for rather than
`Sites.Read.All`: app-only auth means the application can read everything the scope allows,
across every site, unrestricted by any user's permissions, so bounding it site by site
bounds the blast radius of a misconfiguration. The connector is untested against a live
tenant — every test drives a mock transport — so the first real sync should be against one
library with a known permission set, compared row by row against `document_acl` before a
wider rollout.

**Revisit if:** the AUC needs SharePoint site groups honoured, which means expanding their
membership through Graph and is a feature rather than a mapping; or if delta queries become
necessary for library size, which changes enumeration but not any of the above.

---

### ADR-0023 — Evaluation runs are recorded, with what makes them comparable
`accepted`

**Context.** The harness could score a run and fail a build on it. It could not answer the
question anybody actually asks after a change: *did this make retrieval worse?* A score with
nothing to compare against is a number, not a measurement. `eval_runs` and `eval_results`
have existed since the first migration and nothing had ever written to them.

**Decision.** Both entry points — `POST /api/v1/evaluation/runs` and
`python -m askau.evaluation.cli` — record the run. The CLI matters most: it is what CI uses,
and a quality history missing the pipeline's own runs would have a hole exactly where the
regressions are.

**Four fields decide whether two runs can be compared at all**, and they are why the table is
useful rather than merely full: `git_sha`, `model`, `prompt_hash`, `retriever`. A drop in
mean reciprocal rank means something if those match the previous run and nothing whatever if
they do not — comparing a `bge-m3` run against a hash-embedding run yields a confident,
meaningless regression, which is worse than no measurement because somebody will act on it.

`prompt_hash` earns its place least obviously and matters most in practice: the system prompt
is a *file* that ships with the wheel. It can change with no commit that looks related to
retrieval, and the result is a quality shift with no apparent cause. Recording it turns that
into a one-line diff.

`git_sha` is read from `.git` rather than by running `git` — a deployed container often has
neither the binary nor the repository, and shelling out for a string sitting in a file is a
subprocess and a failure mode for nothing. It is `NULL` when genuinely unknown, rather than
the word "unknown" written where a revision belongs.

**What is deliberately not stored.** `actual_answer` stays NULL for curated questions: the
answer is reproducible by re-running the question, and keeping generated text for every
question of every run would grow the table steadily without recording a fact the scores do
not already carry.

**Questions are upserted, not inserted.** Keyed on the harness's own id, so correcting a
question's wording keeps its history — a renamed question starting a fresh series would read
as a fixed bug and a new failure at the same time. It also puts the curated datasets and the
questions sampled from real use (ADR-0020) in one table, which is what lets a later run be
scored over both.

**Recording is never fatal.** A measurement that cannot be saved is still a measurement, and
a full disk surfaced as a red build looks exactly like the retrieval regression this table
exists to detect. The endpoint returns a null `run_id` and the CLI says so on stdout, rather
than either pretending the run was stored or failing the gate.

**Consequences.** `GET /api/v1/evaluation/runs` returns the series with each run's provenance
beside its metrics, so the comparison can be made without a second lookup. Neither this nor
ADR-0020's sampling adds a semantic judge: `evaluation/runner.py` scopes that to Phase 2
because it needs a second model and a human calibration set, and an uncalibrated judge would
report confident nonsense about answer quality. A docstring in `rag/grounding.py` described
that judge as though it already existed; it has been corrected, because a comment claiming a
measurement the system does not take is worse than no comment.

**Revisit if:** the table grows past what a simple `ORDER BY started_at` can serve, or a
per-question trend view is wanted — both are indexes and a query, not a redesign.

---

### ADR-0024 — What a sync must notice besides changed content
`accepted`

**Context.** ADR-0022 gave documents their own access lists. Testing what a *second* sync
does found three defects, all of them things the first sync could not have revealed. They
share a shape: the pipeline was built around "has the content changed", and three of the
four things a repository actually does are invisible to a content hash.

**1. A permission changed and the content did not.** The most common change a document
repository sees — somebody leaves a team, a library is re-shared, an over-broad grant is
tightened — alters no byte of any file. `_unchanged` short-circuited before the access list
was written, so the revocation never landed.

The failure was permanent and silent, and the silence is the worst part: `document_acl`
stayed stale, and `ingestion/acl_reconciler.py` faithfully copied the stale value into
`chunks.acl_principals`. The denormalised copy agreed with the authoritative table, every
consistency check passed, and the revoked grantee kept access until somebody happened to
edit the document.

Fixed by refreshing the access list on unchanged content whenever the source carries
per-item permissions — and refreshing the chunk array in the same transaction, because
`document_acl` is authoritative but the array is what the authorization predicate reads.
Writing one without the other leaves the predicate answering from the old audience.

**2. A document was removed at the source.** Nothing marked it. For a policy assistant that
is not housekeeping: a rescinded policy stays in the corpus, keeps being retrieved, and is
cited in a form indistinguishable from a live one. Documents absent from a sync are now
`expired` and their chunks leave `is_current`.

Expired, not deleted — the document was real and was cited, an audit row naming it must
still resolve, and FR-018 makes historical content retrievable on request.

**The safety condition is the whole of this feature.** "Absent from this run" and "deleted
at the source" are the same observation, and the same *fact* only when the run finished. A
sync that died halfway would otherwise expire everything it had not reached, which is most
of the corpus — the most destructive possible outcome, triggered by the most ordinary
possible failure. An empty enumeration also expires nothing: a library that legitimately
returns zero items is indistinguishable from a misconfigured one that lists nothing, and the
two demand opposite actions, so this declines to guess.

**3. The enumeration failed and the run stayed `running` for ever.** `_close_run` was
unreachable when `documents()` raised, and `_start_run` refuses a second run while one is in
progress. So one exhausted Graph throttle, one dropped connection, one expired secret, and
**the source could never be synced again** without somebody editing the database by hand.
`failed` had been in the status constraint since the first migration with nothing writing it.

The partial counts are kept rather than zeroed: a run that indexed nine hundred documents
before dying did index nine hundred documents, and that is what an operator needs to decide
between retrying and investigating. The stored error carries the exception type and message,
never a traceback — it is read back through the admin API.

**A note on mutation testing.** The `complete` flag guarding expiry initially could not be
made to fail: an exception from `documents()` meant execution never reached the expiry call
at all, so the flag was dead code and control flow was doing the work. That is robust until
somebody wraps the loop in a `try` to "make sync resilient" — which is exactly the
well-meaning change that would cause the disaster. Fixing defect 3 required that `try`, which
made the flag load-bearing and testable. Both now fail under mutation.

**Consequences.** Permissions are read for every document on every sync, because there is no
way to detect a permission change without reading it — one Graph call per document per sync.
That is the cost of revocations landing at all, and for a large library it will need Graph's
`$batch` (20 requests per call). Not done: it is an optimisation, and optimising against a
mock rather than a real library would be guesswork.

**Revisit if:** sync duration on the AUC's real libraries makes the per-item permission read
the bottleneck, which is a batching change and not a design one.

---

### ADR-0025 — The ACL reconciler was blind in production, and said nothing
`accepted`

**Context.** `AclReconciler` (FR-025) closes the window between `document_acl`, which is
authoritative, and `chunks.acl_principals`, the denormalised copy the authorization
predicate actually reads. It is well written, thoroughly tested, and its docstring names
`max_lag_seconds` as "the metric the runbook alerts on".

Three things were wrong, and they compounded.

**The metric was exposed nowhere.** The alert the docstring described could not exist.

**Nothing in `src/` ever called the reconciler.** Its standing obligation was discharged by
no scheduled work at all — only by the per-document reconcile the pipeline now performs
inline.

**And it cannot see chunks when run as the application role.** Row-level security on
`chunks` gives `askau_app` exactly one SELECT policy: an overlap against the session
principals. A maintenance pass sets none. So the reconciler read **zero chunks**, found no
drift, and returned `ReconcileReport(revocations=0, grants=0, ...)` — indistinguishable, in
every log and every metric, from a perfectly consistent corpus.

For the component whose entire job is closing revocation windows, a silent success means the
window never closes and nobody is told.

**Why the tests did not catch it.** Every test in `test_acl_reconciler.py` uses
`admin_engine`, the migration role, which RLS does not filter. The code is correct; the
privilege it would run under in production is wrong. A test suite cannot find that by
exercising the code — only by exercising it *as the identity that will run it*. Which is
exactly the discipline `test_rls_second_lock.py` already applies to retrieval and nobody had
applied here.

**Decision.**

*Refuse to run blind.* `BlindReconcilerError` when indexed documents exist and no chunk rows
are visible. The check is "documents exist but chunks are invisible", not "chunks is empty",
so a fresh deployment's first pass does not fail with a security error. The message names
both the cause and the fix, because whoever reads it has a failed maintenance job and no
other clue.

*Expose the drift.* Two gauges on `/metrics`: `askau_acl_drift_documents` and
`askau_acl_sync_lag_seconds`. The count matters more than the lag — a large lag with zero
disagreement is a quiet corpus, not an incident.

*Through a SECURITY DEFINER function* (migration 0015), because the naive query hits the same
RLS wall and *looks* like it works: both gauges read zero for ever, not because the corpus is
consistent but because the query sees nothing. A gauge that structurally cannot leave zero is
worse than no gauge, because somebody will trust it. Granting the app unfiltered SELECT on
`chunks` was the other available fix and would have deleted the second lock the security model
rests on; the function returns two integers and no rows, which is the contract `/metrics`
already keeps. `SET search_path` is not decoration — a SECURITY DEFINER function without it
can be made to resolve `chunks` to an attacker-controlled table.

**Also fixed while here.** The pipeline had grown a private `_refresh_chunk_acl` duplicating
`AclReconciler.reconcile_document`, and the copy silently dropped two things the original
does: stamping `acl_synced_at` (without which the refreshed chunks read as permanently stale
to the very lag metric being added) and bumping `users.acl_version` to invalidate cached
authorization. Duplication of a security routine is not a style problem — the copy diverges
in exactly the ways nobody checks.

**Consequences.** Reconciliation needs a privileged connection, which is now stated on the
class and enforced. Scheduling it remains unwired: it belongs to whoever operates the
deployment, and the drift gauge is what makes its absence visible rather than theoretical.

**Revisit if:** the platform team wants reconciliation in-process on a timer, at which point
the API would need a second engine on the owner role and that trade — a privileged connection
inside the request-serving process — deserves its own decision.

---

### ADR-0026 — `outdated` finally has a producer, for the second reason
`accepted`

**Context.** The web client renders a banner for the `outdated` answer state — *"Based on an
older approved document"* — and nothing in this codebase could reach it. ADR-0022 recorded
why: `documents.version_seq` defaulted to 1 and nothing set it, so no document ever became
`superseded`.

That was fixed. The banner still could not fire, for a second and independent reason that the
first had been hiding.

**Retrieval reads the *chunk's* lifecycle, not the document's.** `chunks` carries its own
`lifecycle` column, `RetrievedChunk.lifecycle` comes from there, and the citation's status —
the "Superseded" the interface displays — is derived from that. Both places that retire a
document (`_write` superseding an older revision, `_expire_absent` retiring a withdrawn one)
cleared `is_current` and left the chunk's lifecycle at `active`.

That is not a disclosure: `is_current` still holds the content out of default retrieval, and
the `VersionPolicy` predicate requires both. What it means is that material fetched with
`include_historical` comes back labelled *Active*, and `outdated_from_citations` promotes an
answer by looking for exactly that label. So the state existed, the mapping existed, the UI
branch existed, and the one value that triggers it was never written.

**Decision.** Both retirement paths set `lifecycle` alongside `is_current`. Asserted at the
level a reader would experience — the answer state, not the columns — because the columns were
already individually defensible and it was their relationship that was wrong.

**On method.** Two things in this pass are worth recording as much as the fix.

*A near-miss.* Checking whether expiry reached retrieval, a grep for `is_current` in the
retriever found nothing and the obvious conclusion was that expiry was decorative. It was
wrong: the predicate lives in `retrieval/policy.py` and is interpolated as `{version}`. The
correct move was to test the behaviour rather than trust the grep — the test passed, and the
finding above came out of following the same question one level further.

*A test that measured its own setup.* The first version of the retrieval test ingested one
document and deleted it, then asserted the document was no longer retrievable. It failed —
because an empty enumeration expires nothing by design (ADR-0024), so nothing had been
expired. The test was asserting against its own fixture. A second file that stays is what
makes it a test of expiry.

**Consequences.** `include_historical` now returns correctly labelled superseded and expired
material, so the client's banner can render and a reader can tell a historical citation from a
current one. Nothing else changes: default retrieval was already correct via `is_current`.

**Revisit if:** the chunk's copy of `lifecycle` and `classification` proves to drift in other
ways, at which point the question is whether chunks should carry them at all rather than join
— a partitioning decision (ADR-0002) and not a bug fix.

---

### ADR-0027 — Entra group claims become principals, and absence is not emptiness
`accepted`

**Context.** The authorization predicate intersects `chunks.acl_principals` with the caller's
principal set, and that set comes from `user_principals`. The only thing that ever wrote that
table was the seed script. The access token's `groups` claim was parsed into
`VerifiedIdentity.groups` and consumed by **nothing**.

So live Entra could not have worked, and no test tenant would have shown anything else: a real
sign-in either returned 401 (no AskAU account for that identity — deliberate) or, if the user
were inserted by hand, produced an empty principal set, which `AuthorizationContext` refuses
loudly by design. Found while answering a question about how to test against a real tenant,
which is a reminder that "can we test this" is sometimes the same question as "is it built".

**Decision.** `DirectorySync` reconciles one user's group memberships from their token's
claims, at sign-in. Three parts carry the weight.

**Removal, not just addition.** Somebody taken out of a group in Entra loses what it could
read at their next sign-in. An additive-only sync never revokes, and nothing about the result
looks wrong — the same defect as an access list that only grows.

**A missing claim is not an empty claim.** Entra omits `groups` entirely once a user belongs to
more than roughly 200 groups, sending `_claim_names` pointing at Graph instead. Collapsing that
into an empty tuple would strip every membership from the most heavily-permissioned people in
the organisation, silently, at sign-in. `VerifiedIdentity.groups` is therefore `tuple | None`:
`None` means "we were not told" and changes nothing; `()` means "told, and none" and revokes.
A one-line `claims.get("groups", ())` erases the distinction, which is why it is asserted at
the parsing layer as well as the reconciliation one.

**Only directory-sourced group memberships are touched.** Each user also holds a `user`-kind
principal — their own identity, which no group grants. Scoped by `principals.kind` so the self
principal is excluded by construction, and by `granted_via` so a manual or role-based grant is
not reconciled away. Neither filter alone is load-bearing; removing both is, and a test covers
that case rather than the two individually.

**A defect this introduced, and caught.** The uniqueness constraint on `principals` is
`(kind, external_id)`, not `external_id`. The first version always inserted `kind='group'`, so
a group the corpus already knew as a `department` got a **second** row — `grp-hr`,
`grp-finance` and `grp-legal` each ended up duplicated with the user a member of both. No
access changed, which is exactly what would have let it survive: the predicate intersects on
ids and the person held both. The data was wrong, counts inflated, and the next reconcile had
two rows competing for one external id. Groups are now resolved across every kind a group can
be, and comparison is by principal id rather than external id. `mypy` caught the second half of
it — an `int` tested against a `dict[str, int]`'s keys.

**Consequences.** A group change takes effect at the next sign-in, not immediately. Sessions
last eight hours, so that is the window. Closing it further means a Graph call per request,
which is a different trade and not one to make before there is evidence it matters. Failure to
reconcile never fails the sign-in: the person keeps the memberships they had, and a user with
none is refused rather than let through with an empty set. Membership changes are recorded on
the login audit event, because an authorization change should be legible without diffing two
snapshots of `user_principals`.

**Revisit if:** the AUC needs revocation to be immediate, which is a token-lifetime and
Conditional Access question before it is a code one.

---

## ADR-0028 — Provisioning is a command, not an endpoint
`accepted`

**Context.** ADR-0027 gave a signed-in user their group principals, but nothing anywhere
created the AskAU *account* those principals hang off. `INSERT INTO users` existed only in
`seed.py`. The absence was deliberate — a valid organisational token is not an AskAU account,
and `POST /auth/session` refuses an unknown identity on purpose — but the consequence was that
onboarding a real person meant hand-writing twenty lines of SQL across three tables. The first
version of that SQL, written into `docs/azure-setup.md`, had a mistake in it.

**Decision.** `make provision-user` (`askau.scripts.provision_user`). A command, run by a
person who has decided to grant access, not an endpoint anyone can reach.

The distinction is not ceremony. An endpoint invites just-in-time provisioning — mint an
account for whoever presents a valid token — which is precisely the design ADR-0027 rejected,
because it produces users whose department and access posture nobody chose. Keeping it out of
the API keeps that door shut while making the deliberate act repeatable.

**Three things it does that the hand-written SQL did not.**

*The self principal is keyed on the Entra object id.* `SharePointConnector` writes object ids
into `document_acl` for grants made to an individual. A friendly `usr-finance` here would mint
a second principal for the same person, and every individual grant in the corpus would match
an identity nobody holds — with no error, because an ACL matching nobody is a valid ACL.

*It resolves before it inserts.* Ingestion can arrive first: a document shared with somebody
mints their principal long before anyone provisions their account. The same duplicate-principal
bug ADR-0027 hit, in the other direction.

*It grants no group memberships.* Those belong to `DirectorySync`, which owns every row it
wrote and removes the ones the directory no longer claims. A group granted here would either be
swept away at first sign-in or escape the sweep and become an access grant no directory can
revoke. The self grant is written `granted_via = 'manual'` to sit outside that scope.

**Roles are additive.** Re-running with a shorter `--role` list does not revoke anything.
Revoking authority should be a deliberate act, not a side effect of retyping a command.

**It is audited.** `admin.user_provisioned`, category `administration`, recording whether the
account was created or updated and which roles were newly granted. Creating an account and
granting an admin role are security-relevant acts under BR-008. There is no organisational
actor for a shell command, so the row records `granted_by: cli` plus the operating-system
account rather than attributing the act to a user identity that did not perform it.

**Consequences.** Provisioning policy for the AUC is still open — an admin endpoint, a
directory import, or this. This commits to none of them; it removes the hand-written SQL and
leaves the decision where it belongs.

**A correction found by mutation.** The tests originally claimed `granted_via = 'manual'` was
what protected the self principal from `DirectorySync`. Flipping it to `entra_sync` did not
break them: the sync also filters by `principals.kind`, and a `user`-kind principal is out of
scope however it was granted. Only removing both guards loses the principal. The two are kept
as independent locks and the documentation now says so, rather than crediting one of them with
work the other was doing.

---

## ADR-0029 — Azure Blob Storage and Entra ID, and nothing else
`accepted`

**Context.** The document source moved three times in one afternoon: SharePoint, then
SharePoint-with-OneDrive, then Azure Files, then Azure Blob Storage. The AUC settled on **Azure
Blob Storage** for documents and **Microsoft Entra ID** for identity, and asked that everything
connecting to anything else be removed.

**Decision.** Removed: the SharePoint connector, the Graph client it used, the generic web
connector (built earlier the same day for `au.int`), and the `dms`/`s3`/`http` source types.
`filesystem` and `manual` stay — neither reaches another system; one reads a local export and
the other accepts direct uploads, and both are how the corpus is exercised in development.

Keeping unreachable adapters "in case" is not free. They compile, typecheck, get imported, get
tested, and get read by whoever comes next, who cannot tell from the code which ones are real.
The part of the SharePoint connector worth keeping was never the code — it was the reasoning
about permission mapping, which lives in this file and in git history.

**The problem Blob Storage creates that SharePoint did not.** SharePoint answers "who may read
this document?" — that is what a document management system is for. **Blob storage does not.**
Access is granted at the container by RBAC or a SAS, and every blob inside is equally reachable
by whoever holds that grant.

That matters more here than anywhere else, because the product's whole claim is that two people
asking the same question get different answers. Ingest a flat container under one grant and the
claim quietly becomes false — not with an error, but with every reader seeing everything.

So the audience is a **configured choice per source**, never a default:

* `metadata` — blob metadata carries a delimited list of Entra object ids. Per-document, set by
  whoever uploads.
* `prefix` — the blob's path matched against a `prefix_map`. A folder convention: `hr/` grants
  the HR group. The default recommendation — an administrator can see the whole access model by
  looking at the tree, and it asks nothing of the uploader.
* `fixed` — one list for the whole container.

A blob whose audience cannot be determined under the chosen strategy is **not ingested**. It
carries `access_error`, the run reports it, and an administrator sees "1 document could not be
assessed". Treating absent metadata as "everyone" is one line away and is the disclosure this
design exists to prevent. Verified against the emulator: three blobs, two labelled, one not —
two indexed with distinct audiences, one failed with an actionable message.

**No vendor SDK, and the linter is why.** The first draft imported `azure.storage.blob`; ruff
refused it, because `pyproject.toml` bans vendor SDKs outside `*/adapters/`. The rule was right.
`AzureStorageClient` speaks the REST API over the same `httpx` everything else uses, mirroring
the Graph client it replaces — no `azure-core`, `azure-identity` or `msal` in the worker image,
one HTTP client, and the same throttle handling.

**No account-key credential.** Service principal or SAS. An account key grants everything the
storage account can do, to anybody holding it, with no expiry and no audit trail — and once it
is in a `.env` file it is in a backup, a screenshot and a chat log.

**Consequences.** The `sharepoint`, `dms`, `s3` and `http` values remain in the PostgreSQL
`source_type` enum. PostgreSQL cannot remove an enum value without recreating the type and
rewriting every column using it, which is a large amount of risk to retire four strings nothing
can write any more. They are unreachable from the Python enum and from the API, which is what
matters; a later contract migration can drop them if it is ever worth the rewrite.
