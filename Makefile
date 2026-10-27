.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test cov check docker clean

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install the project and dev tools, set up git hooks
	uv sync
	uv run pre-commit install

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and fix lint issues
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Run mypy in strict mode
	uv run mypy

test: ## Run the test suite
	uv run pytest

cov: ## Run tests with an HTML coverage report
	uv run pytest --cov --cov-report=html
	@echo "open htmlcov/index.html"

check: lint typecheck test ## Everything CI runs

docker: ## Build the container image
	docker build -t proxmox-ops-mcp:dev .

clean: ## Remove caches and build artefacts
	rm -rf dist build .coverage coverage.xml htmlcov .mypy_cache .pytest_cache .ruff_cache
