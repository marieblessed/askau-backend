# Running AskAU locally

For someone who has just cloned the repository and wants a working system on their own
machine. Half an hour, most of it waiting for downloads.

`docs/architecture/16-host-dependencies.md` is the *platform team's* contract — what a
deployment must supply. This is the developer's version of the same list.

---

## What you install on your machine

Three things, and deliberately only three.

| | Why this version |
|---|---|
| **Python 3.12** | `make setup` calls `python3.12` by name. 3.13 has not been validated. |
| **Docker** | Not for AskAU — it builds no images (ADR-0014). For Postgres and Redis, below. |
| **Node 20+ and pnpm 9** | Only if you are running the web client. `pnpm@9.15.9` is pinned in its `package.json`. |

**Nothing else.** No Tesseract, no embedding model, no language runtime beyond Python.
Where a capability needs a system binary it runs as its own container and the application
speaks HTTP to it — see §3 of the host-dependencies doc for why that constraint exists and
what it prevents.

---

## 1. The two services

Postgres and Redis are not provisioned from this repository, and there is no compose file
here on purpose — images belong to the platform team. Any Postgres 16+ with pgvector
≥ 0.8.0 will do. If you have no opinion, these two commands get you there:

```bash
docker run -d --name askau-pg -p 5433:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=askau \
  pgvector/pgvector:pg17
```

```bash
docker run -d --name askau-redis -p 6379:6379 redis:7
```

**Note the port: 5433.** Not a preference — a second Postgres on 5432 is common, and
connecting to the wrong one is a confusing failure rather than a loud one: migrations
apply to a database nobody is reading. Pick any free port and keep both URLs in `.env`
consistent with it.

**pgvector ≥ 0.8.0 is enforced at startup, and the reason is worth knowing.** Retrieval
sets `hnsw.iterative_scan = relaxed_order` per session. Below 0.8.0 that setting does not
exist and an ACL-filtered vector search *silently under-returns* — the query succeeds, the
answer is thinner, nothing errors. Because that failure is invisible the application
refuses to boot instead. Check with:

```bash
docker exec -i askau-pg psql -U postgres -d askau -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

---

## 2. The API

```bash
make -C api setup
cp api/.env.example api/.env
```

Then edit `api/.env` for two things only:

- the **port** in both `ASKAU_DATABASE_URL` and `ASKAU_MIGRATION_DATABASE_URL`, if you
  did not use 5433
- nothing else — the defaults are chosen so a fresh checkout runs with no model and no
  tenant

Two roles appear in those URLs and the difference is load-bearing. `askau_app` is what the
application uses and is subject to row-level security on `chunks`; `postgres` runs
migrations and maintenance, which RLS would otherwise blind. The `askau_app` role is
created by migration `0004`, so you do not make it yourself.

```bash
make -C api migrate
make -C api seed
make -C api run
```

`seed` loads a synthetic AUC corpus and twelve test identities with different access.
That corpus is what makes the product demonstrable: two people asking the same question
get different answers, which is not observable with one identity.

Check it:

```bash
curl -s localhost:8080/health/ready
```

### What the defaults give you, and what they don't

| Setting | Default | Effect |
|---|---|---|
| `ASKAU_EMBEDDING_PROVIDER` | `hash` | Deterministic pseudo-embeddings. Retrieval *runs*; semantic quality is meaningless. Refused in production. |
| `ASKAU_LLM_PROVIDER` | `echo` | A stand-in that composes an answer from retrieved text without a model. Refused in production. |
| `ASKAU_AUTH_MODE` | `dev` | Locally-signed tokens for the seeded identities. No Entra tenant needed. |
| `ASKAU_OCR_PROVIDER` | `none` | Scanned PDFs are rejected with an actionable message rather than indexed as empty. |

These defaults are the point: **the authorization path, the retrieval path and the API
contract are all fully exercised with nothing installed.** What the stand-ins cost you is
answer *quality*, not answer *correctness* — an ACL bug is as visible with `hash`
embeddings as with real ones.

To use a real model, run Ollama and set the three `ASKAU_LLM_*` variables commented in
`.env.example`. For real embeddings, `pip install -e ".[embeddings]"` and set
`ASKAU_EMBEDDING_PROVIDER=sentence_transformers` — then **re-seed**, because existing
vectors were produced by a different model and mixing them silently degrades retrieval.

---

## 3. The web client (optional)

A **separate repository**, `askau-frontend`, owned by another team.
`contracts/openapi.json` is the interface between them.

```bash
cd ../askau-frontend
pnpm install
```

There is no `.env.example` in that repository yet, so write `.env.local` by hand:

```
NEXT_PUBLIC_APP_URL=http://localhost:3000
NEXT_PUBLIC_API_BASE_URL=/api
NEXT_PUBLIC_USE_MOCK_API=true
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=<openssl rand -base64 32>
ASKAU_API_URL=http://127.0.0.1:8080
```

`NEXT_PUBLIC_USE_MOCK_API=true` is what enables the dev sign-in bypass and the identity
picker. Set it to `false` only when you have a real Entra tenant configured, which locks
you out of the interface entirely if anything else is wrong.

Then the step that is easy to miss:

```bash
make -C ../ask_au/api dev-tokens > .dev-tokens.json
```

The client never puts a bearer in the browser. Its proxy at `app/api/[...path]/route.ts`
attaches one server-side, and in dev mode it reads that file to decide *which* — keyed by
the username you signed in as. Without the file every request answers "Not signed in"
from the proxy, which looks like a session bug and is not one. The file is gitignored and
the tokens last 30 days.

```bash
pnpm dev
```

Sign in at `http://localhost:3000` with any of the seeded usernames — `staff.finance`,
`staff.hr`, `staff.legal`, `admin.knowledge` — password ignored. Ask the same question as
two of them; the difference in what comes back is the product.

For a real Entra tenant instead, see [azure-setup.md](azure-setup.md).

---

## 3b. Ingesting from Azure Blob Storage

The AUC's documents live in Azure Blob Storage (ADR-0029); nothing else is a source. You do
not need a real storage account to exercise the path — Azurite, Microsoft's emulator, speaks
the same REST API:

```bash
docker run -d --name askau-azurite -p 10000:10000 \
  mcr.microsoft.com/azure-storage/azurite azurite-blob --blobHost 0.0.0.0
```

Then register a source and run one pass:

```bash
make -C api ingest SOURCE=<knowledge_sources.id>
```

**Every source must declare where its audience comes from.** This is the part that surprises
people, and it is deliberate: blob storage has **no per-blob permissions**. Access is granted
at the container, so every blob inside is equally reachable by whoever holds the grant. Ingest
a flat container and the product's central claim — that two people asking the same question
get different answers — silently becomes false.

So `location.acl_strategy` is required, with no default:

| Strategy | Audience comes from |
|---|---|
| `metadata` | A blob metadata key (`askau_principals`) holding Entra object ids |
| `prefix` | The blob's path, matched against `prefix_map` — `hr/` grants the HR group |
| `fixed` | One list for the whole container |

A blob whose audience cannot be determined is **not ingested**. It is reported as a run
failure with an actionable message, rather than being published to everyone.

Credentials are a service principal (`ASKAU_AZURE_STORAGE_CLIENT_ID` /
`_SECRET`, needing **Storage Blob Data Reader** on the container) or a SAS
(`ASKAU_AZURE_STORAGE_SAS_TOKEN`). There is no account-key setting on purpose: an account key
grants everything the account can do, to anyone holding it, with no expiry and no audit trail.

---

## 4. Verifying the checkout

```bash
set -a && . api/.env && set +a && make check
```

Ruff, mypy, the import contracts and 636 tests — what CI runs. Two things about that
command line, both of which cost time to discover:

**`make` does not read `.env`.** `requires_db` keys on `ASKAU_DATABASE_URL` being present
in the environment, so a bare `make check` runs 260 tests and skips 376 — including every
authorization test. It reports success. Export first, as above.

**The suite needs `ASKAU_AUTH_MODE=dev`.** It mints dev tokens; in `entra` mode the API
correctly rejects them and about 150 tests fail with `401` where they expected `403`. If
you have been testing against a real tenant, override for the run:

```bash
set -a && . api/.env && set +a && ASKAU_AUTH_MODE=dev make check
```

---

## Things that will bite

| Symptom | Cause |
|---|---|
| `make setup` — `python3.12: command not found` | The Makefile names the interpreter deliberately. Install 3.12. |
| Refuses to start, complaining about pgvector | Plain `postgres` image, or pgvector < 0.8.0. Use `pgvector/pgvector:pg17`. |
| Migrations apply but the corpus is empty | Two Postgres instances; the app and the migration URL point at different ports. |
| Every UI request says "Not signed in" | `.dev-tokens.json` missing or stale. Regenerate. |
| 401 on everything after a working week | Dev tokens expired. Regenerate. |
| Tests pass suspiciously fast | The DB-backed ones skipped — 260 run instead of 626. Export `api/.env` first. |
| ~150 tests fail with `401 != 403` | `.env` is in `entra` mode. Run with `ASKAU_AUTH_MODE=dev`. |
| Answers are nonsense but citations are right | `hash` embeddings. Expected; switch providers if you are judging quality. |
