# AskAU — CI & Code Quality

SRS §6.6 requires source control, branch management, code review, automated testing,
dependency/container/secret scanning, and security testing. This covers those.

> **Status: designed, not yet built.** CI is deliberately deferred until the
> application answers a question end to end in dev — the Month 2 gate in
> `11-implementation-plan.md`. A pipeline is worth little before there is something for
> it to protect from regression.
>
> **In the meantime every check below already exists as a local command** (`make check`
> in `api`, `npm run check` in `web`; see `02-file-structure.md` §5).
> That is the condition that makes deferral safe rather than merely postponed: when the
> pipeline is written, its job scripts call those commands and nothing else.

**Scope boundary:** deployment, container images, orchestration, and infrastructure
provisioning are handled by the separate platform tooling and are not owned by either
application repository. This document ends where a green pipeline ends.

Two GitLab projects, one pipeline each, neither waiting on the other.

---

## 1. Branching & review

Trunk-based with short-lived branches. Long-lived branches are avoided specifically
because they let the architectural import contracts drift and produce large,
unreviewable security-relevant diffs.

| Rule | Value |
|---|---|
| Default branch | `main`, protected |
| Branch lifetime | ≤ 3 days |
| Approvals — `web` | 1 |
| Approvals — `api` | 1; **2 including the Security Officer** for `retrieval/`, `core/authz.py`, `rag/guardrails/`, `alembic/`, or `contracts/` |
| Merge method | Squash, fast-forward only — linear history |
| Direct push to `main` | Blocked for all roles, including Maintainer |
| Signed commits | Required (`reject_unsigned_commits` push rule) |
| Pipeline must succeed | Required before merge |
| All threads resolved | Required before merge |

The path-scoped approval rule is scoped to *paths*, not to judgment. "Does this touch
authorization?" is not a call a reviewer should have to make under time pressure.

**Licence-tier caveat worth resolving early.** Per-path approval rules and `CODEOWNERS`
require GitLab Premium or above. If AUC's instance is Free, the fallback is a
`verify:ownership` job that fails when a merge request touches a protected path without
the Security Officer in its approver list — enforced by pipeline rather than by
platform. Less elegant, same outcome, works on any tier. Confirm the tier before Month 2
so this is a configuration decision rather than a surprise.

---

## 2. What runs before CI exists

Until the pipeline lands, the same checks run in two places.

| Trigger | Runs | How |
|---|---|---|
| On commit | format, lint, secret scan, contract drift | pre-commit hooks, installed by `make setup` |
| Before pushing | everything | `make check` / `npm run check` |
| Before merging | everything, plus a reviewer | manual, by convention |

The weak link is honest and worth naming: "by convention" is not enforcement. Someone
will push a branch that fails `make check`, and nothing will stop the merge. That is an
accepted, temporary risk for the first weeks of a small team, and it is exactly the risk
CI removes. It is a reason to add the pipeline at the Month 2 gate rather than later —
not a reason to add it now, before the commands it would run have stabilised.

Two checks do **not** have a local equivalent and only arrive with CI: full-history
secret scanning, and the scheduled nightly evaluation run. Neither is a merge gate, so
neither blocks early development.

## 3. Pipeline shape

Both projects use a top-level `.gitlab-ci.yml` that `include:`s one file per stage from
`ci/`. Stage files stay small enough to review, and the same includes run locally with
`gitlab-ci-local` so a pipeline can be debugged without pushing.

```yaml
# api/.gitlab-ci.yml
stages: [verify, test, security, contract, evaluate]

default:
  image: python:3.12-slim
  cache:
    key: {files: [poetry.lock]}
    paths: [.venv/]

include:
  - local: ci/verify.gitlab-ci.yml
  - local: ci/test.gitlab-ci.yml
  - local: ci/security.gitlab-ci.yml
  - local: ci/contract.gitlab-ci.yml
  - local: ci/evaluate.gitlab-ci.yml
```

Jobs use `needs:` rather than relying on stage ordering, so independent work runs as a
DAG instead of a queue — lint does not block the integration suite from starting.

Integration tests get PostgreSQL and Redis from GitLab CI `services:`, which are
ephemeral and CI-managed. This is pipeline configuration, not project infrastructure:
neither repository defines how those services run anywhere else.

```yaml
test:integration:
  stage: test
  services:
    - name: pgvector/pgvector:pg16
      alias: postgres
    - name: redis:7-alpine
      alias: redis
  variables:
    ASKAU_DATABASE_URL: postgresql+asyncpg://askau:askau@postgres:5432/askau
    ASKAU_REDIS_URL: redis://redis:6379/0
    ASKAU_LLM_PROVIDER: echo
    ASKAU_EMBEDDING_PROVIDER: hash
  script:
    - alembic upgrade head
    - pytest tests/integration --junitxml=report.xml
  artifacts:
    reports: {junit: report.xml}
```

`echo` and `hash` providers are what let the whole suite run with no cloud credentials
and produce byte-identical results across runs.

---

## 4. `api` jobs

| Stage | Job | Blocks on |
|---|---|---|
| verify | `lint` — ruff check + format | any violation |
| verify | `typecheck` — mypy | any error |
| verify | `contracts` — import-linter | any violation |
| test | `unit` | any failure |
| test | `integration` — real Postgres + pgvector | any failure |
| test | **`security`** — the full TC-SEC suite | any failure |
| test | `migrations` — dry-run against a seeded snapshot | any failure |
| test | `coverage` | < 80% overall, < 90% on `rag/` and `retrieval/` |
| security | `sast` · `secrets` · `deps` · `sbom` · `licences` | high / critical |
| contract | `export` — regenerate `openapi.json`, fail on drift | any drift |
| contract | `breaking` — diff vs target branch | undeclared break |
| evaluate | `smoke` — 30 questions | metric regression |
| evaluate | `full` — scheduled nightly | reports, alerts |

**The security suite runs complete on every merge request** despite the time cost. It
is the only suite whose target is zero, so sampling it would defeat its purpose.

**The migration dry-run restores a seeded snapshot first.** The migration defect that
reaches production is precisely the one that applies cleanly to a fresh database and
fails against real data volume or an existing partition set — an empty-schema dry-run
would give a false pass on exactly that case.

## 5. `web` jobs

| Stage | Job | Blocks on |
|---|---|---|
| verify | `lint` — ESLint incl. the boundary rules in `02-file-structure.md` §4 | any violation |
| verify | `typecheck` — `tsc --noEmit` | any error |
| verify | `bundle` — size budget, chat route | > 120 KB gzipped |
| test | `unit` — component tests | any failure |
| test | `build` — production build | any failure |
| e2e | `playwright` — against a running API | any failure |
| e2e | `a11y` — axe-core | any critical |
| security | `deps` · `secrets` · `sbom` | high / critical |
| contract | `codegen` — regenerate types, fail on drift | any drift |

The codegen drift check is what makes the two-project split safe. If someone hand-edits
a generated type to silence an error, CI regenerates and the diff fails — so the
frontend cannot quietly disagree with the API about what a response looks like.

---

## 6. Security scanning

GitLab ships managed scanning templates, but several are gated behind Ultimate. Rather
than build the pipeline around whichever tier AUC holds, jobs **invoke the underlying
open-source tools directly**. The pipeline then runs identically on Free, Premium, or
Ultimate, and the same commands run locally — which matters more for a security control
than dashboard integration does.

| Scan | Tool | Runs | Blocks on |
|---|---|---|---|
| Static analysis | `semgrep` (Python + TS rulesets) + `bandit` | merge request | high / critical |
| Secret detection | `gitleaks` | pre-commit + merge request + **full history, scheduled weekly** | any finding |
| Dependency vulnerabilities | `pip-audit` / `npm audit` | merge request + scheduled daily | high / critical |
| SBOM | `cyclonedx-py` / `cyclonedx-npm` | every pipeline | generated, retained as an artifact |
| Licence compliance | `pip-licenses` / `license-checker` | merge request | copyleft in a distributed component |
| Penetration test | third party | pre-pilot, then annually | high / critical open |

Findings are emitted in GitLab's report formats (`sast`, `dependency_scanning`,
`secret_detection` artifacts) so they surface in the merge request widget where the tier
supports it, and remain plain job failures where it does not.

**Full-history secret scanning runs weekly, not only on the diff.** A credential
committed and later removed is still in the history and still compromised; diff-only
scanning gives a false all-clear on exactly that case.

Container image scanning belongs to whoever builds the images — the external platform
tooling, not these projects.

---

## 7. Architectural contracts

Three `import-linter` rules that make `02-file-structure.md` enforceable rather than
aspirational.

| Contract | Rule | Protects |
|---|---|---|
| `domain-is-pure` | `askau.domain` imports nothing from `askau.*` | Testability; keeps I/O out of domain logic |
| `rag-uses-ports-only` | `askau.rag` may not import `*.adapters` or a vendor SDK | FR-042 replaceability |
| `no-sdk-outside-adapters` | `openai`, `azure.*`, raw `httpx` only under `*/adapters/` | Vendor lock-in; keeps the security review surface small |

A fourth is worth adding as the codebase grows: `authz-precedes-retrieval`, asserting
that nothing in `retrieval/` can take user input as an authorization decision. It is
hard to express statically and is covered by TC-SEC-001 in the meantime.

---

## 8. CI credentials

| Need | Grant |
|---|---|
| `web` reads the API contract | Project access token on `api`, `read_repository` only |
| Pipelines read settings | Masked, protected CI/CD variables |
| Scheduled evaluation reaches a model endpoint | Protected variable, available only on protected branches |
| Nothing else | — |

Every token is the narrowest scope that works, and secrets are **masked and protected**
so they are not exposed in job logs or to pipelines on unprotected branches. A CI token
that can write to a repository is a supply-chain risk; a read-only one is not.

---

## 9. Migration discipline

Schema changes follow **expand-contract**, always:

1. Add the nullable column
2. Ship code that writes both old and new
3. Backfill
4. Ship code that reads the new one
5. Drop the old column in a *later* release

Four changes instead of one, and it is worth it. Every schema change must leave the
previous application version working, because a rollback that needs a down-migration
against live data is not a rollback — it is a second incident during the first one.
This holds regardless of who performs the deployment.

---

## 10. Configuration & secrets

Neither project contains a secret, a connection string, or a service definition.

| Concern | Approach |
|---|---|
| Configuration | Environment variables only, documented in `.env.example` |
| Local development | `.env`, git-ignored, pointing at the externally-run Postgres and Redis |
| CI | Masked project variables; ephemeral services from the pipeline |
| Deployed environments | Injected by the platform tooling; the application only reads env vars |
| Validation | `settings.py` fails fast at startup on a missing or malformed value |

Fail-fast validation matters more than it sounds. A missing `ASKAU_DATABASE_URL` should
stop the process at startup with a clear message, not surface twenty minutes later as a
confusing error on the first query.

---

## 11. Observability is written with the feature, not after

Not a hardening-phase activity. The latency budget in `07-scaling-playbook.md` is
unverifiable without per-stage spans, and retrofitting spans across an async pipeline
costs far more than adding them as you go.

| Signal | Implementation |
|---|---|
| Traces | OpenTelemetry; one span tree covers HTTP → authz → retrieval → rerank → LLM → validation |
| Metrics | Prometheus-format; the SLO set in `07-scaling-playbook.md` |
| Logs | Structured JSON, correlation ID on every line, **content-redacted** |
| Audit | A database table with no mutation path — deliberately *not* the log pipeline |

The audit/log distinction matters for BR-008. Logs rotate, and log retention is an
operational setting someone can change; the accountability record must not be erasable
that way, so it lives in `audit_events` with no `UPDATE` or `DELETE` grant.
