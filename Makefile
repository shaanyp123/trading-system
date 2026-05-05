.PHONY: help install lint format typecheck test test-unit test-integration test-golden ci all clean

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

help:
	@echo "Common targets:"
	@echo "  make install       — install Python deps (incl. dev) into current venv"
	@echo "  make lint          — ruff format --check + ruff check"
	@echo "  make format        — ruff format (writes changes)"
	@echo "  make typecheck     — mypy --strict on services + infrastructure + strategies"
	@echo "  make test          — pytest with coverage"
	@echo "  make test-unit     — pytest tests/unit only"
	@echo "  make ci            — what CI runs: lint + typecheck + test"
	@echo "  make all           — format + lint + typecheck + test"

install:
	$(PIP) install -e ".[dev]"

lint:
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy services infrastructure strategies scripts

test:
	$(PYTHON) -m pytest

test-unit:
	$(PYTHON) -m pytest tests/unit

test-integration:
	$(PYTHON) -m pytest tests/integration -m integration

test-golden:
	$(PYTHON) -m pytest tests/golden -m golden

ci: lint typecheck test

all: format lint typecheck test

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} \;
	find . -type d -name .pytest_cache -prune -exec rm -rf {} \;
	find . -type d -name .mypy_cache -prune -exec rm -rf {} \;
	find . -type d -name .ruff_cache -prune -exec rm -rf {} \;
	rm -rf .coverage htmlcov coverage.xml
