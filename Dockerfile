# syntax=docker/dockerfile:1
#
# Multi-stage build: the builder stage compiles/installs into an isolated venv
# so the runtime stage never carries build tooling (gcc, headers) or pip's
# cache. Dependencies are installed before source is copied so that editing
# application code never invalidates the (slow) dependency layer.
#
# Python 3.14 is the newest maintained CPython branch (EOL 2030-10-31) — see
# docs/superpowers/specs/2026-08-23-telegram-ai-cli-mcp-design.md §3. Do not
# downgrade to 3.12/3.13 here; those remain supported only as the floor in
# pyproject.toml and as CI matrix entries.
FROM python:3.14-slim AS builder

# cryptg and cryptography ship source builds for some platforms; build-essential
# covers that without bloating the final runtime image (it never leaves this stage).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# The dependency-only install happens in its own directory, deliberately.
#
# Copy only the metadata needed to resolve dependencies first — this layer is
# cached across rebuilds as long as pyproject.toml/constraints.txt are unchanged,
# so `docker build` on a pure source-code change skips the network entirely.
WORKDIR /deps
COPY pyproject.toml constraints.txt ./
# setuptools needs *something* under the package dir to validate the build
# metadata at this stage; a minimal placeholder keeps the dependency-only
# install self-contained without pulling in real source yet.
RUN mkdir -p telegram_ai_cli_mcp && touch telegram_ai_cli_mcp/__init__.py

# `pip install .` (rather than `pip install -e .`) here only resolves and
# installs the dependency graph pinned by constraints.txt; the real package is
# installed over it from /build below.
RUN pip install -c constraints.txt .

# Build the real package somewhere the placeholder never touched. Sharing one
# directory silently shipped a BROKEN image: the placeholder install leaves
# setuptools' build/lib/telegram_ai_cli_mcp/__init__.py behind as an empty file
# stamped at build time, and build_py copies a source file only when it is
# newer than its destination. The real __init__.py keeps its original mtime,
# loses that comparison, and never makes it into the wheel — while every other
# module, having no stale counterpart, copies fine. The image then builds, runs
# as non-root, and fails on the first import with a missing __version__.
# --force-reinstall because the static version means pip would otherwise call
# the already-installed placeholder "already satisfied" and skip this entirely.
WORKDIR /build
COPY pyproject.toml constraints.txt ./
COPY telegram_ai_cli_mcp/ ./telegram_ai_cli_mcp/
RUN pip install -c constraints.txt --no-deps --force-reinstall .

# --- runtime -----------------------------------------------------------------
FROM python:3.14-slim AS runtime

# UID/GID default to the host `deploy` user (see CLAUDE.md environment rules)
# so bind-mounted directories (accounts/, sessions/, config) stay writable by
# the host owner instead of becoming root-owned. Override at build time for a
# different host user: --build-arg UID=... --build-arg GID=...
ARG UID=1002
ARG GID=1002

RUN groupadd --gid "${GID}" tgai \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin tgai

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runtime state directories: accounts/sessions hold live MTProto auth keys and
# must never be owned by root, or the non-root process can't write to them.
RUN mkdir -p /app/accounts /app/sessions /app/config \
    && chown -R "${UID}:${GID}" /app

WORKDIR /app
USER tgai

ENTRYPOINT ["tg-ai"]
CMD ["--help"]
