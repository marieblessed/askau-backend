# Configuration

Every setting AskAU reads, what it is for, and where the value comes from.

All of them are environment variables prefixed `ASKAU_`, read from `api`'s `.env`
or from the real environment. `.env.example` is the documented shape — copy it and
edit. **Settings are validated at startup**: a malformed or contradictory value
stops the process with a message naming the variable, rather than surfacing twenty
minutes later as a confusing query error.

Only two have no default and must be set: `ASKAU_DATABASE_URL` and
`ASKAU_REDIS_URL`. Everything else runs on defaults chosen so that a fresh
checkout works with no model, no tenant and no cloud account.

---

## Core

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_ENV` | `development` | One of `development`, `test`, `uat`, `production`. Not a label: `production` **refuses** `AUTH_MODE=dev`, the `echo` generator and `hash` embeddings, because each is a stand-in that would silently degrade a real deployment. A common first-run error is setting this to `dev`, which is not one of the four. |
| `ASKAU_LOG_LEVEL` | `INFO` | Standard Python level name. |

## Database

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_DATABASE_URL` | **required** | The application's connection, as the `askau_app` role. Must be `postgresql+asyncpg://` — a sync driver would block the event loop and is rejected at startup. |
| `ASKAU_MIGRATION_DATABASE_URL` | falls back to the above | A role that can run DDL, normally `postgres`. Separate on purpose: `askau_app` is subject to row-level security on `chunks`, which would blind a maintenance pass. `make migrate`, `make seed` and `make reconcile-acls` use this one. |
| `ASKAU_DATABASE_RO_URL` | falls back to the above | An optional read replica. |
| `ASKAU_DB_POOL_SIZE` | `10` | Connections held open. |
| `ASKAU_DB_MAX_OVERFLOW` | `5` | Extra connections under load. |

**The two roles matter.** `askau_app` holds DML but no DDL, cannot write `chunks`
outside the policies granted in migration 0010, and is filtered by RLS. It is
created by migration `0004` with the password `askau` — change it for anything
beyond a laptop.

## Redis

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_REDIS_URL` | **required** | Caches authorization contexts and embeddings, and holds conversation state for readers who have turned history off. `redis://localhost:6379/3` locally — the `/3` is the database number, so it will not collide with another project on the same server. |

## Identity

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_AUTH_MODE` | `dev` | `dev` accepts locally-signed tokens for the seeded identities and needs no tenant. `entra` validates real Entra tokens. `dev` is refused when `ASKAU_ENV=production`. |
| `ASKAU_DEV_TOKEN_SECRET` | — | Required when `AUTH_MODE=dev`. Any string; it signs the tokens `make dev-token` mints. |
| `ASKAU_ENTRA_TENANT_ID` | — | Required when `AUTH_MODE=entra`. Entra admin centre → Overview → **Tenant ID**. |
| `ASKAU_ENTRA_CLIENT_ID` | — | The **API** app registration's Application (client) ID — the one that *exposes* the scope, not the one the browser signs in with. |
| `ASKAU_ENTRA_AUDIENCE` | — | Must equal the token's `aud` claim **exactly**; the verifier compares them literally. Normally `api://<API client id>`. If sign-in fails with a valid-looking token, decode it at jwt.ms and compare this value first. |

See [azure-setup.md](azure-setup.md) for how to obtain these.

## Azure Blob Storage (document ingestion)

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_AZURE_STORAGE_CLIENT_ID` | — | A **separate** app registration from the API's. This identity reads documents with nobody signed in; the other validates user tokens. One compromised secret should not be both. |
| `ASKAU_AZURE_STORAGE_CLIENT_SECRET` | — | Its client secret **Value** (not the Secret ID). Shown once. |
| `ASKAU_AZURE_STORAGE_SAS_TOKEN` | — | An alternative to the service principal, for the emulator or time-boxed testing. Either credential is sufficient; neither means the connector is unconfigured, and `test-connection` says so plainly. |

There is deliberately **no account-key setting**. An account key grants everything
the storage account can do, to anybody holding it, with no expiry and no audit
trail — and once it is in a `.env` file it is in a backup, a screenshot and a chat
log. The service principal needs only **Storage Blob Data Reader**, assigned on
the container.

## Embeddings

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_EMBEDDING_PROVIDER` | `hash` | `hash` \| `sentence_transformers` \| `openai_compatible`. `hash` is deterministic and offline: retrieval *runs*, but semantic quality is meaningless. Refused in production. |
| `ASKAU_EMBEDDING_MODEL` | `hash-1024` | e.g. `BAAI/bge-m3` for sentence-transformers. |
| `ASKAU_EMBEDDING_DIM` | `1024` | Must match the model. The HNSW indexes are built for this width. |
| `ASKAU_EMBEDDING_BASE_URL` | — | Required for `openai_compatible`. |
| `ASKAU_EMBEDDING_API_KEY` | — | Omit for a local endpoint that needs none. |

**Changing the model means re-seeding.** A query embedded by one model cannot be
compared against vectors written by another — they are points in unrelated spaces
— and the failure is silent: retrieval returns noise shaped like results. Install
the extra with `pip install -e ".[embeddings]"`; `make setup` leaves it out because
`sentence-transformers` pulls torch and takes the virtual environment past a
gigabyte.

## Answer generation

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_LLM_PROVIDER` | `echo` | `echo` composes an answer from retrieved passages with no model — enough to exercise the whole pipeline, and refused in production. `openai_compatible` is any endpoint speaking the OpenAI chat API: Ollama, vLLM, llama.cpp, a hosted provider. |
| `ASKAU_LLM_MODEL` | `echo-1` | e.g. `qwen2.5:3b`. |
| `ASKAU_LLM_BASE_URL` | — | Required for `openai_compatible`, and validated at startup. Ollama: `http://localhost:11434/v1`. |
| `ASKAU_LLM_API_KEY` | — | Omit for Ollama. |
| `ASKAU_LLM_MAX_TOKENS` | `800` | Ceiling on the generated answer. |

A small model will produce fluent, plausible text that its sources do not support.
The grounding check measures support and downgrades the answer, but it is lexical
— see `docs/architecture/15-decision-records.md` — so it cannot catch a fabrication
that reuses the source's own vocabulary. Model choice is a safety decision here,
not only a quality one.

## Retrieval

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_RETRIEVER` | `pgvector_hybrid` | The only implemented adapter. |
| `ASKAU_RETRIEVAL_CANDIDATE_K` | `60` | Candidates fetched per arm before fusion. |
| `ASKAU_RETRIEVAL_TOP_K` | `8` | Chunks passed to the model. Cannot exceed candidate_k. |
| `ASKAU_RERANKER` | `noop` | `noop` \| `cross_encoder`. |
| `ASKAU_RERANK_INPUT_K` | `40` | Candidates handed to the reranker. |
| `ASKAU_RRF_K` | `60` | Reciprocal-rank-fusion constant. Scores are positional, not similarity. |
| `ASKAU_MIN_PGVECTOR_VERSION` | `0.8.0` | **Asserted at startup.** Below 0.8 an access-filtered vector search silently under-returns — the query succeeds, the answer is thinner, nothing errors. Because that failure is invisible the application refuses to start. |
| `ASKAU_HNSW_ITERATIVE_SCAN` | `relaxed_order` | The pgvector setting that makes filtered ANN return enough rows. |
| `ASKAU_HNSW_MAX_SCAN_TUPLES` | `20000` | Ceiling on that scan. |

## Answer quality gates

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_MIN_EVIDENCE_SCORE` | `0.015` | Below this the top result is "only weakly related". Compared against RRF scores, which are positional. |
| `ASKAU_MIN_EVIDENCE_CHUNKS` | `1` | Supporting chunks required. |
| `ASKAU_GROUNDEDNESS_FLOOR` | `0.40` | Below this the answer is refused outright rather than shown. Between it and 0.75 the answer is `partially_grounded` and the interface shows a caveat. |
| `ASKAU_CONTEXT_TOKEN_BUDGET` | `6000` | Context assembled per question. |

## Caching and limits

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_AUTHZ_CACHE_TTL` | `300` | Seconds an authorization context is cached. Invalidated early by `users.acl_version`, which every membership change bumps — so a revocation does not wait for expiry. |
| `ASKAU_EMBED_CACHE_TTL` | `86400` | Seconds a query embedding is cached. |
| `ASKAU_RATE_LIMIT_PER_MINUTE` | `30` | Per reader. |
| `ASKAU_RATE_LIMIT_PER_HOUR` | `300` | Per reader. |

## Ingestion

| Variable | Default | What it is |
|---|---|---|
| `ASKAU_CHUNK_TARGET_TOKENS` | `380` | Aimed-for chunk size. |
| `ASKAU_CHUNK_OVERLAP_TOKENS` | `60` | Must be smaller than the target, or chunking cannot progress. Checked at startup. |
| `ASKAU_CHUNK_MAX_TOKENS` | `512` | Hard ceiling. |
| `ASKAU_OCR_PROVIDER` | `none` | `none` \| `tika`. `none` rejects scanned documents with an actionable message rather than indexing them as empty. |
| `ASKAU_OCR_URL` | — | Required when the provider is not `none`. A Tika container, never a host install. |
| `ASKAU_OCR_LANGUAGES` | `eng` | Comma-separated. `amh` and `ara` are not in a minimal image; declaring them is what makes their absence detectable. |
| `ASKAU_ACL_SYNC_INTERVAL_SECONDS` | `900` | Advisory. Nothing schedules the reconciler — run `make reconcile-acls`. |

---

## The shortest working configuration

A laptop, no cloud, no model:

```
ASKAU_ENV=development
ASKAU_DATABASE_URL=postgresql+asyncpg://askau_app:askau@localhost:5433/askau
ASKAU_MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/askau
ASKAU_REDIS_URL=redis://localhost:6379/3
ASKAU_AUTH_MODE=dev
ASKAU_DEV_TOKEN_SECRET=anything-at-all
```

Everything else defaults. That runs the whole pipeline — retrieval, the evidence
gate, citation validation, the authorization predicate — with no credentials.
