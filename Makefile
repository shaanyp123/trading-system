.PHONY: help install lint format typecheck test test-unit test-integration test-golden dep-drift-check frontend-test research ci all clean

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PNPM ?= pnpm

help:
	@echo "Common targets:"
	@echo "  make install       — install Python deps (incl. dev) into current venv"
	@echo "  make lint          — ruff format --check + ruff check"
	@echo "  make format        — ruff format (writes changes)"
	@echo "  make typecheck     — mypy --strict on services + infrastructure + strategies + research"
	@echo "  make research RUN=<cfg> — run a futures research config → research/runs/<ts>/report.html"
	@echo "  make test          — pytest with coverage"
	@echo "  make test-unit     — pytest tests/unit only"
	@echo "  make dep-drift-check — diff pyproject runtime deps vs api Dockerfile pip-install list"
	@echo "  make frontend-test — pnpm typecheck + lint + build in apps/web"
	@echo "  make ci            — what CI runs: lint + dep-drift-check + typecheck + test + frontend-test"
	@echo "  make all           — format + lint + typecheck + test + frontend-test"

install:
	$(PIP) install -e ".[dev]"

lint:
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy services infrastructure strategies scripts research

test:
	$(PYTHON) -m pytest

test-unit:
	$(PYTHON) -m pytest tests/unit

test-integration:
	$(PYTHON) -m pytest tests/integration -m integration

test-golden:
	$(PYTHON) -m pytest tests/golden -m golden

dep-drift-check:
	$(PYTHON) scripts/check_dockerfile_deps_against_pyproject.py

# Day 20: frontend test gate. Runs pnpm typecheck + lint + build against
# apps/web/. Wired into `make ci` so future Python-only diffs don't
# accidentally regress the Next.js build. Requires the apps/web/
# scaffold (Day 20 PR) + `pnpm install` in apps/web before first run.
frontend-test:
	cd apps/web && $(PNPM) typecheck && $(PNPM) lint && $(PNPM) build

# Futures research harness (Docs/futures-backtester-design.md). Runs one config
# through the daily spine → research/runs/<ts>/report.html. Usage:
#   make research RUN=research/config/examples/p1_buy_and_hold.yaml
research:
	$(PYTHON) -m research.run --config $(RUN)

ci: lint dep-drift-check typecheck test frontend-test

all: format lint dep-drift-check typecheck test frontend-test

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} \;
	find . -type d -name .pytest_cache -prune -exec rm -rf {} \;
	find . -type d -name .mypy_cache -prune -exec rm -rf {} \;
	find . -type d -name .ruff_cache -prune -exec rm -rf {} \;
	rm -rf .coverage htmlcov coverage.xml
