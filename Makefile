SHELL := /usr/bin/env bash

.DEFAULT_GOAL := help
.PHONY: help install lock upgrade-python-dependencies build clean test docker-image test-docker build-docker _ensure_uv_is_installed

help: ## Display this help section
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


install: _ensure_uv_is_installed ## Install all project dependencies
	@uv sync --locked


lock: _ensure_uv_is_installed ## Regenerate uv.lock after changing dependencies in pyproject.toml
	uv lock


upgrade-python-dependencies: ## Upgrade Python dependencies
	uv sync --upgrade


build: _ensure_uv_is_installed ## Build the sdist and wheel into ./dist
	@uv build


clean: ## Remove Python cache files
	@find . -name "*.py[co]" -o -name __pycache__ -o -name "*.egg-info" -exec rm -rf {} +


test:  ## Run the tests using Pytest
	@uv run pytest --disable-warnings pgwarehouse/tests/


DOCKER_IMAGE = pgwarehouse-dev

docker-image:  ## Build the Docker image used by test-docker and build-docker
	@docker build -t $(DOCKER_IMAGE) .


test-docker: docker-image  ## Run the tests in Docker (no local Postgres install required)
	@docker run --rm $(DOCKER_IMAGE)


# Runs as the host user so the files written to ./dist are not owned by root on Linux hosts
build-docker: docker-image  ## Build the sdist and wheel into ./dist using Docker (no local uv required)
	@mkdir -p dist
	@docker run --rm --user $$(id -u):$$(id -g) -e UV_CACHE_DIR=/tmp/uv-cache -v "$(CURDIR)/dist:/out" $(DOCKER_IMAGE) uv build -o /out


_ensure_uv_is_installed:
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "Installing uv..."; \
		pip install uv; \
	fi
