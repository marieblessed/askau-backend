# AskAU — Data Model & Flow Diagrams

Closes the gap the SRS names in Appendix C: *"Entity-relationship diagrams for the
knowledge, user, and conversation data models (Section 4.1) shall be produced during
detailed data modeling and added to this appendix prior to baselining."*

Also supplies the analysis models referenced as Figures C-1, C-2 and C-3.

---

## 1. Knowledge data model

```mermaid
erDiagram
    KNOWLEDGE_SOURCES ||--o{ DOCUMENT_FAMILIES : "publishes"
    KNOWLEDGE_SOURCES ||--o{ DOCUMENTS : "owns"
    KNOWLEDGE_SOURCES }o--|| USERS : "business_owner (BR-002)"
    KNOWLEDGE_SOURCES }o--o| USERS : "approved_by (BR-001)"
    DOCUMENT_FAMILIES ||--o{ DOCUMENTS : "has versions"
    DOCUMENT_FAMILIES }o--o| DOCUMENTS : "current_document_id (FR-017)"
    DOCUMENTS ||--o{ CHUNKS : "is chunked into"
    DOCUMENTS ||--o{ DOCUMENT_ACL : "authoritative permissions"
    DOCUMENTS }o--o| DOCUMENTS : "supersedes"
    DOCUMENT_ACL }o--|| PRINCIPALS : "grants read to"
    CHUNKS }o--o{ PRINCIPALS : "acl_principals[] (materialized)"

    KNOWLEDGE_SOURCES {
        uuid id PK
        text name UK
        enum source_type
        uuid business_owner_id FK "NOT NULL"
        enum default_classification
        jsonb location "connector config"
        enum status
        uuid approved_by FK "CHECK: active requires approval"
        text sync_cron
    }
    DOCUMENT_FAMILIES {
        uuid id PK
        uuid source_id FK
        text external_key "UK with source_id"
        uuid current_document_id FK
    }
    DOCUMENTS {
        uuid id PK
        uuid family_id FK
        text title
        text source_uri "BR-004 authoritative link"
        bytea content_hash "drives skip-unchanged"
        enum classification
        int version_seq "UK with family_id"
        enum lifecycle
        date effective_from
        date effective_to
        enum ingest_status
        smallint injection_risk "FR-036"
    }
    CHUNKS {
        bigint id PK "PK is (id, classification)"
        uuid document_id FK
        text content
        text_array heading_path "FR-015 citation anchor"
        smallint page_from
        enum classification "LIST partition key"
        bigint_array acl_principals "DERIVED from DOCUMENT_ACL"
        bool is_current "denormalized FR-017"
        vector embedding "1024d, HNSW per partition"
        tsvector tsv "generated, GIN"
    }
    DOCUMENT_ACL {
        uuid document_id PK
        bigint principal_id PK
        text source_of_truth "repository | askau_override"
    }
```

**The one relationship to understand:** `CHUNKS.acl_principals` is a *derived cache* of
`DOCUMENT_ACL`, drawn as a many-to-many but never written directly. `DOCUMENT_ACL`
remains authoritative. Everything in the security model depends on that derivation
staying correct, which is what the reconciler and the `acl_synced_at` metric exist for.

## 2. Identity & authorization model

```mermaid
erDiagram
    PRINCIPALS ||--o| USERS : "user principal"
    USERS ||--o{ USER_PRINCIPALS : "flattened membership"
    PRINCIPALS ||--o{ USER_PRINCIPALS : "granted to"
    USERS ||--o{ APP_ROLE_ASSIGNMENTS : "holds"
    USERS ||--o{ SESSIONS : "authenticates"
    PRINCIPALS ||--o{ DOCUMENT_ACL : "authorized on"

    PRINCIPALS {
        bigint id PK "BIGINT: lives in hot-path arrays"
        enum kind "user|group|role|department"
        text external_id "Entra objectId, UK with kind"
        bool is_active
    }
    USERS {
        uuid id PK
        bigint principal_id FK UK
        text entra_oid UK
        citext email UK
        text department
        int acl_version "bump invalidates caches"
    }
    USER_PRINCIPALS {
        uuid user_id PK
        bigint principal_id PK
        text granted_via
        timestamptz synced_at
    }
    APP_ROLE_ASSIGNMENTS {
        uuid user_id PK
        text role PK "end_user|knowledge_admin|system_admin|security_admin"
        uuid granted_by FK
    }
```

Two deliberate separations. **Transitive group membership is flattened at sync time**
into `USER_PRINCIPALS`, so no request ever walks a group graph. And **AskAU application
roles are separate from Entra groups** — administrative authority over AskAU itself is
granted explicitly and audited, rather than inherited from a directory group that
someone may have joined for an unrelated reason.

## 3. Conversation data model

```mermaid
erDiagram
    USERS ||--o{ CONVERSATIONS : "owns"
    CONVERSATIONS ||--o{ MESSAGES : "contains"
    MESSAGES ||--o{ CITATIONS : "evidenced by"
    MESSAGES ||--o{ MESSAGE_FEEDBACK : "rated by"
    CITATIONS }o--|| CHUNKS : "resolves to (FR-030)"
    CITATIONS }o--|| DOCUMENTS : "attributed to"

    CONVERSATIONS {
        uuid id PK
        uuid user_id FK "ownership predicate, FR-006"
        text title
        timestamptz last_message_at
        timestamptz purge_after "configurable retention"
    }
    MESSAGES {
        uuid id PK "PK is (id, created_at)"
        uuid conversation_id FK
        enum role
        text content
        enum answer_state "grounded|conflict|insufficient_evidence|..."
        numeric groundedness
        int ttft_ms "NFR-002a measurement"
        int total_ms "NFR-002b measurement"
        uuid correlation_id
        timestamptz created_at "RANGE partition key, monthly"
    }
    CITATIONS {
        bigint id PK
        uuid message_id FK
        bigint chunk_id FK "FK makes fabrication impossible"
        smallint marker "the [1] the user sees"
        text quote "exact supporting span"
        bool verified
    }
    MESSAGE_FEEDBACK {
        bigint id PK
        uuid message_id FK
        uuid user_id FK "UK with message_id"
        enum rating
        text reason_code "closed vocabulary, FR-044"
    }
```

**`CITATIONS.chunk_id` is a foreign key, and that is the point.** FR-030 forbids
fictional document references. A citation that does not resolve to a chunk cannot be
inserted, so "no fabricated citations" is enforced by referential integrity rather than
by trusting the model or the validator alone.

## 4. Operations, evaluation and Phase 3 seams

The three models above are the ones a reader of the product needs. These are the ones an
operator, an auditor and an evaluator need — and they are just as much part of the schema.

```mermaid
erDiagram
    KNOWLEDGE_SOURCES ||--o{ INGESTION_RUNS : "synced by"
    INGESTION_RUNS ||--o{ INGESTION_TASKS : "one per document"
    INGESTION_TASKS }o--o| DOCUMENTS : "produces"
    INGESTION_RUNS ||--o{ MODEL_INVOCATIONS : "embedding cost"
    USERS ||--o{ SESSIONS : "holds"
    USERS ||--o{ MODEL_INVOCATIONS : "attributed to"
    EVAL_DATASETS ||--o{ EVAL_QUESTIONS : "contains"
    EVAL_DATASETS ||--o{ EVAL_RUNS : "is run as"
    EVAL_RUNS ||--o{ EVAL_RESULTS : "produces"
    EVAL_QUESTIONS ||--o{ EVAL_RESULTS : "answered by"
    EVAL_QUESTIONS }o--o| PRINCIPALS : "as_principal_id (asks AS someone)"
    TOOL_REGISTRY ||--o{ APPROVAL_REQUESTS : "gated by"
    USERS ||--o{ APPROVAL_REQUESTS : "requests / approves"

    SESSIONS {
        uuid id PK
        uuid user_id FK
        timestamptz expires_at
        timestamptz revoked_at
        bytea ip_hash "hashed, never raw (NFR-005)"
    }
    AUDIT_EVENTS {
        bigint id PK "RANGE partitioned on occurred_at"
        uuid correlation_id "ties one request across every table"
        text event_category "authz, ai_safety, ingestion, ..."
        enum outcome "success | failure | denied"
        uuid actor_user_id
        bytea ip_hash
        jsonb detail
    }
    INGESTION_RUNS {
        uuid id PK
        uuid source_id FK
        text trigger "schedule | manual | webhook | reindex"
        text status
        integer docs_discovered
        integer docs_failed
        bigint embedding_tokens
    }
    INGESTION_TASKS {
        bigint id PK
        uuid run_id FK
        uuid document_id FK
        enum stage "checkpoint — a failed run resumes here"
        smallint attempts
        text error_code "no_text_layer, insufficient_text, ..."
        jsonb stage_timings
    }
    MODEL_INVOCATIONS {
        bigint id PK "RANGE partitioned on occurred_at"
        text operation "embed_query | rerank | chat | ..."
        text provider
        text model
        text department "denormalized for per-directorate rollup"
        integer input_tokens
        boolean cached
    }
    EVAL_DATASETS {
        uuid id PK
        text name UK
        text category "retrieval | generation | security | performance"
    }
    EVAL_QUESTIONS {
        uuid id PK
        uuid dataset_id FK
        text question
        enum expected_state
        bigint as_principal_id FK "the identity to ask as"
        uuid_array must_not_retrieve "the isolation assertion"
    }
    EVAL_RUNS {
        uuid id PK
        uuid dataset_id FK
        text git_sha "which commit produced this score"
        text model
        text prompt_hash
        jsonb metrics
        boolean passed
    }
    EVAL_RESULTS {
        bigint id PK
        uuid run_id FK
        uuid question_id FK
        enum actual_state
        jsonb scores
        boolean passed
        text failure_reason
    }
    TOOL_REGISTRY {
        uuid id PK
        text name UK
        text system
        boolean requires_approval "default TRUE"
        boolean is_enabled "default FALSE"
    }
    APPROVAL_REQUESTS {
        uuid id PK
        uuid tool_id FK
        uuid requested_by FK
        uuid approver_id FK
        text status "pending | approved | rejected | expired | executed"
        jsonb payload
    }
```

**`correlation_id` is the spine.** One value is issued per request and written to
`AUDIT_EVENTS`, `MESSAGES` and `MODEL_INVOCATIONS`, so "what happened when this person
asked this question" is one query rather than three joined by timestamp. It is also the
reference shown to the reader under every answer, which is what makes a support request
answerable.

**`EVAL_QUESTIONS.as_principal_id` and `must_not_retrieve` are the security-testing
model.** A question is asked *as* a specific identity and asserts a set of documents that
must **not** come back. That is the isolation requirement expressed as data rather than
as a test someone remembered to write.

**Two tables are deliberately writer-less.** `TOOL_REGISTRY` and `APPROVAL_REQUESTS` are
Phase 3 seams — migrated now so that adding actions later is additive rather than a
restructure, and defaulted to `requires_approval = TRUE` / `is_enabled = FALSE` so the
safe state is the one you get by doing nothing.

> **Known gap.** The four `EVAL_*` tables are schema only. The evaluation harness works
> and its gate runs in CI, but it holds its dataset as Python constants and keeps results
> in memory — nothing is written here. The consequence is that quality can be reported
> for today but not trended, and `EVAL_RUNS.git_sha` exists precisely to make trending
> possible. Wiring the harness to these tables is outstanding work, not a design choice.

---

## 5. Figure C-3 — end-to-end query flow

```mermaid
sequenceDiagram
    autonumber
    participant U as Staff user
    participant W as Web (Next.js)
    participant A as API
    participant R as Redis
    participant P as Postgres
    participant L as LLM

    U->>W: asks a question
    W->>A: POST /conversations/{id}/messages (SSE)
    A->>A: verify token (cached JWKS)
    A->>R: authorization context?
    alt cache hit
        R-->>A: principals[]
    else miss
        A->>P: resolve principals
        P-->>A: principals[]
        A->>R: cache, keyed by acl_version
    end
    A->>A: input guardrail scan
    A->>A: query understanding (coref, filters, strategy)
    A->>R: answer cache? key=(acl_signature, question)
    alt cache hit
        R-->>A: cached grounded answer
        A-->>W: stream cached, done
    else miss
        A->>L: embed question
        L-->>A: vector
        rect rgb(228, 239, 234)
        note over A,P: AUTHORIZATION BOUNDARY
        A->>P: hybrid query — ACL + version filtered, RRF fused
        P-->>A: top 40 authorized chunks only
        end
        A->>A: rerank 40 to 8
        A->>A: evidence sufficiency gate
        alt insufficient evidence
            A-->>W: insufficient_evidence — LLM never called
        else sufficient
            A->>A: assemble context, wrap in data envelopes
            A-->>W: event: sources (before any token)
            A->>L: generate, streaming
            loop tokens
                L-->>A: token
                A-->>W: event: token
            end
            A->>A: validate citations, score groundedness, leak check
            A-->>W: event: done (answer_state, groundedness, timings)
            A->>P: persist message, citations, audit, usage
        end
    end
```

Two properties this diagram is drawn to make visible: the LLM is contacted only
*after* the authorization boundary and only *after* the evidence gate, and the
insufficient-evidence path never reaches it at all.

## 6. Figure C-2 — knowledge ingestion pipeline

```mermaid
flowchart TD
    A[Approved source<br/>SharePoint / DMS] -->|connector| B{Content hash<br/>changed?}
    B -->|no| B1[skipped_unchanged]
    B -->|yes| C{Validation gate<br/>FR-013}
    C -->|fail| C1[quarantined<br/>error_code recorded]
    C -->|pass| D[Extract text<br/>+ OCR if approved]
    D --> E[Normalize +<br/>injection risk score]
    E --> F[Structure-aware chunking<br/>heading path, page anchors]
    F --> G[Batch embed]
    G --> H[(Index into<br/>classification partition)]
    H --> I[Version reconcile<br/>supersede prior]
    I --> J[Materialize acl_principals<br/>from document_acl]
    J --> K[indexed — retrievable]
    E -->|risk high| E1[review_required<br/>admin decision]

    style H fill:#e4efea,stroke:#1f6f5c
    style J fill:#e4efea,stroke:#1f6f5c
    style C1 fill:#f7e7e4,stroke:#a33a31
    style E1 fill:#f7eedc,stroke:#a9701a
```

Each stage checkpoints into `ingestion_tasks`, so a failed run resumes at the failing
document rather than restarting the source. A high injection-risk score routes to
review but does **not** auto-block — a legitimate policy may quote instruction-like
text, and silently dropping approved content is its own failure.

## 7. Figure 6-1 — correct vs incorrect authorization

The SRS calls for this contrast explicitly. It is the single most important diagram in
the set.

```mermaid
flowchart LR
    subgraph WRONG["✗ Incorrect — the model decides"]
        direction TB
        W1[Question] --> W2[Retrieve<br/>everything]
        W2 --> W3[LLM: 'only use<br/>what they may see']
        W3 --> W4[Answer]
        W4 -.->|unauthorized content<br/>already reached the model| W5[Leak]
    end

    subgraph RIGHT["✓ Correct — the query decides"]
        direction TB
        R1[Question] --> R2[Resolve principals]
        R2 --> R3[Retrieve WHERE<br/>acl_principals && principals]
        R3 --> R4[LLM sees only<br/>authorized content]
        R4 --> R5[Answer + validated citations]
    end

    style WRONG fill:#f7e7e4,stroke:#a33a31
    style RIGHT fill:#e4efea,stroke:#1f6f5c
```

The failure on the left is not that the instruction is badly worded. It is that
unauthorized content entered the model's context at all — after which no prompt,
however well written, can undo the exposure. Instructing a model to self-censor is a
request; a `WHERE` clause is a guarantee.

## 8. State model — answer outcomes

```mermaid
stateDiagram-v2
    [*] --> Understanding
    Understanding --> Clarification: ambiguous (FR-008)
    Understanding --> Retrieving
    Retrieving --> OutOfScope: no authorized match (FR-009)
    Retrieving --> EvidenceGate
    EvidenceGate --> Insufficient: below threshold (FR-028)
    EvidenceGate --> Generating: sufficient
    Generating --> Validating
    Validating --> Grounded: all claims supported
    Validating --> PartiallyGrounded: some unsupported
    Validating --> Conflict: sources disagree (FR-035)
    Validating --> RefusedSafety: output scan blocked (FR-038)
    Generating --> Error: upstream unavailable

    Clarification --> [*]
    OutOfScope --> [*]
    Insufficient --> [*]
    Grounded --> [*]
    PartiallyGrounded --> [*]
    Conflict --> [*]
    RefusedSafety --> [*]
    Error --> [*]

    note right of Insufficient
        LLM is never called
        on this path
    end note
```

Every terminal state is persisted in `messages.answer_state`, which is what turns each
of these SRS behaviors into a rate that can be monitored and alerted on rather than a
described intention.
