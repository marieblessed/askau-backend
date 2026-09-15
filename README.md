# askau-backend

Backend for **AskAU**, an enterprise Retrieval-Augmented Generation knowledge
assistant for the African Union Commission. A FastAPI service providing
authenticated, permission-aware retrieval and grounded, cited answers over
approved AUC knowledge sources.

Design documentation — architecture, decision records, the requirements trace — is
maintained by the programme separately from this repository. Ask the AskAU team for it;
nothing in the build needs it.

## The one thing to understand

**Authorization is a SQL predicate, never a model judgment.** The retrieval
query intersects the caller's principal set against each chunk's access list, so
the language model only ever sees rows that already passed. A row-level security
policy enforces the same rule independently, and one test in the suite removes
the application predicate to prove the policy still holds on its own.

Everything else in this repository is arranged around keeping that true.

## Prerequisites

PostgreSQL **with pgvector ≥ 0.8** and Redis, both provisioned externally —
neither is defined here (ADR-0014). The version floor is not a preference:
below 0.8 an access-filtered vector search silently returns fewer results than
asked for, which degrades answers with nothing in the logs to say so. The
application asserts it at startup.

```bash
docker run -d --name askau-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=askau \
  -p 5433:5432 pgvector/pgvector:pg17
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
`ASKAU_EMBEDDING_PROVIDER=hash` exercise the entire pipeline — retrieval,
evidence gate, citation validation — with no model endpoint and no cost. Both
are refused in production by configuration validation.

## Seeing the design work

The seeded identities have deliberately overlapping group membership, so
permission behaviour is visible rather than merely asserted:

```bash
curl -H "Authorization: Bearer $(make -s dev-token USER=staff.finance)" \
  -X POST localhost:8080/v1/ask -H 'Content-Type: application/json' \
  -d '{"content":"What are the budget reallocation thresholds?"}'
#   → grounded, cites the confidential Finance procedure

curl -H "Authorization: Bearer $(make -s dev-token USER=staff.misd)" \
  -X POST localhost:8080/v1/ask -H 'Content-Type: application/json' \
  -d '{"content":"What are the budget reallocation thresholds?"}'
#   → insufficient_evidence, no citations, no leakage
```

Same question, same corpus, different answers. That comparison is the whole
design in one observation.

## Commands

```
make check          lint + typecheck + tests + contract   (what CI will run)
make test-security  the TC-SEC authorization isolation suite
make dev-token      mint a bearer token for a seeded identity
make eval           the RAG evaluation gate
```

`make check` is the entire future pipeline in one command. CI, when it is added,
calls it and nothing else — so a check that passes locally cannot fail remotely
for reasons nobody can reproduce.

## Layout

```
domain/      pure types, no I/O, no framework          ─┐ enforced by
rag/         orchestration — depends only on ports      │ import-linter
retrieval/   port + adapters (pgvector hybrid is default)
llm/         port + adapters (echo, OpenAI-compatible)  │ vendor SDKs may
ingestion/   extractors, chunking, language, ACL sync   │ appear only here
api/         HTTP edge — thin, holds no logic          ─┘
core/        identity, authorization, errors, caching
db/          engines and the hot query
```

Three import contracts run in `make lint`. Code that reaches for a vendor SDK
from the orchestration layer fails the build.
