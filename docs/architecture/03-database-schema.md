# AskAU — Database Schema (Phase 1)

PostgreSQL 16 + pgvector ≥ 0.8. Three data domains per SRS §4.1 — **knowledge**,
**identity**, **conversation** — plus **operations** (audit, ingestion, model usage) and
**evaluation**.

Conventions: `snake_case`; UUIDv7 surrogate keys for externally-visible entities
(time-ordered → better index locality than v4); `BIGINT` identities where the value
appears inside hot-path arrays or high-volume rows; `timestamptz` always, UTC always;
soft-delete only where audit requires it.

---

## 0. Extensions & enums

```sql
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector >= 0.8
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- fuzzy title/acronym match
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- SRS §4.1 access-tiered classification
CREATE TYPE classification AS ENUM ('public','internal','confidential','highly_restricted');
CREATE TYPE lifecycle_status AS ENUM ('draft','active','review_required','expired','superseded');
CREATE TYPE ingest_status  AS ENUM ('pending','fetching','extracting','chunking','embedding',
                                    'indexed','failed','quarantined','skipped_unchanged');
CREATE TYPE principal_kind AS ENUM ('user','group','role','department');
CREATE TYPE source_type    AS ENUM ('sharepoint','dms','filesystem','s3','http','manual');
CREATE TYPE source_status  AS ENUM ('draft','active','paused','error','archived');
CREATE TYPE message_role   AS ENUM ('user','assistant');
CREATE TYPE answer_state   AS ENUM ('grounded','partially_grounded','conflict',
                                    'insufficient_evidence','clarification_needed',
                                    'out_of_scope','refused_safety','error');
CREATE TYPE feedback_rating AS ENUM ('helpful','not_helpful');
CREATE TYPE audit_outcome  AS ENUM ('success','failure','denied');
```

`answer_state` is a first-class column, not a derived flag. Every SRS trust
requirement — refusal (FR-028), conflict (FR-035), out-of-scope (FR-009),
clarification (FR-008) — becomes a measurable rate rather than a prose behavior.

---

## 1. Identity & authorization

```sql
-- Unified principal namespace. BIGINT because these IDs live inside
-- chunks.acl_principals arrays on the hot path (see §3).
CREATE TABLE principals (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind           principal_kind NOT NULL,
    external_id    TEXT NOT NULL,              -- Entra objectId / group id / role name
    display_name   TEXT NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    synced_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, external_id)
);

CREATE TABLE users (
    id             UUID PRIMARY KEY DEFAULT uuidv7(),
    principal_id   BIGINT NOT NULL UNIQUE REFERENCES principals(id),
    entra_oid      TEXT NOT NULL UNIQUE,
    email          CITEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    department     TEXT,
    job_title      TEXT,
    preferred_language TEXT NOT NULL DEFAULT 'en',
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    -- bumped on any membership change; invalidates cached authz + answer cache
    acl_version    INTEGER NOT NULL DEFAULT 1,
    last_login_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Flattened group/role membership (transitive closure resolved at sync time,
-- so request time never walks a graph).
CREATE TABLE user_principals (
    user_id        UUID   NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    principal_id   BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    granted_via    TEXT   NOT NULL DEFAULT 'entra_sync',
    synced_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, principal_id)
);
CREATE INDEX ON user_principals (principal_id);

-- Application roles (FR-002a, §2.5). Separate from Entra groups so AskAU-specific
-- admin authority is explicit and auditable.
CREATE TABLE app_role_assignments (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('end_user','knowledge_admin',
                                             'system_admin','security_admin')),
    granted_by UUID REFERENCES users(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role)
);

CREATE TABLE sessions (
    id           UUID PRIMARY KEY DEFAULT uuidv7(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    ip_hash      BYTEA,                        -- hashed, not stored raw (NFR-005)
    user_agent   TEXT
);
CREATE INDEX ON sessions (user_id) WHERE revoked_at IS NULL;
```

**Why `acl_version` on the user row:** the authorization cache and the answer cache
must be invalidated the instant a membership changes, and a version counter does that
in one `UPDATE` without a cache-wide sweep. It is the cheap half of solving FR-025.

---

## 2. Knowledge sources & documents

```sql
CREATE TABLE knowledge_sources (
    id                UUID PRIMARY KEY DEFAULT uuidv7(),
    name              TEXT NOT NULL UNIQUE,
    source_type       source_type NOT NULL,
    description       TEXT,
    -- governance (FR-011, FR-045, BR-002)
    business_owner_id UUID NOT NULL REFERENCES users(id),
    department        TEXT NOT NULL,
    default_classification classification NOT NULL,
    -- connector config: site URL, library, credentials ref (Key Vault path only)
    location          JSONB NOT NULL,
    access_rules      JSONB NOT NULL DEFAULT '{}',   -- declarative ACL mapping
    sync_cron         TEXT,                           -- NULL = manual only
    status            source_status NOT NULL DEFAULT 'draft',
    approved_by       UUID REFERENCES users(id),      -- BR-001: no ingest without approval
    approved_at       timestamptz,
    last_sync_at      timestamptz,
    last_sync_status  TEXT,
    next_sync_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT active_requires_approval
        CHECK (status <> 'active' OR approved_by IS NOT NULL)
);
CREATE INDEX ON knowledge_sources (status, next_sync_at) WHERE status = 'active';
```

That `CHECK` constraint is BR-001 (*only approved sources may be indexed*) enforced by
the database rather than by application discipline. Business rules that map to a
constraint belong in a constraint.

```sql
-- A document *family* groups all versions of the same logical document, so
-- version-aware retrieval (FR-017) is a predicate, not a join.
CREATE TABLE document_families (
    id            UUID PRIMARY KEY DEFAULT uuidv7(),
    source_id     UUID NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    external_key  TEXT NOT NULL,               -- stable id in the source repository
    canonical_title TEXT NOT NULL,
    current_document_id UUID,                  -- FK added after documents exists
    UNIQUE (source_id, external_key)
);

CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    family_id       UUID NOT NULL REFERENCES document_families(id) ON DELETE CASCADE,
    source_id       UUID NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,

    title           TEXT NOT NULL,
    doc_type        TEXT,                       -- policy · procedure · SOP · circular …
    language        TEXT NOT NULL DEFAULT 'en',
    source_uri      TEXT NOT NULL,              -- resolvable link to the authoritative original (BR-004)
    mime_type       TEXT NOT NULL,
    byte_size       BIGINT,
    page_count      INTEGER,
    content_hash    BYTEA NOT NULL,             -- sha256; drives skip-unchanged

    -- governance & access
    classification  classification NOT NULL,
    owner_user_id   UUID REFERENCES users(id),
    department      TEXT,

    -- versioning (FR-017, FR-018)
    version_label   TEXT,                       -- "Rev 3", "2024-A"
    version_seq     INTEGER NOT NULL DEFAULT 1,
    lifecycle       lifecycle_status NOT NULL DEFAULT 'draft',
    supersedes_id   UUID REFERENCES documents(id),
    published_at    DATE,
    effective_from  DATE,
    effective_to    DATE,

    -- ingestion state
    ingest_status   ingest_status NOT NULL DEFAULT 'pending',
    ingest_error    JSONB,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    indexed_at      timestamptz,
    last_synced_at  timestamptz,
    -- FR-036 ingest-time injection screening
    injection_risk  SMALLINT NOT NULL DEFAULT 0,   -- 0..100
    review_required BOOLEAN NOT NULL DEFAULT FALSE,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT valid_effective_window
        CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
    UNIQUE (family_id, version_seq)
);
ALTER TABLE document_families
    ADD CONSTRAINT fk_current_doc FOREIGN KEY (current_document_id) REFERENCES documents(id);

CREATE INDEX ON documents (source_id, ingest_status);
CREATE INDEX ON documents (family_id, version_seq DESC);
CREATE INDEX ON documents (lifecycle) WHERE lifecycle IN ('active','review_required');
CREATE INDEX ON documents USING gin (title gin_trgm_ops);
CREATE INDEX ON documents (review_required) WHERE review_required;

-- Authoritative ACL. chunks.acl_principals is *derived* from this and never edited directly.
CREATE TABLE document_acl (
    document_id  UUID   NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    principal_id BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    source_of_truth TEXT NOT NULL DEFAULT 'source_repository',  -- vs 'askau_override'
    synced_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, principal_id)
);
CREATE INDEX ON document_acl (principal_id);
```

---

## 3. Chunks — the retrieval hot path

This is the only table whose shape is driven by latency rather than by normalization.

```sql
CREATE TABLE chunks (
    id             BIGINT GENERATED ALWAYS AS IDENTITY,
    document_id    UUID   NOT NULL,
    family_id      UUID   NOT NULL,
    ordinal        INTEGER NOT NULL,

    content        TEXT   NOT NULL,
    token_count    SMALLINT NOT NULL,

    -- citation anchors (FR-015, FR-029): what makes "page 12, §4.3" possible
    heading_path   TEXT[]  NOT NULL DEFAULT '{}',
    section_ref    TEXT,
    page_from      SMALLINT,
    page_to        SMALLINT,
    char_start     INTEGER,
    char_end       INTEGER,

    -- ── DENORMALIZED from documents: eliminates all joins at query time ──
    classification classification NOT NULL,
    lifecycle      lifecycle_status NOT NULL,
    department     TEXT,
    effective_from DATE,
    effective_to   DATE,
    version_seq    INTEGER NOT NULL,
    is_current     BOOLEAN NOT NULL DEFAULT TRUE,
    acl_principals BIGINT[] NOT NULL,        -- materialized from document_acl

    embedding      vector(1024),
    -- NOT a generated column: the text-search configuration depends on the document's
    -- language, and a generated column cannot select one per row. Written by the
    -- ingestion pipeline, which knows the language. See §3.5.
    lang_config    regconfig NOT NULL DEFAULT 'simple',
    tsv            tsvector NOT NULL,

    acl_synced_at  timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (id, classification),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
) PARTITION BY LIST (classification);

CREATE TABLE chunks_public       PARTITION OF chunks FOR VALUES IN ('public');
CREATE TABLE chunks_internal     PARTITION OF chunks FOR VALUES IN ('internal');
CREATE TABLE chunks_confidential PARTITION OF chunks FOR VALUES IN ('confidential');
CREATE TABLE chunks_restricted   PARTITION OF chunks FOR VALUES IN ('highly_restricted');

-- Per-partition indexes. m/ef_construction tuned for recall@10 ≥ 0.95 at this corpus size.
DO $$ DECLARE p text;
BEGIN
  FOREACH p IN ARRAY ARRAY['chunks_public','chunks_internal',
                           'chunks_confidential','chunks_restricted'] LOOP
    EXECUTE format('CREATE INDEX ON %I USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 128)', p);
    EXECUTE format('CREATE INDEX ON %I USING gin (acl_principals)', p);
    EXECUTE format('CREATE INDEX ON %I USING gin (tsv)', p);
    EXECUTE format('CREATE INDEX ON %I (document_id, ordinal)', p);
    -- partial index for the dominant case: current, non-expired content
    EXECUTE format('CREATE INDEX ON %I USING gin (acl_principals)
                    WHERE is_current AND lifecycle = ''active''', p);
  END LOOP;
END $$;

-- Defense in depth: even a hand-written query that forgets the ACL predicate
-- cannot read unauthorized rows. Belt and braces for BR-005.
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY chunk_acl_read ON chunks FOR SELECT TO askau_app
  USING (acl_principals && current_setting('askau.principals', true)::bigint[]);
```

### Why these four choices

| Choice | Rationale | Accepted cost |
|---|---|---|
| `acl_principals BIGINT[]` on the chunk | Replaces a 3-table join with one GIN overlap test — the single largest latency win in the design | Must be reconciled when ACLs change; FR-025 explicitly allows a sync window |
| LIST partition on `classification` | Most users are `public`+`internal` only → planner prunes 2 partitions; smaller HNSW graphs; structural containment of restricted content | Classification change = row move between partitions (rare, handled by reindex) |
| `is_current` denormalized | FR-017/018 version filtering becomes a boolean predicate instead of a correlated subquery over `document_families` | Reconciled on every supersede |
| `tsv` written at ingest, not generated | Keyword retrieval (FR-020) lives beside the vector, enabling in-database RRF — and each document is stemmed in **its own** language | ~15% storage overhead; the pipeline must set it, so a bug there is silent |

### 3.5 Language handling — a defect worth naming

The obvious form of the keyword column is
`tsvector GENERATED ALWAYS AS (to_tsvector('english', content))`. That is wrong for AUC
and it fails silently.

The Commission works in Arabic, English, French, Kiswahili, Portuguese and Spanish, and
is headquartered in a country using Ethiopic script. Stemming a French policy with the
English configuration does not error — it simply produces poor tokens, so the keyword arm
quietly under-performs on every non-English document while the semantic arm masks the
loss. Nobody sees a failure; retrieval is just worse.

**Resolution.** The text-search configuration is chosen per document:

| Document language | Postgres configuration | Notes |
|---|---|---|
| English, French, Portuguese, Spanish | `english`, `french`, `portuguese`, `spanish` | Shipped with Postgres, full stemming |
| Arabic | `arabic` | Shipped; verify stemmer quality against real AUC content |
| Kiswahili, Amharic / Ethiopic | `simple` | **No Postgres stemmer exists.** Tokenizes without stemming; the `pg_trgm` index carries fuzzy matching, and the semantic arm carries the rest |
| Unknown / mixed | `simple` | Safe default — never mis-stem |

This forces three further decisions that would otherwise be discovered late:

1. **The embedding model must be multilingual.** `bge-m3` or `multilingual-e5-large`,
   not an English-first model. This is what makes cross-lingual retrieval work at all —
   an English question surfacing a French policy — and the keyword arm cannot do it.
2. **The query is stemmed with the *asker's* configuration, the document with its own.**
   Cross-lingual keyword matching is not achievable, by construction. The hybrid design
   absorbs this: keyword handles exact identifiers (which are language-neutral anyway —
   circular numbers, acronyms), semantic handles meaning across languages.
3. **Answer language must be explicit in the prompt.** Answer in the user's language even
   when the cited source is in another, and never silently translate a quoted policy
   passage — a translated quote is no longer verifiable against the authoritative
   document (BR-004).

`documents.language` already exists in the schema but nothing populated it; ingestion now
detects it, stores it, and derives `chunks.lang_config` from it.

**Still open for AUC** (§6.7 leaves i18n undefined): which languages are in scope for the
pilot, and whether Ethiopic-script content is in the initial corpus. The schema handles
all of them; the question is which to test and resource. `simple` is a correct, safe
default for anything undecided.

### Row Level Security is a second lock, not the first

The application always passes the ACL predicate explicitly. RLS exists because the
consequence of one forgotten predicate is an unauthorized-disclosure incident, and
"unauthorized retrieval = 0" is a stated acceptance metric (§6.8). Two independent
mechanisms must fail simultaneously for a leak.

---

## 4. Conversations

```sql
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT,                       -- auto-summarized from first turn
    message_count   INTEGER NOT NULL DEFAULT 0,
    last_message_at timestamptz,
    is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- FR-006: retention configurable per AUC policy; row expires, not the user
    purge_after     timestamptz
);
CREATE INDEX ON conversations (user_id, last_message_at DESC) WHERE NOT is_archived;
CREATE INDEX ON conversations (purge_after) WHERE purge_after IS NOT NULL;

CREATE TABLE messages (
    id              UUID NOT NULL DEFAULT uuidv7(),
    conversation_id UUID NOT NULL,
    user_id         UUID NOT NULL,              -- denormalized: enables per-user purge
    role            message_role NOT NULL,
    seq             INTEGER NOT NULL,
    content         TEXT NOT NULL,

    -- assistant-only telemetry
    answer_state    answer_state,
    model_provider  TEXT,
    model_name      TEXT,
    retrieval_strategy TEXT,
    retrieved_count SMALLINT,
    reranked_count  SMALLINT,
    groundedness    NUMERIC(4,3),               -- 0..1, from grounding.py
    ttft_ms         INTEGER,                    -- NFR-002a measurement
    total_ms        INTEGER,                    -- NFR-002b measurement
    -- token counts are NOT here: see model_invocations below. One answer may invoke
    -- a model up to four times, so a per-answer total is a rollup, not a fact.
    cache_hit       BOOLEAN NOT NULL DEFAULT FALSE,
    correlation_id  UUID NOT NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at),
    UNIQUE (conversation_id, seq, created_at)
) PARTITION BY RANGE (created_at);
-- monthly partitions, created ahead by ops/sql/partition_maintenance.sql
CREATE INDEX ON messages (conversation_id, seq);
CREATE INDEX ON messages (user_id, created_at DESC);

-- FR-029/030: every citation is a row pointing at a chunk that was actually retrieved.
-- A citation with no row cannot be rendered, which is how "no fabricated citations"
-- becomes structurally true rather than prompt-dependent.
CREATE TABLE citations (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id     UUID NOT NULL,
    message_created_at timestamptz NOT NULL,
    chunk_id       BIGINT NOT NULL,
    chunk_classification classification NOT NULL,
    document_id    UUID NOT NULL REFERENCES documents(id),
    marker         SMALLINT NOT NULL,           -- the [1], [2] the user sees
    rank           SMALLINT NOT NULL,
    retrieval_score NUMERIC(6,5),
    rerank_score   NUMERIC(6,5),
    quote          TEXT,                        -- exact supporting span
    page_from      SMALLINT,
    page_to        SMALLINT,
    section_ref    TEXT,
    verified       BOOLEAN NOT NULL DEFAULT FALSE,   -- set by CitationValidator
    FOREIGN KEY (message_id, message_created_at) REFERENCES messages(id, created_at)
        ON DELETE CASCADE,
    FOREIGN KEY (chunk_id, chunk_classification) REFERENCES chunks(id, classification)
);
CREATE INDEX ON citations (message_id);
CREATE INDEX ON citations (document_id);

CREATE TABLE message_feedback (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id  UUID NOT NULL,
    message_created_at timestamptz NOT NULL,
    user_id     UUID NOT NULL REFERENCES users(id),
    rating      feedback_rating NOT NULL,
    -- FR-044 closed vocabulary → aggregatable, unlike free text
    reason_code TEXT CHECK (reason_code IN ('incorrect_answer','wrong_source',
                    'outdated_information','missing_information','not_relevant',
                    'unclear','other')),
    comment     TEXT,
    triaged_at  timestamptz,
    triaged_by  UUID REFERENCES users(id),
    resolution  TEXT,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, user_id),
    FOREIGN KEY (message_id, message_created_at) REFERENCES messages(id, created_at)
        ON DELETE CASCADE
);
```

---

## 5. Operations: audit, ingestion, model usage

```sql
-- FR-051/052. Append-only: no UPDATE/DELETE grant for the app role.
CREATE TABLE audit_events (
    id             BIGINT GENERATED ALWAYS AS IDENTITY,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    correlation_id UUID NOT NULL,
    event_type     TEXT NOT NULL,       -- auth.login · query.submitted · retrieval.performed
                                        -- document.opened · admin.source_created
                                        -- security.access_denied · ai.injection_detected
    event_category TEXT NOT NULL CHECK (event_category IN
                     ('authentication','authorization','query','retrieval',
                      'document_access','administration','configuration',
                      'security','ai_safety','ingestion')),
    outcome        audit_outcome NOT NULL,
    actor_user_id  UUID,
    actor_email    CITEXT,
    resource_type  TEXT,
    resource_id    TEXT,
    ip_hash        BYTEA,
    user_agent     TEXT,
    -- FR-052: metadata only. Never question or answer text.
    detail         JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE INDEX ON audit_events (actor_user_id, occurred_at DESC);
CREATE INDEX ON audit_events (event_category, occurred_at DESC);
CREATE INDEX ON audit_events (correlation_id);
CREATE INDEX ON audit_events (event_type, occurred_at DESC)
    WHERE event_category IN ('security','ai_safety');

CREATE TABLE ingestion_runs (
    id            UUID PRIMARY KEY DEFAULT uuidv7(),
    source_id     UUID NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    trigger       TEXT NOT NULL CHECK (trigger IN ('schedule','manual','webhook','reindex')),
    triggered_by  UUID REFERENCES users(id),
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','completed','failed','cancelled','partial')),
    docs_discovered INTEGER NOT NULL DEFAULT 0,
    docs_processed  INTEGER NOT NULL DEFAULT 0,
    docs_skipped    INTEGER NOT NULL DEFAULT 0,
    docs_failed     INTEGER NOT NULL DEFAULT 0,
    chunks_written  INTEGER NOT NULL DEFAULT 0,
    embedding_tokens BIGINT NOT NULL DEFAULT 0,   -- maintained rollup of model_invocations,
                                                  -- written once at run completion
    error         JSONB,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
CREATE INDEX ON ingestion_runs (source_id, started_at DESC);

-- Per-document checkpoint: this is what makes a failed run resumable (FR-049, FR-050)
CREATE TABLE ingestion_tasks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    external_key TEXT NOT NULL,
    stage       ingest_status NOT NULL DEFAULT 'pending',
    attempts    SMALLINT NOT NULL DEFAULT 0,
    error_code  TEXT,
    error_detail JSONB,
    stage_timings JSONB NOT NULL DEFAULT '{}',   -- {extract_ms, chunk_ms, embed_ms}
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ingestion_tasks (run_id, stage);
CREATE INDEX ON ingestion_tasks (stage) WHERE stage = 'failed';

-- FR-053: token consumption monitoring.
--
-- One row per MODEL CALL, not per answer. A single grounded answer may invoke a model
-- up to four times — query understanding, query embedding, reranking, generation — and
-- recording them separately is the difference between knowing "answers are slow" and
-- knowing "reranking is slow". Per-answer totals are derived from this rather than
-- stored on `messages`, which would hold the same numbers at a coarser grain.
CREATE TABLE model_invocations (
    id            BIGINT GENERATED ALWAYS AS IDENTITY,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    operation     TEXT NOT NULL CHECK (operation IN
                    ('understand','embed_query','embed_document','rerank','chat')),
    provider      TEXT NOT NULL,             -- vllm · azure_openai · ollama · echo
    model         TEXT NOT NULL,

    -- attribution: whichever applies to what triggered the call
    message_id       UUID,                   -- set for query-time operations
    ingestion_run_id UUID REFERENCES ingestion_runs(id) ON DELETE SET NULL,
    user_id          UUID REFERENCES users(id) ON DELETE SET NULL,
    department       TEXT,                   -- denormalized: §7.4 per-directorate rollup
                                             -- without joining users on every report

    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    cached        BOOLEAN NOT NULL DEFAULT FALSE,   -- served from cache: real latency,
                                                    -- zero token consumption
    outcome       TEXT NOT NULL DEFAULT 'success'
                  CHECK (outcome IN ('success','error','timeout')),

    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);        -- monthly, same maintenance as audit_events

CREATE INDEX ON model_invocations (occurred_at DESC, operation, model);
CREATE INDEX ON model_invocations (department, occurred_at DESC);
CREATE INDEX ON model_invocations (message_id);
CREATE INDEX ON model_invocations (ingestion_run_id) WHERE ingestion_run_id IS NOT NULL;
```

Three notes, because this is the table most likely to become a liability.

**It is written asynchronously.** Up to four inserts per answer on the hot path would add
measurable latency for data nobody reads in real time. The writer batches and flushes off
the request path, like `audit/writer.py` — with one deliberate difference: a failed audit
write must be loud, whereas a dropped usage row is acceptable. Telemetry must never block
an answer.

**`cached` is what keeps the numbers honest.** A cache hit is a real operation with real
latency and zero token consumption. Without the flag, cache effectiveness is invisible and
consumption looks like it is falling when demand is actually flat.

**`department` is denormalized on purpose.** §7.4 asks for consumption by directorate, and
joining `users` on every report over a high-volume partitioned table is avoidable work. It
is a reporting convenience only — nothing security-relevant reads it.

---

## 6. Evaluation (SRS §7.5 — a production acceptance gate)

```sql
CREATE TABLE eval_datasets (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK (category IN ('retrieval','generation','security','performance')),
    description TEXT,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eval_questions (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    dataset_id UUID NOT NULL REFERENCES eval_datasets(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    expected_answer TEXT,
    expected_document_ids UUID[],           -- retrieval recall/precision ground truth
    expected_state answer_state,            -- e.g. must refuse, must flag conflict
    as_principal_id BIGINT REFERENCES principals(id),  -- run as this identity
    must_not_retrieve UUID[],               -- ACL-bypass assertions
    tags TEXT[]
);

CREATE TABLE eval_runs (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    dataset_id UUID NOT NULL REFERENCES eval_datasets(id),
    git_sha TEXT, model TEXT, prompt_hash TEXT, retriever TEXT,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    metrics JSONB NOT NULL DEFAULT '{}',    -- groundedness, citation_accuracy, recall@k, p95_ms
    passed BOOLEAN
);

CREATE TABLE eval_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    question_id UUID NOT NULL REFERENCES eval_questions(id),
    actual_answer TEXT,
    actual_state answer_state,
    retrieved_document_ids UUID[],
    scores JSONB NOT NULL DEFAULT '{}',
    passed BOOLEAN NOT NULL,
    failure_reason TEXT
);
```

`eval_questions.must_not_retrieve` is the schema-level expression of the
"unauthorized retrieval = 0" acceptance metric: the security suite asserts absence,
not just presence, and it does so as a specific principal.

---

## 7. Phase 3 seams (defined, unused in Phase 1)

Created now so the Phase 3 agent layer is additive (§7.1) rather than a migration
of live tables.

```sql
CREATE TABLE tool_registry (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    name TEXT NOT NULL UNIQUE,
    system TEXT NOT NULL,                   -- hr · finance · travel · servicedesk
    openapi_ref TEXT,
    requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
    required_scopes TEXT[] NOT NULL DEFAULT '{}',
    is_enabled BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE approval_requests (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    requested_by UUID NOT NULL REFERENCES users(id),
    tool_id UUID NOT NULL REFERENCES tool_registry(id),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
           CHECK (status IN ('pending','approved','rejected','expired','executed')),
    approver_id UUID REFERENCES users(id),
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
```

---

## 8. The hot query, in full

Everything above exists to make this one statement fast and safe.

```sql
-- Params: $1 query_embedding vector(1024),
--         $2 tsquery — built by the application with the ASKER's language config (§3.5);
--                      documents are stemmed with their own, so the keyword arm is
--                      same-language by construction and the semantic arm carries
--                      cross-lingual recall
--         $3 principals bigint[],
--         $4 candidate_k, $5 include_historical bool, $6 dept_filter text[]
WITH bounds AS (SELECT $3::bigint[] AS principals, CURRENT_DATE AS today),
semantic AS (
    SELECT c.id, c.classification,
           row_number() OVER (ORDER BY c.embedding <=> $1) AS rnk
    FROM chunks c, bounds b
    WHERE c.acl_principals && b.principals                    -- ← authorization boundary
      AND ($5 OR (c.is_current AND c.lifecycle = 'active'))   -- FR-017 / FR-018
      AND (c.effective_from IS NULL OR c.effective_from <= b.today)
      AND (c.effective_to   IS NULL OR c.effective_to   >= b.today)
      AND ($6 IS NULL OR c.department = ANY($6))
    ORDER BY c.embedding <=> $1
    LIMIT $4
),
keyword AS (
    SELECT c.id, c.classification,
           row_number() OVER (ORDER BY ts_rank_cd(c.tsv, $2) DESC) AS rnk
    FROM chunks c, bounds b
    WHERE c.acl_principals && b.principals                    -- ← same boundary, both arms
      AND c.tsv @@ $2
      AND ($5 OR (c.is_current AND c.lifecycle = 'active'))
      AND (c.effective_to IS NULL OR c.effective_to >= b.today)
    ORDER BY ts_rank_cd(c.tsv, $2) DESC
    LIMIT $4
),
fused AS (                                    -- Reciprocal Rank Fusion, k = 60
    SELECT id, classification, SUM(1.0 / (60 + rnk)) AS rrf_score
    FROM (SELECT * FROM semantic UNION ALL SELECT * FROM keyword) u
    GROUP BY id, classification
)
SELECT f.rrf_score, c.id, c.content, c.heading_path, c.section_ref,
       c.page_from, c.page_to, c.token_count, c.version_seq,
       d.id AS document_id, d.title, d.source_uri, d.classification,
       d.version_label, d.effective_from, d.effective_to, d.lifecycle,
       ks.name AS source_name, ks.business_owner_id
FROM fused f
JOIN chunks c   ON c.id = f.id AND c.classification = f.classification
JOIN documents d ON d.id = c.document_id
JOIN knowledge_sources ks ON ks.id = d.source_id
ORDER BY f.rrf_score DESC
LIMIT 40;
```

Set `SET LOCAL hnsw.iterative_scan = 'relaxed_order'` and `hnsw.max_scan_tuples` per
session. Without iterative scan, a selective ACL filter causes HNSW to return fewer
than `k` rows and retrieval quietly degrades — the failure mode is silent recall loss,
not an error, which is why the pgvector floor is 0.8.

The joins to `documents` / `knowledge_sources` happen **after** `LIMIT 40` — 40 index
lookups, not a join across 50M rows.

---

## 9. Retention, archival, growth

| Table | Growth driver | Policy |
|---|---|---|
| `chunks` | corpus size | Retained; superseded versions kept but `is_current = false` |
| `messages` | DAU × turns | Monthly partitions; retention per AUC policy via `conversations.purge_after` |
| `audit_events` | all activity | Monthly partitions; hot 90 d, then detach → cold storage; retention per policy |
| `model_invocations` | model calls | Monthly partitions; roll up to daily aggregates after 90 d, drop raw |
| `eval_results` | eval runs | Keep last 100 runs per dataset |

Volume projections are TBD in the SRS (§4.2). The design point is 2M documents /
50M chunks / 200k DAU; nothing in the schema assumes a number below that, and every
high-volume table is already partitioned so growth is an ops task rather than a
migration.
