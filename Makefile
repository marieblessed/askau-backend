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

worker: ## Run the ingestion worker
	$(PY) -m askau.workers.main

migrate: ## Apply migrations to head
	$(VENV)/bin/alembic upgrade head

migrate-down: ## Roll back one revision
	$(VENV)/bin/alembic downgrade -1

migrate-reset: ## Drop everything and re-apply (destructive, dev only)
	$(VENV)/bin/alembic downgrade base && $(VENV)/bin/alembic upgrade head

seed: ## Load the synthetic corpus and test identities
	$(PY) -m askau.scripts.seed

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

.PHONY: help setup run worker migrate migrate-down migrate-reset seed dev-token \
        test test-unit test-security lint fmt typecheck contract contract-check check dev-token-web

dev-token-web: ## Refresh BOTH dev tokens in ../web/.env.local
	@# Two tokens, because the console and the chat are used by different
	@# people: §6.4 separates knowledge, system and security administration so
	@# that no single account can both change the corpus and erase the record
	@# of having done so. Refreshing only one leaves the other silently 401ing.
	@$(PY) -m askau.scripts.dev_token $(or $(AS_USER),staff.finance) > /tmp/.askau-tok
	@$(PY) -m askau.scripts.dev_token $(or $(AS_ADMIN),admin.system) > /tmp/.askau-tok-admin
	@touch ../web/.env.local
	@grep -vE '^ASKAU_DEV_TOKEN(_ADMIN)?=' ../web/.env.local > /tmp/.askau-env || true
	@echo "ASKAU_DEV_TOKEN=$$(cat /tmp/.askau-tok)" >> /tmp/.askau-env
	@echo "ASKAU_DEV_TOKEN_ADMIN=$$(cat /tmp/.askau-tok-admin)" >> /tmp/.askau-env
	@mv /tmp/.askau-env ../web/.env.local
	@rm -f /tmp/.askau-tok /tmp/.askau-tok-admin
	@echo "30-day tokens written: $(or $(AS_USER),staff.finance) (chat), $(or $(AS_ADMIN),admin.system) (console)"
