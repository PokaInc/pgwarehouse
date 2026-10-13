SHELL := /usr/bin/env bash

.DEFAULT_GOAL := help
.PHONY: help
help: ## Display this help section
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


.PHONY: install
install: _ensure_uv_is_installed ## Install all project dependencies
	@uv sync --locked


.PHONY: lock
lock: _ensure_uv_is_installed ## Regenerate uv.lock after changing dependencies in pyproject.toml
	uv lock


.PHONY: upgrade-python-dependencies
upgrade-python-dependencies: ## Upgrade Python dependencies
	uv sync --upgrade


.PHONY: build
build: _ensure_uv_is_installed ## Build the sdist and wheel into ./dist
	@uv build


.PHONY: clean
clean: ## Remove Python cache files
	@find . -name "*.py[co]" -o -name __pycache__ -o -name "*.egg-info" -exec rm -rf {} +


.PHONY: test
test:  ## Run the tests using Pytest
	@uv run pytest --disable-warnings pgwarehouse/tests/


DOCKER_IMAGE = pgwarehouse-dev

.PHONY: docker-image
docker-image:  ## Build the Docker image used by test-docker and build-docker
	@docker build -t $(DOCKER_IMAGE) .


.PHONY: test-docker
test-docker: docker-image  ## Run the tests in Docker (no local Postgres install required)
	@docker run --rm $(DOCKER_IMAGE)


# Runs as the host user so the files written to ./dist are not owned by root on Linux hosts
.PHONY: build-docker
build-docker: docker-image  ## Build the sdist and wheel into ./dist using Docker (no local uv required)
	@mkdir -p dist
	@docker run --rm --user $$(id -u):$$(id -g) -e UV_CACHE_DIR=/tmp/uv-cache -v "$(CURDIR)/dist:/out" $(DOCKER_IMAGE) uv build -o /out


.PHONY: _ensure_uv_is_installed
_ensure_uv_is_installed:
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "Installing uv..."; \
		pip install uv; \
	fi
