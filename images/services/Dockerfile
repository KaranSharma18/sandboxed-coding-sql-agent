# syntax=docker/dockerfile:1.7
# One image for every service (api, worker, runner, publisher, pkgproxy); compose and Kubernetes
# pick the command. Multi-stage: dependencies are resolved from uv.lock in a build stage, and the
# runtime stage has only the virtual environment, configuration, prompts and migrations.
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY packages packages
COPY apps apps
COPY eval eval
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --all-packages --no-editable

FROM python:3.12-slim-bookworm
# Mount points for named volumes are created here, owned by the service user, so a fresh volume
# inherits that ownership (the package proxy writes its cache as this user).
RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin fixloop \
 && mkdir -p /data/pkgcache /data/runner \
 && chown -R fixloop:fixloop /data
COPY --from=build /app/.venv /app/.venv
COPY config /app/config
COPY prompts /app/prompts
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FIXLOOP_CONFIG_DIR=/app/config \
    FIXLOOP_PROMPTS_DIR=/app/prompts
WORKDIR /app
USER fixloop
EXPOSE 8080
CMD ["fixloop-api"]
