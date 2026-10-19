# syntax=docker/dockerfile:1
# One image recipe for every Python service. Build args pick the workspace package:
#   PACKAGE  uv workspace package to install (trader, recorder, dashboard, lab, alerter, gate, trading-core)
#   APP_DIR  its folder, used as the working directory (tests and alembic.ini live there)
#   GIT_COMMIT  the commit being built (`GIT_COMMIT=$(git rev-parse HEAD) docker compose build`)

ARG UV_VERSION=0.12.21
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# NautilusTrader wheels are manylinux_2_35, so the base needs glibc 2.35+ (bookworm has 2.36).
FROM python:3.14-slim-bookworm AS runtime
ARG PACKAGE
ARG APP_DIR

ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /usr/local/bin/uv
# Non root user. Folders created here keep this owner when a named volume first mounts them.
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app /data/catalog /data/spool /run/heartbeat \
    && chown app:app /app /data/catalog /data/spool /run/heartbeat
WORKDIR /app

# Third party dependencies first, so code edits do not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/trader/pyproject.toml apps/trader/
COPY apps/recorder/pyproject.toml apps/recorder/
COPY apps/dashboard/pyproject.toml apps/dashboard/
COPY apps/lab/pyproject.toml apps/lab/
COPY apps/alerter/pyproject.toml apps/alerter/
COPY apps/gate/pyproject.toml apps/gate/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package "$PACKAGE" --no-install-workspace

COPY --chown=app:app packages packages
COPY --chown=app:app apps apps
# The design system's source of truth; the dashboard's token test compares it with tokens.css (spec 0004).
COPY --chown=app:app DESIGN.md DESIGN.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package "$PACKAGE"

# The commit this image was built from, recorded on every strategy version (spec 0002).
# Declared after the installs so a new commit does not reinstall anything.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

USER app
WORKDIR /app/${APP_DIR}


# Laptop image: the same plus the package's dev group (pytest), so
# `docker compose run --rm <app> pytest -q` works.
FROM runtime AS dev
ARG PACKAGE
USER root
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /app && uv sync --frozen --package "$PACKAGE"
USER app


# Tools image: every workspace package plus all dev groups in one env, for mypy and
# pytest across the whole repo. Compose bind mounts the repo over /app (service `tools`).
FROM runtime AS tools
USER root
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /app && uv sync --frozen --all-packages --group dev \
    && mkdir -p /cache && chown app:app /cache
USER app
WORKDIR /app
