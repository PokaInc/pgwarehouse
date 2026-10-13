# Dev image: bundles the Postgres server binaries that testing.postgresql spawns, so the
# test suite can run without a local Postgres install (see `make test-docker`). Also used
# to build the package without a local uv install (see `make build-docker`).
FROM python:3.14-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

# postgresql: server binaries (initdb, postgres) used by the tests
# gcc + libc6-dev + libpq-dev: psycopg2 is built from source
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql gcc libc6-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/lib/postgresql/*/bin/initdb /usr/lib/postgresql/*/bin/postgres /usr/local/bin/

# Postgres refuses to run as root. The home dir is made world-readable because
# `make build-docker` runs as the host user (trixie defaults home dirs to 0700).
RUN useradd --create-home app && chmod 755 /home/app
USER app
WORKDIR /home/app/pgwarehouse

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first so they are cached across source changes
COPY --chown=app:app pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-install-project

COPY --chown=app:app . .
RUN uv sync --locked

CMD ["uv", "run", "pytest", "--disable-warnings", "pgwarehouse/tests/"]
