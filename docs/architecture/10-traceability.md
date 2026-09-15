# AskAU — Requirements Traceability Matrix

Completes SRS Appendix B: every requirement maps to a design element and a
verification test. Paths without a prefix are in `api`; frontend elements
are marked `web`. This is the artifact a security or QA reviewer reads to confirm the
build actually implements the signed-off SRS.

## Functional requirements

| FR | Requirement (abbrev.) | Design element | Test ID |
|---|---|---|---|
| FR-001/001a | Entra ID auth, session, logout | `core/identity.py`, `api/v1/routes/auth.py` | TC-AUTH-001/002 |
| FR-002/002a | Authorization outside the LLM; principal model | `core/authz.py`, `principals`/`user_principals`, `domain/authz.py` | TC-SEC-001 |
| FR-003 | Natural-language questions | `POST /conversations/{id}/messages` | TC-CHAT-001 |
| FR-004 | Conversation context within a session | `rag/understanding.py` (coref window) | TC-CHAT-002 |
| FR-005 | Start a new conversation | `POST /conversations` | TC-CHAT-003 |
| FR-006 | Own history only | `conversations.user_id` predicate | TC-SEC-002 |
| FR-007 | Intent, terms, filters, strategy | `rag/understanding.py` | TC-RAG-002 |
| FR-008 | Ask for clarification when ambiguous | `answer_state='clarification_needed'` | TC-RAG-003 |
| FR-009 | Out-of-scope detection | `rag/evidence.py`, `answer_state='out_of_scope'` | TC-RAG-004 |
| FR-010 | Approved sources only | `knowledge_sources.approved_by` + `active_requires_approval` CHECK | TC-KNOW-001 |
| FR-011 | Register a source with governance metadata | `POST /knowledge/sources` | TC-KNOW-002 |
| FR-012 | PDF/DOCX/XLSX/PPTX/TXT/HTML | `ingestion/extractors/*` | TC-ING-001 |
| FR-013 | Pre-index validation gate | `ingestion/validation.py` | TC-ING-002 |
| FR-014 | Text extraction + OCR | `extractors/pdf.py`, `extractors/ocr.py` | TC-ING-003 |
| FR-015 | Context-preserving chunks | `ingestion/chunking.py` | TC-ING-004 |
| FR-016 | Full chunk metadata | `chunks` columns (denormalized) | TC-ING-005 |
| FR-017 | Version awareness, prefer current | `chunks.is_current`, `document_families.current_document_id` | TC-RAG-005 |
| FR-018 | Expired/superseded excluded by default | retrieval `lifecycle`/`effective_to` predicates | TC-RAG-006 |
| FR-019 | Semantic retrieval | `semantic` CTE (HNSW) | TC-RAG-007 |
| FR-020 | Keyword retrieval | `keyword` CTE (`tsv`) | TC-RAG-008 |
| FR-021 | Hybrid search | `fused` CTE (RRF) | TC-RAG-009 |
| FR-022 | Reranking | `adapters/rerank_crossencoder.py` | TC-RAG-010 |
| FR-023/024 | Permission filter before generation | ACL predicate in both CTE arms + RLS | TC-SEC-003/004 |
| FR-025 | ACL sync window | `ingestion/acl_reconciler.py`, `users.acl_version` | TC-SEC-005 |
| FR-026 | Context with source metadata | `rag/context.py` | TC-RAG-011 |
| FR-027 | Grounded generation | `prompts/system_v3.md`, `rag/grounding.py` | TC-RAG-001 |
| FR-028 | Insufficient evidence → refuse | `rag/evidence.py` (pre-LLM short-circuit) | TC-RAG-012 |
| FR-029 | Citations with title/§/page/link | `citations` table, `sources` SSE event | TC-CITE-001 |
| FR-030 | No fabricated citations | `rag/citations.py` validator + FK to `chunks` | TC-CITE-002 |
| FR-031 | Open only if authorized | `GET /documents/{id}/open` re-check | TC-SEC-006 |
| FR-032 | Clear, structured responses | `prompts/system_v3.md`; `web` `AnswerBody` | TC-CHAT-004 |
| FR-033 | AUC sources over model knowledge | system prompt + `grounding.py` threshold | TC-RAG-013 |
| FR-034 | Communicate uncertainty | `answer_state`; `web` `AnswerStateBanner` | TC-RAG-014 |
| FR-035 | Conflict detection | `rag/conflict.py`, `conflict` SSE event | TC-RAG-015 |
| FR-036 | Injection defense | `guardrails/input_scan.py`, `documents.injection_risk` | TC-SEC-007 |
| FR-037 | Instruction hierarchy | `guardrails/context_shield.py` | TC-SEC-008 |
| FR-038 | No unauthorized info in responses | `guardrails/output_scan.py` | TC-SEC-009 |
| FR-039/040 | Data minimization to the LLM | `rag/context.py` token budget | TC-SEC-010 |
| FR-041 | Enterprise AI service only | `llm/adapters/azure_openai.py`, `vllm.py`; config allow-list | TC-INSP-001 |
| FR-042 | Model abstraction | `llm/ports.py` + `registry.py` + import-linter contract | TC-INSP-002 |
| FR-043/044 | Feedback + reason codes | `message_feedback`; `web` `FeedbackBar` | TC-FB-001/002 |
| FR-045 | Business owner per source | `knowledge_sources.business_owner_id` NOT NULL | TC-INSP-003 |
| FR-046 | Lifecycle visibility | `/knowledge/documents` filters, admin saved views | TC-ADM-001 |
| FR-047 | Added/modified/indexed/synced timestamps | `documents` timestamp columns | TC-ADM-002 |
| FR-048 | Admin dashboard | `GET /admin/overview` — **API only; no console UI.** The web client's `/admin` is a stub and ours was deleted with `web/` (ADR-0017). Administration is out of Phase 1 scope, so this is a known gap rather than a silent one | TC-ADM-003 |
| FR-049 | Trigger reindex | `POST /knowledge/sources/{id}/reindex` | TC-ADM-004 |
| FR-050 | Ingestion error detail | `ingestion_tasks.error_code/error_detail` | TC-ADM-005 |
| FR-051 | Security/operational event log | `audit_events`, `audit/writer.py` | TC-AUDIT-001 |
| FR-052 | Audit fields + no sensitive content | `audit/events.py`, `core/redaction.py` | TC-AUDIT-002 |
| FR-053 | Availability/latency/token monitoring | `observability/metrics.py`, `/admin/metrics`; `model_invocations` + `/admin/usage` for token consumption | TC-OPS-001 |
| FR-054 | Retrieval quality & hallucination indicators | `evaluation/*`, `/admin/quality` | TC-EVAL-001 |

## Non-functional requirements

| NFR | Requirement | Design element | Test ID |
|---|---|---|---|
| NFR-001 | 99.5% availability | multi-replica, PDB, HA Postgres | TC-OPS-002 |
| NFR-002a | First response < 3 s | latency budget §Scaling; SSE `accepted`→`token` | TC-PERF-001 |
| NFR-002b | Full answer < 10 s | streaming + reranker cap | TC-PERF-002 |
| NFR-003 | Horizontal scalability | stateless services, partitioning, PgBouncer | TC-PERF-003 |
| NFR-004a | Encryption in transit + at rest | TLS 1.3, TDE, Key Vault | TC-SEC-011 |
| NFR-004b | Strong auth, RBAC, least privilege | Entra, `app_role_assignments`, `core/rbac.py` | TC-SEC-012 |
| NFR-004c | Secrets + network controls | Key Vault CSI, NetworkPolicy | TC-SEC-013 |
| NFR-004d | Audit + vulnerability mgmt | `audit_events`, `security.yml` CI | TC-SEC-014 |
| NFR-005 | Privacy / data minimization | `ip_hash`, redaction, `purge_after` | TC-PRIV-001 |
| NFR-006 | Data residency | region-pinned deployment + LLM endpoint | TC-INSP-004 |
| NFR-007 | Replaceable components | ports + import-linter contracts; repo split isolates the UI from the pipeline | TC-INSP-002 |
| NFR-008 | REST/OpenAPI interoperability | `/openapi.json`, committed to `contracts/` and consumed by an independent client | TC-INSP-005 |
| NFR-009 | Logs, metrics, traces, alerts | OpenTelemetry | TC-OPS-003 |
| NFR-010a | Backup and recovery | PITR, partition archival | TC-OPS-004 |
| §6.9 | Host-country regulation (Proclamation 958/2016), records management | `06-security-model.md` §4b; append-only `audit_events`; signed export endpoint; no FK from audit to deletable entities | TC-AUDIT-003 |

## Business rules

| BR | Rule | Enforcement point | Kind |
|---|---|---|---|
| BR-001 | Only approved sources indexed | `active_requires_approval` CHECK constraint | Database |
| BR-002 | Owner responsible for accuracy | `business_owner_id` NOT NULL; shown in citations | Database + UI |
| BR-003 | Not the official source of policy | UI disclaimer; originals stay in source repos | Product |
| BR-004 | Reference the official source | `documents.source_uri` NOT NULL; `/open` | Database + API |
| BR-005 | No access beyond authorization | ACL predicate **and** RLS policy | Database ×2 |
| BR-006 | LLM never determines authorization | Architectural: filtering precedes generation; import-linter forbids the inverse | Architecture |
| BR-007 | No fabrication without evidence | `rag/evidence.py` pre-LLM gate | Application |
| BR-008 | Admin/security actions auditable | append-only `audit_events`, no mutate endpoint | Database + API |

Seven of eight business rules are enforced by a database constraint, a policy, or an
architectural boundary rather than by application code that a future change could
bypass. BR-003 is the exception — it is a product and communication commitment, not
something a schema can hold.

## Open items requiring AUC decision before baselining

Carried from the SRS, unresolved by design and needing a business answer:

| Item | SRS ref | Blocks | Proposed default |
|---|---|---|---|
| **Languages in the pilot corpus** | §6.7 | keyword stemming config, OCR language packs, embedding model validation | EN + FR + AR; `simple` config for anything else. Schema handles all — the question is which to test and resource |
| **Is Ethiopic-script content in scope** | §6.7 | Tesseract `amh` pack; no Postgres stemmer exists | Assume not for pilot; `simple` + trigram if it appears |
| **Document-level approval** | BR-001, FR-018 | nothing — deferred by decision | **Phase 1 assumes every document in an approved source is approved.** BR-001 approves the *library*; nothing inspects the documents inside it, so a draft saved into an approved library is quotable as policy. Accepted knowingly. Intended fix when the approval process is built: map SharePoint's own per-item moderation status (Draft / Pending / Approved / Rejected) onto `lifecycle`, rather than adding a second approval queue in AskAU that the document owners would have to work in twice. Retrieval already honours `lifecycle` in both search arms, so the change is confined to what ingestion writes. Pinned by `tests/unit/test_phase1_assumptions.py` |
| **Mobile authorization** | §3.2 | whether phones reach AskAU at all | Full chat on mobile, read-only admin |
| **Supported browser set** | §3.2 | test matrix | Chrome/Edge/Firefox/Safari, last 2 majors |
| **Is conversation history a retained record?** | §6.5, §6.9 | `purge_after` default | 180 days as transient user data — but this is a records-management judgment, not an engineering one |
| Data volume & growth projections | §4.2 | final capacity sizing | the envelope in `07-scaling-playbook.md` |
| Official classification scheme mapping | §4.1 | `classification` enum values | the four SRS tiers as implemented |
| Conversation retention period | §6.5 | `conversations.purge_after` default | 180 days |
| RPO / RTO | §6.2 | backup configuration | RPO 15 min, RTO 4 h |
| MTTR & code-coverage thresholds | §6.6 | CI gates | MTTR 4 h; 80% line / 90% on `rag/` and `retrieval/` |
| ACL synchronization period | FR-025 | reconciler schedule | 15 min incremental, 24 h full |
| GitLab licence tier | §6.6 | per-path approval rules for security-sensitive code | Premium+; pipeline-enforced fallback if Free |
