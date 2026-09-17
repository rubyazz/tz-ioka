SHELL := /bin/bash
COMPOSE := docker compose

# docker compose reads env from .env; bootstrap it from the template once.
ensure_env = @[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up: ## Build and start the whole stack (api on :8000, rabbit ui on :15672)
	$(ensure_env)
	$(COMPOSE) up --build -d
	@echo "API:     http://localhost:8000/travel/docs  (agent / agent123)"
	@echo "Rabbit:  http://localhost:15672 (ioka/ioka)"

.PHONY: down
down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail api + worker logs
	$(COMPOSE) logs -f api worker

.PHONY: test
test: ## Run all tests against started infra (unit + integration)
	$(ensure_env)
	$(COMPOSE) up -d postgres redis rabbitmq
	-$(COMPOSE) exec -T postgres createdb -U ioka ioka_travel_test
	$(COMPOSE) build api
	$(COMPOSE) run --rm --no-deps \
		-v "$(CURDIR)/tests:/srv/tests" \
		-e PYTEST_ADDOPTS="-p no:cacheprovider" \
		api sh -c "pip install -q pytest pytest-asyncio httpx && python -m pytest -q"

.PHONY: lint
lint: ## Ruff lint + format check (in a throwaway container)
	docker run --rm -v "$(CURDIR):/src" -w /src -e RUFF_CACHE_DIR=/tmp/ruff python:3.12-slim \
		sh -c "pip install -q ruff && ruff check app scripts alembic tests && ruff format --check app scripts alembic tests"

.PHONY: fmt
fmt: ## Ruff autofix + format (in a throwaway container)
	docker run --rm -v "$(CURDIR):/src" -w /src -e RUFF_CACHE_DIR=/tmp/ruff python:3.12-slim \
		sh -c "pip install -q ruff && ruff check --fix app scripts alembic tests; ruff format app scripts alembic tests"

.PHONY: hooks
hooks: ## Install pre-commit git hooks (requires: pip install pre-commit)
	pre-commit install

.PHONY: precommit
precommit: ## Run all pre-commit hooks on all files
	pre-commit run --all-files

.PHONY: migrate
migrate: ## Apply alembic migrations
	$(ensure_env)
	$(COMPOSE) run --rm migrate

.PHONY: migration
migration: ## Autogenerate a migration (make migration m="add foo table"); file lands in ./alembic/versions
	$(ensure_env)
	$(COMPOSE) build migrate
	$(COMPOSE) run --rm -v "$(CURDIR)/alembic/versions:/srv/alembic/versions" \
		migrate alembic revision --autogenerate -m "$(m)"
	@sudo chown -R `id -u` alembic/versions 2>/dev/null || true
