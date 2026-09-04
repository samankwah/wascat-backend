# Two stages: `builder` resolves and installs the locked dependency set with
# uv, `runtime` is what actually ships. Nothing from the build toolchain -
# uv itself, the wheel cache, pip - ends up in the image that runs in
# production; only the venv it produced and the application source do.
#
# docker-compose.yml's `api` service (profile "full") already builds this
# image at `target: runtime` for a container-shaped local smoke test, so this
# file is exercised locally the same way it is in production, not just on
# whatever platform deploys it first.

# Pinned via a build arg rather than `pip install uv` unpinned, so a uv
# release doesn't change what a rebuild produces. A named stage, not
# `COPY --from=ghcr.io/...:${UV_VERSION}` directly - BuildKit does not expand
# an ARG inside --from unless it comes from a FROM it already resolved.
ARG UV_VERSION=0.11.17
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.13-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so an application-code change doesn't invalidate the
# (much slower) dependency-install layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now the source, and the project itself (its own console script,
# `wascat = "wascat.cli:app"`, needs the package installed to resolve).
# README.md has to come along too: hatchling reads `readme = "README.md"`
# from pyproject.toml and refuses to build the package without the file
# actually being there, even though nothing at runtime imports it.
COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.13-slim AS runtime

# libjpeg/zlib runtime libraries for Pillow - the builder stage's wheel
# already links against them, but the slim base doesn't ship them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 wascat
WORKDIR /app

COPY --from=builder --chown=wascat:wascat /app/.venv /app/.venv
COPY --from=builder --chown=wascat:wascat /app/src /app/src
COPY --from=builder --chown=wascat:wascat /app/migrations /app/migrations
COPY --from=builder --chown=wascat:wascat /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER wascat

# Render (and most PaaS hosts) inject $PORT and route to it; 8000 is the
# fallback for `docker run` without one set, matching docker-compose.yml.
ENV PORT=8000
EXPOSE 8000

# Same command docker-compose.yml's `api` service already runs: migrate,
# then serve. --proxy-headers/--forwarded-allow-ips trust the platform's own
# reverse proxy for X-Forwarded-* rather than the raw connecting IP, which is
# always the platform's edge, never the real client, behind Render or Fly.
CMD ["sh", "-c", "alembic upgrade head && uvicorn wascat.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
