# AskAU — data models as built

Every table in the database, what it stores, and whether anything uses it yet.

**"In use"** means the table has working code reading and writing it.
**"Schema only"** means the table exists but nothing writes to it.

**24 tables** · **18 in use** · **6 schema only**

| Area | Table | Cols | Status | What it stores |
|---|---|---:|---|---|
| Identity & access | `principals` | 6 | In use | Everyone and every group that access can be granted to. |
| Identity & access | `users` | 12 | In use | Staff accounts — who a person is and which directorate they belong to. |
| Identity & access | `user_principals` | 4 | In use | Which groups each person belongs to. |
| Identity & access | `app_role_assignments` | 5 | In use | Who is an AskAU administrator, and of which kind. |
| Identity & access | `sessions` | 7 | In use | Active sign-ins and when they expire. |
| Knowledge | `knowledge_sources` | 18 | In use | Each document repository AskAU pulls from, and who owns it. |
| Knowledge | `document_families` | 5 | In use | Groups every version of the same policy together, and marks which one is current. |
| Knowledge | `documents` | 31 | In use | One version of one document — its title, link, classification and dates. |
| Knowledge | `document_acl` | 4 | In use | Who is allowed to read each document. |
| Retrieval | `chunks` | 26 | In use | Documents split into searchable passages, each carrying its own permission list. |
| Conversation | `conversations` | 8 | In use | A chat thread belonging to one person. |
| Conversation | `messages` | 18 | In use | The questions asked and the answers given. |
| Conversation | `citations` | 17 | In use | Which passage each part of an answer came from. |
| Conversation | `message_feedback` | 14 | In use | Whether an answer was useful, and if not, what was wrong with it. |
| Operations | `audit_events` | 16 | In use | A permanent log of who did what, and whether it was allowed. |
| Operations | `ingestion_runs` | 16 | In use | Each time a source was synced, and how many documents succeeded or failed. |
| Operations | `ingestion_tasks` | 10 | In use | Progress of each individual document during a sync. |
| Operations | `model_invocations` | 15 | In use | Every AI model call — how many tokens it used and how long it took. |
| Evaluation | `eval_datasets` | 6 | Schema only | Named sets of test questions. |
| Evaluation | `eval_questions` | 9 | Schema only | A test question, whose account to ask it as, and what should come back. |
| Evaluation | `eval_runs` | 7 | Schema only | One run of a test set against a specific version of the code. |
| Evaluation | `eval_results` | 9 | Schema only | How each question scored in a run. |
| Future actions | `tool_registry` | 7 | Schema only | Actions AskAU could perform in other systems. None enabled. |
| Future actions | `approval_requests` | 9 | Schema only | A person's sign-off before such an action runs. |

---

## Design points worth knowing

A handful of these tables carry a decision rather than just data.

**Permissions are stored twice, on purpose.** `document_acl` is the authority — who may
read a document. `chunks.acl_principals` is a copy of that answer on each passage, kept in
step automatically. Searching millions of passages cannot afford to join back to the
permission table on every row, so the answer is already there.

**Group membership is flattened.** `user_principals` records the groups a person is in
*including* groups inherited through other groups, worked out once when their account
syncs. No search ever has to trace a chain of group memberships to decide what someone can
see.

**Approval covers the source, not yet the documents inside it.**

A *source* is a repository AskAU pulls from — a SharePoint library, say. BR-001 says a
named person must approve a source before AskAU indexes it, and the `knowledge_sources`
table refuses to hold an unapproved active source:

```sql
CHECK (status <> 'active' OR approved_by IS NOT NULL)
```

That is a database rule rather than an application check on purpose. An application check
only covers the paths that go through the application — a data-fix script, a migration or
a direct connection each bypass it. A constraint refuses the write whoever is asking.

**Phase 1 assumes everything inside an approved library is approved.** Approving the
library says nothing about the individual documents in it, and nothing currently
distinguishes them: every document is indexed as current policy. So a draft saved into an
approved library can be quoted as though it were settled policy. This is accepted
knowingly for Phase 1, not overlooked — it is recorded in the traceability matrix and
pinned by a test that fails if the assumption changes.

The intended fix, when the approval process is built: SharePoint already tracks a
moderation status per document (Draft / Pending / Approved / Rejected) on libraries with
content approval enabled. Mapping that onto `documents.lifecycle` is better than adding a
second approval queue inside AskAU, because the document owners already maintain it — an
approval step nobody performs is worse than none, since it still looks like a control.
Retrieval already filters on `lifecycle` in both search arms, so the change is confined to
what ingestion writes.

**A citation cannot be invented.**

Every citation stores the identifier of the passage it came from, and the database requires
that identifier to match a passage that actually exists. If the language model referred to
a source that was never retrieved, saving the answer fails — the invented reference has
nowhere to point. This is FR-030 held by the database rather than by trusting the model's
output, which is the point: the model is not the thing being trusted.

**`chunks` is split four ways by classification.** Public, internal, confidential and
highly restricted each live in their own physical partition with their own search indexes,
plus a second database-level access rule on top. A search for public material never touches
restricted pages.

**Some tables are split by time.** `messages`, `audit_events` and `model_invocations` are
partitioned by month, so old data can be archived or dropped without a large delete.

**Administrator roles are separate from directory groups.** `app_role_assignments` is
AskAU's own list. Authority over AskAU is granted deliberately, not inherited because
somebody was added to a group for an unrelated reason.

---

## The two "schema only" groups are not the same thing

**Future actions** — `tool_registry` and `approval_requests` are *meant* to have nothing
writing to them yet. They are in place so that letting AskAU take actions in other systems
later is an addition rather than a rebuild. Both default to "off" and "needs approval".

**Evaluation** — this one is a real gap. The quality test suite works and runs on every
build, but it keeps its questions in code and its results in memory, so nothing is written
to these four tables. The practical consequence: **we can report quality today, but we
cannot show whether it is improving.** `eval_runs` was designed to record the code version
behind each score for exactly that purpose. Wiring the test suite to write here is
outstanding work.

---

## Related

- Entity-relationship diagrams: [architecture/12-data-model-diagrams.md](architecture/12-data-model-diagrams.md)
- Full column-level schema: [architecture/03-database-schema.md](architecture/03-database-schema.md)
- Requirement traceability: [architecture/10-traceability.md](architecture/10-traceability.md)

Column counts read from `api/alembic/versions/`. All 11 migrations apply and reverse
cleanly.
