# StateSync — the single entrypoint. Every claim in the README is one of these.
#
# Targets for phases not yet built announce themselves rather than passing
# silently: a green no-op is worse than a red failure.

PY      := .venv/bin/python
PYTEST  := $(PY) -m pytest
SEED    := 20260905
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help setup up down test test-unit test-prop test-int test-chaos test-e2e \
        smoke smoke-frontend verify determinism eval eval-arm demo lint clean

help:  ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup:  ## install deps, start services, apply migrations
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e ".[dev]"
	$(COMPOSE) up -d --wait
	$(PY) -c "from statesync.ledger.store import apply_migrations; \
	          print('migrations:', apply_migrations())"

up:  ## start Postgres and Redis (skipped when STATESYNC_NO_COMPOSE=1)
	@if [ "$$STATESYNC_NO_COMPOSE" = "1" ]; then echo "up: using external services"; \
		else $(COMPOSE) up -d --wait; fi

down:  ## stop Postgres and Redis
	$(COMPOSE) down

test: test-unit test-int  ## every test layer built so far

test-unit:  ## one function, no I/O (<5s)
	$(PYTEST) tests/unit -q

test-prop:  ## invariants under generated input (Phase 3)
	@echo "test-prop: no property tests until Phase 3 (idempotency)."

test-int:  ## real Postgres + Redis, full pipeline (<2min)
	$(PYTEST) tests/integration -q

test-chaos:  ## injected failures and degradation paths (Phase 5)
	@echo "test-chaos: no chaos tests until Phase 5 (LLM degradation)."

test-e2e:  ## Playwright ops-person journeys (Phase 6)
	@echo "test-e2e: no frontend until Phase 6."

smoke: up  ## does it boot and do one real thing? (<30s, always)
	@start=$$(date +%s); $(PYTEST) tests/smoke -q; \
		elapsed=$$(($$(date +%s) - start)); \
		echo "smoke: $${elapsed}s of a 30s budget"; \
		test $$elapsed -le 30 || (echo "SMOKE OVER BUDGET" && exit 1)

determinism:  ## the same seed must produce byte-identical output
	@a=$$($(PY) -m statesync.generator --seed $(SEED) --n 500 --digest); \
		b=$$($(PY) -m statesync.generator --seed $(SEED) --n 500 --digest); \
		test "$$a" = "$$b" && echo "determinism: OK  $$a" \
		|| (echo "determinism: BROKEN  $$a != $$b" && exit 1)

verify: lint test-unit test-int smoke determinism  ## green before any commit to main
	@echo "verify: green"

lint:  ## ruff + mypy --strict
	$(PY) -m ruff check src tests
	$(PY) -m mypy

eval:  ## the 3-arm measurement run (Phase 2)
	@echo "eval: arms 1-2 land in Phase 2, arm 3 in Phase 5."

eval-arm:  ## a single arm, e.g. make eval-arm ARM=rules (Phase 2)
	@echo "eval-arm: Phase 2."

demo:  ## the exact video sequence (Phase 7)
	@echo "demo: Phase 7."

clean:  ## drop caches and stop services
	$(COMPOSE) down -v
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
