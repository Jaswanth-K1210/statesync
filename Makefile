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
        smoke smoke-frontend verify determinism eval eval-arm demo lint clean \
        warm-cache readme reproduce stop stop-hard

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

stop:  ## stop this project's containers and test runs, and nothing else
	@bash scripts/stop.sh

stop-hard:  ## stop and drop volumes
	@bash scripts/stop.sh --hard

test: test-unit test-prop test-int test-chaos  ## every test layer built so far

test-unit:  ## one function, no I/O (<5s)
	$(PYTEST) tests/unit -q

test-prop:  ## invariants under generated input (hypothesis)
	$(PYTEST) tests/property -q

test-int:  ## real Postgres + Redis, full pipeline (<2min)
	$(PYTEST) tests/integration -q

test-chaos:  ## injected failures and degradation paths
	$(PYTEST) tests/chaos -q

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

verify: lint test-unit test-prop test-int test-chaos smoke determinism  ## green before any commit to main
	@echo "verify: green"

lint:  ## ruff + mypy --strict
	$(PY) -m ruff check src tests eval
	$(PY) -m mypy

eval:  ## the 3-arm measurement run, regenerates README numbers
	$(PY) -m eval.harness --seed $(SEED) --n 500

warm-cache:  ## populate and commit the LLM cache (records which client filled it)
	$(PY) -m eval.warm_cache

eval-arm:  ## a single arm, e.g. make eval-arm ARM=rules
	$(PY) -m eval.harness --seed $(SEED) --n 500 --arm $(ARM)

readme:  ## regenerate README.md from measured output
	$(PY) -m eval.readme

demo:  ## the exact video sequence, from a warm cache and no API key
	@bash scripts/demo.sh

reproduce:  ## clone into a temp dir and run setup + verify, as a stranger would
	@bash scripts/reproduce.sh

clean:  ## drop caches and stop services
	$(COMPOSE) down -v
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
