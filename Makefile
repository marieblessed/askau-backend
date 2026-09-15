.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

$(VENV):
	python3.12 -m venv $(VENV)

setup: $(VENV) ## Create the venv, install deps and pre-commit hooks
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev,ingestion]"
	-$(VENV)/bin/pre-commit install 2>/dev/null || true
	@echo "ready — copy .env.example to .env and run 'make migrate'"

run: ## Start the API with reload
	$(VENV)/bin/uvicorn askau.main:app --reload --port 8080

migrate: ## Apply migrations to head
	$(VENV)/bin/alembic upgrade head

migrate-down: ## Roll back one revision
	$(VENV)/bin/alembic downgrade -1

migrate-reset: ## Drop everything and re-apply (destructive, dev only)
	$(VENV)/bin/alembic downgrade base && $(VENV)/bin/alembic upgrade head

seed: ## Load the synthetic corpus and test identities
	$(PY) -m askau.scripts.seed

reconcile-acls: ## Bring chunks.acl_principals back in step with document_acl
	$(PY) -m askau.scripts.reconcile_acls

provision-user: ## Create an AskAU account: make provision-user OID=... EMAIL=... NAME="..." [DEPARTMENT=... JOB_TITLE=... ROLES="end_user knowledge_admin" DRY_RUN=1]
	@$(PY) -m askau.scripts.provision_user \
	  --oid "$(OID)" --email "$(EMAIL)" --name "$(NAME)" \
	  $(if $(DEPARTMENT),--department "$(DEPARTMENT)") \
	  $(if $(JOB_TITLE),--job-title "$(JOB_TITLE)") \
	  $(foreach r,$(ROLES),--role $(r)) \
	  $(if $(DRY_RUN),--dry-run)

ingest: ## Run one ingestion pass: make ingest SOURCE=<uuid> [RUN=<uuid>]
	$(PY) -m askau.scripts.ingest --source "$(SOURCE)" $(if $(RUN),--run "$(RUN)")

dev-tokens: ## Every seeded identity as JSON, for the web client's .dev-tokens.json
	@$(PY) -m askau.scripts.dev_tokens

dev-token: ## Mint a dev bearer token: make dev-token USER=staff.misd
	@$(PY) -m askau.scripts.dev_token $(USER)

test: ## Full test suite
	$(VENV)/bin/pytest -q

test-unit: ## Unit tests only (no database)
	$(VENV)/bin/pytest -q tests/unit

test-security: ## Authorization isolation suite (TC-SEC-*)
	$(VENV)/bin/pytest -q tests/security -v

lint: ## Ruff + format check + architectural import contracts
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests
	$(VENV)/bin/lint-imports

fmt: ## Apply formatting and safe fixes
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

typecheck: ## Static type check
	$(VENV)/bin/mypy src

contract: ## Regenerate contracts/openapi.json
	$(PY) -m askau.scripts.export_openapi

contract-check: ## Fail if the committed contract has drifted
	@$(PY) -m askau.scripts.export_openapi --check

check: lint typecheck test contract-check ## Everything CI will run

.PHONY: help setup run migrate migrate-down migrate-reset seed reconcile-acls provision-user ingest dev-token dev-tokens \
        test test-unit test-security lint fmt typecheck contract contract-check check eval-real

eval-real: ## Re-seed with the real embedding model and measure retrieval quality
	@# Two steps and not one, because the embedder is a property of the *corpus*:
	@# a query embedded by bge-m3 cannot be compared against vectors written by
	@# the hash embedder — they are points in unrelated spaces. Seeding and
	@# evaluating must therefore agree, which is why this target does both and
	@# why `make eval` alone would be a trap.
	@echo "re-embedding with $(or $(MODEL),BAAI/bge-m3) — first run downloads ~2GB"
	@ASKAU_EMBEDDING_PROVIDER=sentence_transformers ASKAU_EMBEDDING_MODEL=$(or $(MODEL),BAAI/bge-m3) \
	  $(PY) -m askau.scripts.seed >/dev/null
	@ASKAU_EMBEDDING_PROVIDER=sentence_transformers ASKAU_EMBEDDING_MODEL=$(or $(MODEL),BAAI/bge-m3) \
	  $(PY) -m askau.evaluation.cli
	@printf "\n  NOTE: the corpus is now embedded with %s.\n" "$(or $(MODEL),BAAI/bge-m3)"
	@echo "        Run 'make seed' to return it to the hash embedder before 'make test'."
