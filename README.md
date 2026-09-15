# askau-backend

Backend for **AskAU**, an enterprise Retrieval-Augmented Generation knowledge
assistant for the African Union Commission. A FastAPI service providing
authenticated, permission-aware retrieval and grounded, cited answers over
approved AUC knowledge sources.

## The one thing to understand

**Authorization is a SQL predicate, never a model judgment.** The retrieval query
intersects the caller's principal set against each chunk's access list, so the
language model only ever sees rows that already passed. A row-level security
policy enforces the same rule independently, and one test in the suite removes
the application predicate to prove the policy still holds on its own.

A model cannot be prompted into disclosing something it was never given.
Everything else here is arranged around keeping that true.

## Prerequisites

Python **3.12** (the Makefile names it), plus PostgreSQL with **pgvector ≥ 0.8**
and Redis. Neither service is defined here — they are provisioned externally.

The pgvector floor is not a preference. Below 0.8 an access-filtered vector
search silently returns fewer rows than asked for, degrading answers with nothing
in the logs to say so, so the application asserts the version at startup and
refuses to run.

```bash
docker run -d --name askau-pg -p 5433:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=askau pgvector/pgvector:pg17
docker run -d --name askau-redis -p 6379:6379 redis:7
```

## Setup

```bash
make setup                 # venv, dependencies, pre-commit hooks
cp .env.example .env       # point at your Postgres and Redis
make migrate               # apply the schema
make seed                  # synthetic corpus + 12 test identities
make run                   # http://localhost:8080/docs
```

Defaults run credential-free: `ASKAU_LLM_PROVIDER=echo` and
`ASKAU_EMBEDDING_PROVIDER=hash` exercise the whole pipeline — retrieval, the
evidence gate, citation validation — with no model endpoint and no cost. Both are
refused in production by configuration validation.

## Seeing the design work

The seeded identities have deliberately overlapping group membership, so
permission behaviour is visible rather than merely asserted:

```bash
TOK=$(make -s dev-token USER=staff.finance)
curl -s localhost:8080/api/v1/ask -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"content":"What are the budget reallocation thresholds?"}'
#   → grounded, cites the confidential Finance procedure
```

Ask the identical question as `staff.misd` and it returns `insufficient_evidence`
with no citations and nothing leaked. Same question, same corpus, different
answers — that comparison is the whole design in one observation.

## Where documents and identities come from

**Azure Blob Storage** for documents, **Microsoft Entra ID** for identity.

Blob storage has no per-blob permissions: access is granted at the container, so
every blob inside is equally reachable by whoever holds the grant. Each source
therefore *declares* where audiences come from, and there is no default —
`metadata` (Entra object ids on the blob), `prefix` (a folder convention), or
`fixed`. A blob whose audience cannot be determined is **not ingested**; the run
reports it. See `src/askau/ingestion/connectors/azure_blob.py`.

## Commands

```
make check           lint + typecheck + tests + contract   (what CI runs)
make test-security   the TC-SEC authorization isolation suite
make ingest          one ingestion pass:  SOURCE=<uuid>
make reconcile-acls  bring chunks.acl_principals back in step with document_acl
make provision-user  create an AskAU account for a directory identity
make dev-token       mint a bearer token for a seeded identity
make eval-real       re-seed with a real embedding model and measure retrieval
```

`make help` lists them all. `make check` is the entire pipeline in one command,
so a check that passes locally cannot fail remotely for reasons nobody can
reproduce.

Two things about running it:

* **`make` does not read `.env`.** Database-backed tests key on
  `ASKAU_DATABASE_URL` being in the environment and skip silently without it, so
  a bare `make check` reports success having run a third of the suite. Use
  `set -a && . ./.env && set +a && make check`.
* **The suite needs `ASKAU_AUTH_MODE=dev`.** It mints dev tokens; in `entra` mode
  the API correctly rejects them.

## Layout

```
domain/      pure types, no I/O, no framework          ─┐ enforced by
rag/         orchestration — depends only on ports      │ import-linter
retrieval/   port + adapters (pgvector hybrid default)  │
llm/         port + adapters (echo, OpenAI-compatible)  │ vendor SDKs may
ingestion/   extractors, chunking, connectors, ACL sync │ appear only here
api/         HTTP edge — thin, holds no logic          ─┘
core/        identity, authorization, errors, caching
db/          engines and the hot query
scripts/     operator entry points and the seed
```

Three import contracts run in `make lint`: code that reaches for a vendor SDK
from the orchestration layer fails the build. That rule is load-bearing — it is
why the Azure Blob connector speaks REST over `httpx` rather than pulling the
Azure SDK into the service.

`contracts/openapi.json` is generated by `make contract` and committed, so an
interface change appears in the diff of the merge request that caused it.
`make contract-check` fails when the API moves without it — the web client is a
separate repository and generates its types from that file.

## Documentation

Architecture, decision records and the requirements trace are maintained by the
programme separately from this repository. Nothing in the build needs them.
