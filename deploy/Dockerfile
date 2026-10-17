# market-data-hub image: `mdhub` CLI (scheduler, migrate, ops) + read API (uvicorn).
#
# Build from the repository root (compose does this):
#   docker build -f deploy/Dockerfile -t mdhub:latest .
# Excludes: deploy/Dockerfile.dockerignore (BuildKit picks it up next to this file).
#
# - Python 3.12 (matches .python-version), dependencies installed from uv.lock only
#   (`uv sync --frozen --no-dev`), no dev tools in the image.
# - postgresql-client-16 from the PGDG apt repository: pg_dump/pg_restore must match the
#   server major version (postgres:16).
# - Runs as the unprivileged user `mdhub` (uid/gid 10001).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=Europe/Moscow

# PostgreSQL 16 client tools (pg_dump / pg_restore / psql) from PGDG.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata; \
    install -d -m 0755 /usr/share/postgresql-common/pgdg; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-16; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version

# ---------------------------------------------------------------------------- build stage
FROM base AS build
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# 1) dependencies only (cached until pyproject.toml / uv.lock change)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2) the project itself (non-editable, so /app/src is not needed at runtime). Its version comes
#    from the git tags (hatch-vcs): .git_archival.txt when building from a `git archive` bundle,
#    else the MDHUB_VERSION build arg, else 0.0.0.
ARG MDHUB_VERSION=
COPY README.md .git_archival.txt ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "${MDHUB_VERSION}" ]; then export SETUPTOOLS_SCM_PRETEND_VERSION="${MDHUB_VERSION}"; fi; \
    uv sync --frozen --no-dev --no-editable

# -------------------------------------------------------------------------- runtime stage
FROM base AS runtime
ARG MDHUB_UID=10001
ARG MDHUB_GID=10001
RUN set -eux; \
    groupadd --system --gid "${MDHUB_GID}" mdhub; \
    useradd --system --uid "${MDHUB_UID}" --gid mdhub --home-dir /app --shell /usr/sbin/nologin mdhub; \
    install -d -o mdhub -g mdhub -m 0750 /var/backups/mdhub

COPY --from=build --chown=mdhub:mdhub /app/.venv /app/.venv
# Pinned release keys (README_DEPLOY §11), root-owned: read-only for the mdhub user.
COPY deploy/release-keys /app/release-keys
ENV PATH=/app/.venv/bin:$PATH \
    VIRTUAL_ENV=/app/.venv \
    MDHUB_BACKUP_DIR=/var/backups/mdhub \
    MDHUB_RELEASE_KEYS_DIR=/app/release-keys
WORKDIR /app
USER mdhub

RUN mdhub --version && mdhub release keys

EXPOSE 8080
CMD ["mdhub", "scheduler"]
